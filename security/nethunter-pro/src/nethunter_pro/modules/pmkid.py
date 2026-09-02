"""PMKID capture with hcxdumptool -- the clientless WPA/WPA2 attack.

Modern APs include a PMKID in the first EAPOL M1 frame the moment any
station tries to associate. hcxdumptool exploits this by sending
association requests to every visible AP and recording the responses
into a pcapng file. If the AP is old enough to leak PMKIDs (many still
do), you have a hash that can be cracked offline for the PSK -- no
client needed on the target network, no deauth required.

This module runs hcxdumptool through the DBus root helper (we need
raw sockets), streams its stdout into the terminal view, and drops
the pcapng into the central loot store the moment the user hits Stop.
The user then hops to the Loot module and submits the pcap to
wpa-sec.stanev.org for distributed cracking. No hashcat runs on the
phone.

Two capture profiles are exposed:

* Passive -- ``--disable_deauthentication --disable_association``.
  hcxdumptool only listens; PMKIDs that come out of unrelated
  associations by other stations are still recorded. Useful when
  active probing is out of scope (physical recon, stealth).

* Active -- the default. hcxdumptool sends association requests to
  every audible AP roughly every second. Way more PMKIDs, way more
  RF noise. Only run this if you have permission to touch the APs
  in range.

Monitor mode setup mirrors ``deauth.py``: isolate the external adapter
(wlan1 by default) so the phone's own Wi-Fi (wlan0) stays managed
and SSH survives.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import LOOT_ROOT, get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

DEFAULT_IFACE = "wlan1"
MON_SUFFIX = "mon"


def _mon_iface(base: str) -> str:
    return base + MON_SUFFIX


# hcxdumptool prints one line per new AP / PMKID it observes. We match
# both the "PMKID" tag and the "EAPOL" tag so the live counters go up
# for either kind of capture (both crackable at wpa-sec).
_LINE_PMKID = re.compile(r"\b(PMKID|EAPOL M[0-9])\b")
_LINE_AP = re.compile(
    r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})\s+.*\[([^\]]{1,32})\]", re.I)


@register
class Pmkid(NHModule):
    title = "PMKID capture"
    icon = "network-wireless-encrypted-symbolic"
    description = ("Grab WPA/WPA2 PMKIDs from every AP in range with "
                   "hcxdumptool; submit them to wpa-sec for offline "
                   "cracking.")
    required_tools = ["hcxdumptool"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._monitor_iface: str | None = None
        self._proc: Process | None = None
        self._pcap_path: str | None = None
        self._loot_id: int | None = None
        self._start_ts: float = 0.0
        # ap_map[bssid] = ssid (or "" if hidden). Kept fresh from
        # the hcxdumptool line stream.
        self._ap_map: dict[str, str] = {}
        # Bumped every time we see a PMKID / EAPOL M-frame in the
        # output. Approximate but plenty for the UI counter.
        self._pmkid_hits: int = 0
        self._tick_id: int | None = None

    # ---------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['NetworkManager', 'gpsd']))

        # ---- monitor mode
        mon_group = Adw.PreferencesGroup(
            title="Monitor mode",
            description="Puts an external adapter into monitor mode "
                        "without touching NetworkManager on the "
                        "phone's own Wi-Fi. SSH survives.")
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

        # ---- capture profile
        cap_group = Adw.PreferencesGroup(
            title="Capture",
            description="hcxdumptool runs as root and writes a pcapng "
                        "under ~/loot/pmkid/. Passive listens only; "
                        "Active probes every AP.")

        self.profile = Adw.ComboRow(title="Profile")
        self.profile.set_model(Gtk.StringList.new([
            "Active (probe every AP)",
            "Passive (listen only)",
        ]))
        cap_group.add(self.profile)

        self.band = Adw.ComboRow(title="Band")
        self.band.set_model(Gtk.StringList.new([
            "2.4 GHz + 5 GHz (default channels)",
            "2.4 GHz only (1-14)",
            "5 GHz only (36-165)",
        ]))
        cap_group.add(self.band)

        self.rds = Adw.SwitchRow(
            title="Include GPS in pcapng",
            subtitle="Adds --rds=1; needs gpsd running to be useful")
        cap_group.add(self.rds)

        cap_row = Adw.ActionRow(
            title="Start / Stop capture",
            subtitle="Turn monitor on first")
        self.start_btn = Gtk.Button(label="Start",
                                    valign=Gtk.Align.CENTER)
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", lambda _b: self._start())
        cap_row.add_suffix(self.start_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        cap_row.add_suffix(self.stop_btn)
        cap_group.add(cap_row)
        box.append(cap_group)

        # ---- live status
        stat_group = Adw.PreferencesGroup(title="Session")
        self.state_row = Adw.ActionRow(
            title="Status", subtitle="idle")
        stat_group.add(self.state_row)
        self.file_row = Adw.ActionRow(
            title="Output file", subtitle="—")
        stat_group.add(self.file_row)
        self.hits_row = Adw.ActionRow(
            title="PMKID / EAPOL frames seen", subtitle="0")
        stat_group.add(self.hits_row)
        self.aps_row = Adw.ActionRow(
            title="Access points heard", subtitle="0")
        stat_group.add(self.aps_row)

        loot_row = Adw.ActionRow(
            title="Loot",
            subtitle="Submit to wpa-sec once the capture is stopped")
        open_loot_btn = Gtk.Button(label="Open Loot",
                                   valign=Gtk.Align.CENTER)
        open_loot_btn.connect(
            "clicked", lambda _b: self._open_loot())
        loot_row.add_suffix(open_loot_btn)
        stat_group.add(loot_row)
        box.append(stat_group)

        # ---- live log
        self.output = OutputView()
        box.append(self.output)

        self._detect_existing_monitor()
        return box

    # ---------------------------------------------------------- monitor
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
                    "%s is already in monitor mode" % mon)

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
        self.output.append("# enabling monitor on %s\n" % iface)
        script = (
            "IF=%s; MON=%s; "
            "PHY=$(readlink /sys/class/net/$IF/phy80211 2>/dev/null "
            "     | awk -F/ '{print $NF}'); "
            "if [ -z \"$PHY\" ] && [ -e /sys/class/net/$MON/phy80211 ]; then "
            "  PHY=$(readlink /sys/class/net/$MON/phy80211 "
            "     | awk -F/ '{print $NF}'); "
            "fi; "
            "[ -n \"$PHY\" ] || { echo 'no phy for '$IF; exit 1; }; "
            "nmcli device set $IF managed no 2>/dev/null || true; "
            "iw dev $IF del 2>/dev/null || true; "
            "iw dev $MON del 2>/dev/null || true; "
            "iw phy $PHY interface add $MON type monitor "
            "  2>/dev/null || true; "
            "ip link set $MON up && echo ok || echo 'ip link set failed'; "
            "iw dev $MON info | head -6"
        ) % (iface, mon)

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            if r.returncode == 0 and "type monitor" in (r.stdout or ""):
                self._monitor_iface = mon
                self.mon_row.set_subtitle(
                    "%s is in monitor mode" % mon)
                toast(self.app_window, "Monitor mode on: " + mon)
            else:
                self._monitor_iface = None
                self.mon_row.set_subtitle(
                    "Enable failed; see log below")
                self.mon_toggle.handler_block_by_func(
                    self._on_monitor_toggle)
                self.mon_toggle.set_active(False)
                self.mon_toggle.handler_unblock_by_func(
                    self._on_monitor_toggle)

        run_async(["sh", "-c", script], done, root=True, timeout=20)

    def _disable_monitor(self, iface: str) -> None:
        mon = _mon_iface(iface)
        self.mon_row.set_subtitle("Restoring %s to managed…" % iface)
        self.output.append("# disabling monitor on %s\n" % iface)
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
            toast(self.app_window, "Monitor mode off")

        run_async(["sh", "-c", script], done, root=True, timeout=15)

    # ---------------------------------------------------------- capture
    def _start(self) -> None:
        if not self._monitor_iface:
            toast(self.app_window, "Enable monitor mode first")
            return
        if self._proc is not None and self._proc.running:
            toast(self.app_window, "Capture already running")
            return

        # Fresh output file per session. Timestamp-named so multiple
        # runs don't clobber each other and the Loot table can list
        # them as separate rows.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._pcap_path = loot_path(
            "pmkid", "pmkid-%s.pcapng" % stamp)
        self._ap_map = {}
        self._pmkid_hits = 0
        self._hits_render()

        argv = [
            "hcxdumptool",
            "-i", self._monitor_iface,
            "-w", self._pcap_path,
            "--enable_status=1",  # print live status lines
        ]

        # Passive mode: disable everything active. The manpage covers
        # both flag spellings; hcxdumptool >=6.3 uses the "disable_"
        # form.
        if self.profile.get_selected() == 1:
            argv += [
                "--disable_deauthentication",
                "--disable_association",
                # Some builds also expose --disable_client_attacks
                # which is a superset. Include both defensively.
            ]

        # Band restriction. hcxdumptool sets channels with -c; we
        # give it plain-english groups here.
        band_sel = self.band.get_selected()
        if band_sel == 1:
            argv += ["-c", "1,2,3,4,5,6,7,8,9,10,11,12,13"]
        elif band_sel == 2:
            argv += ["-c", "36,40,44,48,52,56,60,64,100,104,108,"
                     "112,116,120,124,128,132,136,140,149,153,157,161,165"]

        if self.rds.get_active():
            argv += ["--rds=1"]

        self.output.append("$ " + " ".join(argv) + "\n")

        # Record in loot store *before* hcxdumptool actually starts;
        # even if the run aborts we still want the row so the user
        # can delete the empty pcap.
        target = "hcxdumptool " + (
            "passive" if self.profile.get_selected() == 1 else "active")
        self._loot_id = get_loot_store().record(
            module="pmkid", type="pmkid_pcap",
            target=target, path=self._pcap_path,
            notes="capture in progress")

        self._proc = Process(
            argv, self._on_line, self._on_done, root=True)
        self._proc.start()
        self._start_ts = time.time()
        self._set_state("running")
        self.file_row.set_subtitle(self._pcap_path)
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.iface_entry.set_sensitive(False)
        self._tick_id = GLib.timeout_add_seconds(2, self._tick)

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
        self.stop_btn.set_sensitive(False)
        self._set_state("stopping…")

    def _on_line(self, text: str) -> None:
        self.output.append(text)
        # Count PMKID / EAPOL hits. The hcxdumptool "status" lines
        # print a summary too but new-events lines are what we want.
        n = len(_LINE_PMKID.findall(text or ""))
        if n:
            self._pmkid_hits += n
        # Learn AP BSSIDs from any line that mentions one; keep the
        # SSID if we can parse it out.
        for line in (text or "").splitlines():
            m = _LINE_AP.search(line)
            if not m:
                continue
            bssid = m.group(1).lower()
            ssid = m.group(2)
            self._ap_map.setdefault(bssid, ssid)
        self._hits_render()

    def _hits_render(self) -> None:
        self.hits_row.set_subtitle(str(self._pmkid_hits))
        self.aps_row.set_subtitle(str(len(self._ap_map)))

    def _tick(self) -> bool:
        # Refresh loot row size so the file grows visibly in the UI.
        if self._loot_id is not None:
            get_loot_store().refresh_size(self._loot_id)
        if self._proc is None or not self._proc.running:
            return False
        elapsed = int(time.time() - self._start_ts)
        self._set_state("running (%dm %02ds elapsed)"
                         % (elapsed // 60, elapsed % 60))
        return True

    def _on_done(self, code: int) -> None:
        self.output.append("[hcxdumptool exited: %d]\n" % code)
        self._proc = None
        self.start_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        self.iface_entry.set_sensitive(True)
        elapsed = int(time.time() - self._start_ts)
        self._set_state("stopped after %dm %02ds"
                         % (elapsed // 60, elapsed % 60))

        # Finalise the loot row: pick up SSID list, refresh size,
        # rewrite notes with the summary.
        if self._loot_id is not None:
            store = get_loot_store()
            store.refresh_size(self._loot_id)
            aps = sorted(v or b for b, v in self._ap_map.items())[:5]
            note = "%d frames of interest across %d APs" % (
                self._pmkid_hits, len(self._ap_map))
            if aps:
                note += "; sampled: " + ", ".join(aps)
            get_loot_store().append_notes(self._loot_id, note)

    def _set_state(self, text: str) -> None:
        self.state_row.set_subtitle(text)

    def _open_loot(self) -> None:
        # Deep-link to the Loot screen, filtered to this module.
        try:
            self.app_window.activate_module("loot", "module:pmkid")
        except Exception:
            toast(self.app_window,
                  "Loot module not available")
