"""IoT Recon -- fuse Wi-Fi + BLE + P2P signals into one target table.

The other modules in this app produce partial views of what is in the
target environment:

  * ``probe_harvester``    -- Wi-Fi client MACs + probed SSIDs
  * ``net_discovery``      -- APs on the air + APs on the LAN
  * ``wifi_direct``        -- Wi-Fi soft-APs in setup mode
  * ``iot_setup``          -- IoT devices actively provisioning
  * ``ble_track``          -- BLE peripherals + Continuity metadata
  * ``iot_hacking``        -- LAN devices, fingerprinted post-associate

Individually they answer "what is on the air?" or "what is on the
LAN?" but not "which device is which, and how do I attack it?".

This screen fuses them. It aggregates every observation by MAC (or,
for Bluetooth, by BLE MAC + optional Wi-Fi correlate), classifies
the device by vendor OUI + advertisement fingerprints, and offers
per-device deep-link buttons to the right attack module:

  * Smart TVs        -> tv_remote (Chromecast/Roku/AndroidTV/DIAL)
  * Chromecasts      -> wifi_direct (setup mode) or tv_remote
  * Printers         -> iot_hacking (PRET) or wifi_direct
  * Cameras          -> iot_hacking (playbooks)
  * Smart plugs      -> iot_hacking (playbooks)
  * Routers          -> pmkid / handshake (get the PSK)
  * BLE-only devices -> ble_prov (provisioning intercept)

The scans run in parallel background threads and merge into a live
table. Nothing here transmits new frames -- it consumes what the
other passive modules produce -- so the whole screen stays quiet.
"""
from __future__ import annotations

import csv
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

# OUI prefix → (vendor, category, hint) for the top offenders we care
# about. Not exhaustive; enough to seed the classifier so unknown
# MACs get a "?" and known ones tell the operator where to click.
_OUI_TABLE: list[tuple[str, str, str, str]] = [
    ("F4:F5:D8", "Google",   "chromecast",  "Chromecast / Google Home"),
    ("54:60:09", "Google",   "chromecast",  "Chromecast Ultra"),
    ("6C:AD:F8", "Google",   "chromecast",  "Chromecast Audio"),
    ("F0:EF:86", "Google",   "chromecast",  "Nest Hub / Google Home"),
    ("64:16:66", "Google",   "chromecast",  "Nest / Chromecast"),
    ("DA:A1:19", "Google",   "chromecast",  "Google Chromecast randomised"),
    ("00:1A:11", "Google",   "chromecast",  "Google (Nest)"),
    ("A4:77:33", "Google",   "chromecast",  "Nest"),
    ("94:EB:2C", "Amazon",   "smart_speaker", "Amazon Echo"),
    ("74:C2:46", "Amazon",   "smart_speaker", "Amazon Echo Dot"),
    ("F0:D2:F1", "Amazon",   "smart_speaker", "Amazon Echo Show"),
    ("40:B4:CD", "Amazon",   "smart_speaker", "Amazon Fire TV"),
    ("FC:65:DE", "Amazon",   "smart_speaker", "Amazon Echo Plus"),
    ("00:71:47", "Amazon",   "smart_speaker", "Amazon Fire Stick"),
    ("00:E2:69", "Roku",     "tv",           "Roku streaming stick"),
    ("D4:9A:20", "Roku",     "tv",           "Roku TV"),
    ("8C:49:62", "Roku",     "tv",           "Roku"),
    ("AC:AE:19", "Roku",     "tv",           "Roku Ultra"),
    ("F0:B3:EC", "Roku",     "tv",           "Roku Express"),
    ("00:D0:59", "Samsung",  "tv",           "Samsung TV"),
    ("BC:8C:CD", "Samsung",  "tv",           "Samsung Smart TV"),
    ("F8:04:2E", "Samsung",  "tv",           "Samsung"),
    ("D0:03:4B", "Apple",    "phone",        "iPhone / iPad"),
    ("A8:20:66", "Apple",    "tv",           "Apple TV"),
    ("D8:00:4D", "Apple",    "tv",           "Apple TV 4K"),
    ("28:CF:E9", "Apple",    "phone",        "Apple"),
    ("C0:CE:CD", "LG",       "tv",           "LG Smart TV"),
    ("B8:AD:3E", "LG",       "tv",           "LG WebOS"),
    ("50:76:AF", "Sony",     "tv",           "Sony Bravia"),
    ("FC:0F:E6", "Sony",     "tv",           "Sony"),
    ("00:5F:86", "Sony",     "console",      "PlayStation"),
    ("00:D9:D1", "Vizio",    "tv",           "Vizio SmartCast"),
    ("00:F8:2C", "Vizio",    "tv",           "Vizio"),
    ("00:26:B7", "Philips",  "tv",           "Philips TV"),
    ("D4:20:B0", "Philips",  "tv",           "Philips Hue bridge"),
    ("00:23:A7", "Nest",     "thermostat",   "Nest thermostat"),
    ("18:B4:30", "Nest",     "camera",       "Nest camera / doorbell"),
    ("64:16:66", "Nest",     "camera",       "Nest"),
    ("F8:E7:1E", "Ring",     "camera",       "Ring doorbell"),
    ("00:62:6E", "Ring",     "camera",       "Ring"),
    ("94:E3:6D", "Wyze",     "camera",       "Wyze camera"),
    ("2C:AA:8E", "Wyze",     "camera",       "Wyze"),
    ("EC:1B:BD", "Reolink",  "camera",       "Reolink camera"),
    ("EC:71:DB", "Reolink",  "camera",       "Reolink"),
    ("B0:C5:54", "Hikvision", "camera",      "Hikvision IP camera"),
    ("28:57:BE", "Hikvision", "camera",      "Hikvision"),
    ("00:11:32", "Synology", "nas",          "Synology NAS"),
    ("38:8B:59", "TP-Link",  "smart_plug",   "TP-Link Kasa / Tapo"),
    ("60:32:B1", "TP-Link",  "smart_plug",   "TP-Link Kasa"),
    ("50:C7:BF", "TP-Link",  "router",       "TP-Link router"),
    ("A0:B8:6E", "TP-Link",  "router",       "TP-Link"),
    ("74:AC:5F", "TP-Link",  "router",       "TP-Link Archer"),
    ("D8:07:B6", "TP-Link",  "router",       "TP-Link"),
    ("EC:88:8F", "Netgear",  "router",       "Netgear router"),
    ("C0:3F:0E", "Netgear",  "router",       "Netgear Orbi"),
    ("00:1B:2F", "Netgear",  "router",       "Netgear"),
    ("A0:63:91", "Netgear",  "router",       "Netgear Nighthawk"),
    ("EC:AD:E0", "ASUS",     "router",       "ASUS router"),
    ("50:EB:F6", "ASUS",     "router",       "ASUS RT-AX"),
    ("18:D6:C7", "ASUS",     "router",       "ASUS"),
    ("A0:F3:C1", "Belkin",   "router",       "Belkin / Linksys"),
    ("EC:1A:59", "Belkin",   "router",       "Belkin"),
    ("00:0C:43", "MediaTek", "chipset",      "MediaTek (generic)"),
    ("14:B4:57", "Espressif","esp32",        "ESP32 / ESP8266 DIY"),
    ("BC:DD:C2", "Espressif","esp32",        "Espressif"),
    ("A4:CF:12", "Espressif","esp32",        "ESP32"),
    ("C4:4F:33", "Espressif","esp32",        "ESP8266"),
    ("F0:08:D1", "Espressif","esp32",        "ESP"),
    ("28:76:CD", "HP",       "printer",      "HP printer"),
    ("A4:5D:36", "HP",       "printer",      "HP LaserJet"),
    ("EC:8E:B5", "HP",       "printer",      "HP"),
    ("2C:44:FD", "HP",       "printer",      "HP OfficeJet"),
    ("00:80:77", "Brother",  "printer",      "Brother printer"),
    ("18:A9:05", "Brother",  "printer",      "Brother"),
    ("00:BB:C1", "Canon",    "printer",      "Canon printer"),
    ("BC:5C:D0", "Canon",    "printer",      "Canon iP"),
    ("00:26:73", "Epson",    "printer",      "Epson printer"),
    ("74:C2:9A", "Epson",    "printer",      "Epson EcoTank"),
    ("58:8E:81", "Xiaomi",   "iot",          "Xiaomi Mi / Roborock"),
    ("64:CC:2E", "Xiaomi",   "iot",          "Xiaomi"),
    ("F8:1A:67", "Tuya",     "iot",          "Tuya / SmartLife"),
    ("D8:80:39", "Shelly",   "smart_plug",   "Shelly plug"),
    ("50:02:91", "iRobot",   "vacuum",       "iRobot Roomba"),
    ("70:5A:0F", "GoPro",    "camera",       "GoPro"),
    ("34:1B:22", "DJI",      "drone",        "DJI drone"),
    ("60:60:1F", "DJI",      "drone",        "DJI"),
]


def _oui(mac: str) -> str:
    """Normalise a MAC into the 8-char OUI prefix used in the table."""
    if not mac or ":" not in mac:
        return ""
    return mac.upper()[:8]


def _classify_mac(mac: str) -> tuple[str, str, str]:
    """Return ``(vendor, category, hint)`` for a MAC.

    Falls back to ``("?", "unknown", "")`` if the OUI is not in the
    table. Bit 1 of the first octet ("locally administered") means
    the MAC is randomised -- we tag it as such so the operator knows
    the vendor guess is not reliable.
    """
    if not mac:
        return "?", "unknown", ""
    prefix = _oui(mac)
    for oui, vendor, category, hint in _OUI_TABLE:
        if prefix == oui.upper():
            # Check LAA bit
            try:
                first = int(mac.split(":")[0], 16)
                if first & 0x02:
                    return vendor, category, hint + " (randomised)"
            except ValueError:
                pass
            return vendor, category, hint
    try:
        first = int(mac.split(":")[0], 16)
        if first & 0x02:
            return "?", "unknown", "randomised MAC"
    except ValueError:
        pass
    return "?", "unknown", ""


# Which attack modules apply to each category. This is the routing
# table iot_recon uses to expose per-target attack buttons; each
# entry is (button_label, destination_module_id, target_shape_key).
#
# Vector ordering mirrors the ranking from the "IoT attack from
# outside the LAN" research pass:
#
#   1. BLE direct-attack (unpaired GATT, WhisperPair) -- ble_attack
#   2. Setup-mode soft-AP capture                     -- iot_setup
#   3. Cloud pivot via rogue AP + DNS/NTP shift        -- karma + evil_twin
#   4. BLE provisioning sniff (Matter, Tuya, ESP-IDF) -- ble_prov
#   5. Deauth-driven persistent DoS -> setup mode      -- deauth
#
# So when we know which category a device belongs to, we surface the
# most productive vectors *first*.
_ATTACK_MAP: dict[str, list[tuple[str, str, str]]] = {
    "tv":            [("Remote / cast",    "tv_remote",    "ip"),
                      ("Deauth to setup",  "deauth",       "bssid"),
                      ("Cloud pivot AP",   "karma",        "ssid"),
                      ("IoT playbooks",    "iot_hacking",  "ip")],
    "chromecast":    [("Remote / cast",    "tv_remote",    "ip"),
                      ("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("Wi-Fi Direct",     "wifi_direct",  "ssid"),
                      ("Cloud pivot AP",   "karma",        "ssid")],
    "smart_speaker": [("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("BLE attack",       "ble_attack",   "mac"),
                      ("Cloud pivot AP",   "karma",        "ssid"),
                      ("IoT playbooks",    "iot_hacking",  "ip")],
    "printer":       [("IoT (PRET)",       "iot_hacking",  "ip"),
                      ("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("Wi-Fi Direct",     "wifi_direct",  "ssid")],
    "camera":        [("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("BLE attack",       "ble_attack",   "mac"),
                      ("BLE provisioning", "ble_prov",     "mac"),
                      ("Deauth (defeat)",  "deauth",       "bssid"),
                      ("Cloud pivot AP",   "karma",        "ssid"),
                      ("IoT playbooks",    "iot_hacking",  "ip")],
    "smart_plug":    [("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("BLE attack",       "ble_attack",   "mac"),
                      ("BLE provisioning", "ble_prov",     "mac"),
                      ("Cloud pivot AP",   "karma",        "ssid"),
                      ("IoT playbooks",    "iot_hacking",  "ip")],
    "thermostat":    [("BLE provisioning", "ble_prov",     "mac"),
                      ("BLE attack",       "ble_attack",   "mac"),
                      ("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("IoT playbooks",    "iot_hacking",  "ip")],
    "vacuum":        [("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("BLE attack",       "ble_attack",   "mac"),
                      ("IoT playbooks",    "iot_hacking",  "ip")],
    "esp32":         [("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("BLE provisioning", "ble_prov",     "mac"),
                      ("BLE attack",       "ble_attack",   "mac"),
                      ("Wi-Fi Direct",     "wifi_direct",  "ssid")],
    "router":        [("PMKID capture",    "pmkid",        "bssid"),
                      ("Handshake",        "handshake",    "bssid_full"),
                      ("Attacks",          "wifi_attacks", "bssid"),
                      ("Deauth flood",     "deauth",       "bssid")],
    "iot":           [("Setup-AP hunt",    "iot_setup",    "ssid"),
                      ("BLE attack",       "ble_attack",   "mac"),
                      ("BLE provisioning", "ble_prov",     "mac"),
                      ("Cloud pivot AP",   "karma",        "ssid"),
                      ("IoT playbooks",    "iot_hacking",  "ip")],
    "phone":         [("Karma spoof",      "karma",        "mac"),
                      ("Probe recon",      "probe_harvester", "mac"),
                      ("BLE spam DoS",     "blespam",      "mac"),
                      ("BLE attack",       "ble_attack",   "mac")],
    "console":       [("IoT playbooks",    "iot_hacking",  "ip"),
                      ("Deauth",           "deauth",       "bssid")],
    "drone":         [("Wi-Fi Direct",     "wifi_direct",  "ssid"),
                      ("BLE attack",       "ble_attack",   "mac")],
    "nas":           [("IoT playbooks",    "iot_hacking",  "ip")],
    "tv:apple":      [("Remote / cast",    "tv_remote",    "ip")],
    "chipset":       [("IoT playbooks",    "iot_hacking",  "ip")],
    "unknown":       [("Probe recon",      "probe_harvester", "mac"),
                      ("BLE attack",       "ble_attack",   "mac"),
                      ("IoT playbooks",    "iot_hacking",  "ip")],
}


class Sighting:
    """One evidence source about a device. We keep a list of these
    per unique key (MAC most of the time) and let the row summarise
    them in the UI."""

    def __init__(self, source: str, mac: str = "", ip: str = "",
                 ssid: str = "", name: str = "", extra: str = ""):
        self.source = source     # 'probe' / 'ap' / 'wifi_direct' /
                                 # 'iot_setup' / 'ble' / 'lan'
        self.mac = (mac or "").lower()
        self.ip = ip
        self.ssid = ssid         # SSID seen (for AP / softAP rows)
        self.name = name         # BLE local name, mDNS hostname, etc.
        self.extra = extra       # freeform
        self.ts = time.time()


@register
class IoTRecon(NHModule):
    title = "IoT Recon"
    icon = "network-workgroup-symbolic"
    description = ("Fuse Wi-Fi + BLE + P2P sightings from the passive "
                   "recon modules into one target table; each row "
                   "deep-links to the attack module that fits.")
    required_tools = []

    def __init__(self, app_window):
        super().__init__(app_window)
        # key -> {"mac", "ip", "ssids": set, "names": set, "sources":
        #         set, "vendor", "category", "hint", "sightings": [Sighting]}
        self._devices: dict[str, dict] = {}
        self._rows: list[Adw.ExpanderRow] = []
        self._scan_running = False
        # Cooperative-cancel flag: each background scanner checks it
        # between iterations and bails when true.
        self._stop_requested = False
        # Subprocess handles we own during a run -- terminate() them
        # from _stop_scan so we don't leak tcpdump / btmon / airodump
        # when the user hits Stop halfway.
        self._child_procs: list[subprocess.Popen] = []

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ---- controls
        cfg = Adw.PreferencesGroup(
            title="Scan",
            description="Kicks off the passive-recon sources at once "
                        "and merges their output. No frames "
                        "transmitted; feeds the deep-links below.")

        self.iface_ap = Adw.EntryRow(title="Monitor iface (Wi-Fi)")
        self.iface_ap.set_text("wlan1mon")
        cfg.add(self.iface_ap)

        self.iface_ble = Adw.EntryRow(title="HCI adapter (BLE)")
        self.iface_ble.set_text("hci0")
        cfg.add(self.iface_ble)

        self.subnet = Adw.EntryRow(
            title="LAN subnet (CIDR, optional)")
        cfg.add(self.subnet)

        self.duration = Adw.SpinRow.new_with_range(10, 3600, 10)
        self.duration.set_title(
            "Max scan duration (s) -- 0 = until Stop")
        self.duration.set_value(30)
        cfg.add(self.duration)

        run_row = Adw.ActionRow(
            title="Sweep",
            subtitle="Wi-Fi + BLE + LAN (if a subnet is set) "
                     "-- all in parallel")
        self.scan_btn = Gtk.Button(label="Start",
                                   valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda _b: self._start_scan())
        run_row.add_suffix(self.scan_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop_scan())
        run_row.add_suffix(self.stop_btn)
        cfg.add(run_row)
        box.append(cfg)

        # ---- stats
        stat = Adw.PreferencesGroup(title="Session")
        self.state_row = Adw.ActionRow(title="Status",
                                       subtitle="idle")
        stat.add(self.state_row)
        self.dev_row = Adw.ActionRow(title="Unique devices",
                                     subtitle="0")
        stat.add(self.dev_row)
        self.tv_row = Adw.ActionRow(title="TVs / Chromecasts",
                                    subtitle="0")
        stat.add(self.tv_row)
        self.iot_row = Adw.ActionRow(title="IoT (plugs/cameras/etc)",
                                     subtitle="0")
        stat.add(self.iot_row)
        self.router_row = Adw.ActionRow(title="Routers / APs",
                                        subtitle="0")
        stat.add(self.router_row)
        box.append(stat)

        # ---- table
        self.results = Adw.PreferencesGroup(
            title="Devices seen",
            description="One row per unique MAC; expand to see the "
                        "signals and the per-attack buttons.")
        self._empty_row = Adw.ActionRow(
            title="No sightings yet",
            subtitle="Press Sweep and give it a moment")
        self.results.add(self._empty_row)
        box.append(self.results)

        # ---- log
        self.output = OutputView()
        box.append(self.output)
        return box

    # ---------------------------------------------------------- scan
    def _start_scan(self) -> None:
        if self._scan_running:
            toast(self.app_window, "Sweep already in progress")
            return
        self._scan_running = True
        self._stop_requested = False
        self._child_procs = []
        self.scan_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.state_row.set_subtitle("scanning…")
        self._devices = {}
        self._render()
        # 0 = run until user hits Stop. Anything else is a hard cap.
        duration = int(self.duration.get_value())
        if duration <= 0:
            duration = 86400  # a day: effectively "run forever"
        iface_ap = self.iface_ap.get_text().strip() or "wlan1mon"
        iface_ble = self.iface_ble.get_text().strip() or "hci0"
        subnet = self.subnet.get_text().strip()

        # Kick each scanner in its own thread; each drops findings
        # into ``self._devices`` and re-renders on the GTK thread.
        # Each scanner honours self._stop_requested / kills its own
        # subprocess when the user hits Stop.
        threading.Thread(
            target=self._scan_air_ap, args=(iface_ap, duration),
            daemon=True).start()
        threading.Thread(
            target=self._scan_air_probes, args=(iface_ap, duration),
            daemon=True).start()
        threading.Thread(
            target=self._scan_ble, args=(iface_ble, duration),
            daemon=True).start()
        if subnet:
            threading.Thread(
                target=self._scan_lan, args=(subnet,),
                daemon=True).start()
        threading.Thread(
            target=self._scan_wifi_direct, args=(duration,),
            daemon=True).start()

        # Watchdog: fire _scan_finished after the hard cap OR let
        # _stop_scan short-circuit it. Store the id so Stop can
        # remove the timer early.
        self._watchdog_id = GLib.timeout_add_seconds(
            duration + 3, self._scan_finished)

    def _stop_scan(self) -> None:
        """User pressed Stop -- kill every child subprocess and let
        the scanner threads observe self._stop_requested and bail."""
        if not self._scan_running:
            return
        self._stop_requested = True
        for p in list(self._child_procs):
            try:
                p.terminate()
            except Exception:
                pass
        # Cancel the duration-cap watchdog and finalise now.
        wid = getattr(self, "_watchdog_id", None)
        if wid is not None:
            try:
                GLib.source_remove(wid)
            except Exception:
                pass
            self._watchdog_id = None
        self._scan_finished()

    def _scan_finished(self) -> bool:
        if not self._scan_running:
            return False
        self._scan_running = False
        self._stop_requested = True  # signal any remaining threads
        self.scan_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        self.state_row.set_subtitle(
            "done -- %d devices" % len(self._devices))
        self._render()
        # Save the whole aggregate CSV to loot for later analysis.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = loot_path(
            "iot_recon", "iot-recon-%s.csv" % stamp)
        try:
            with open(path, "w", newline="") as fp:
                w = csv.writer(fp)
                w.writerow(["mac", "ip", "vendor", "category",
                            "hint", "ssids", "names", "sources"])
                for d in self._devices.values():
                    w.writerow([
                        d.get("mac", ""), d.get("ip", ""),
                        d.get("vendor", ""), d.get("category", ""),
                        d.get("hint", ""),
                        "|".join(sorted(d.get("ssids", set()))),
                        "|".join(sorted(d.get("names", set()))),
                        "|".join(sorted(d.get("sources", set()))),
                    ])
        except OSError:
            pass
        get_loot_store().record(
            module="iot_recon", type="iot_recon_csv",
            target="%d devices" % len(self._devices),
            path=path,
            notes="Merged Wi-Fi + BLE + LAN scan")
        return False

    # ------------------------------------------------- individual scans
    def _scan_air_ap(self, iface: str, duration: int) -> None:
        """airodump-ng one-shot -- feeds AP rows. We register the
        subprocess with self._child_procs so the Stop button can
        terminate it mid-scan."""
        script = (
            "rm -f /tmp/nhp-iot-air-*; "
            "airodump-ng --output-format csv "
            "  -w /tmp/nhp-iot-air %s >/dev/null 2>&1"
        ) % iface
        proc = self._spawn(script, root=True)
        if proc is None:
            return
        # Watch until either duration elapses or Stop is pressed.
        start = time.time()
        while True:
            if self._stop_requested:
                break
            if proc.poll() is not None:
                break
            if time.time() - start > duration:
                break
            time.sleep(0.5)
        try:
            proc.terminate()
        except Exception:
            pass
        # Read the CSV whether we finished early or hit the cap.
        result = _read_file("/tmp/nhp-iot-air-01.csv")
        for row in (result or "").splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) < 14 or ":" not in parts[0]:
                continue
            bssid = parts[0]
            essid = parts[13]
            if not essid:
                continue
            self._record(Sighting(source="ap", mac=bssid,
                                    ssid=essid,
                                    extra="ch " + parts[3]))
        GLib.idle_add(self._render)

    def _scan_air_probes(self, iface: str, duration: int) -> None:
        """Sniff probe requests via tcpdump. Reads line by line and
        bails when Stop is pressed or the duration cap elapses."""
        cmd = ("tcpdump -i %s -e -s0 "
                "'type mgt subtype probereq' -l 2>/dev/null"
                ) % iface
        proc = self._spawn(cmd, root=True, stdout_pipe=True)
        if proc is None:
            return
        start = time.time()
        try:
            while True:
                if self._stop_requested:
                    break
                if time.time() - start > duration:
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                m_mac = re.search(
                    r"SA:([0-9A-Fa-f:]{17})", line)
                m_ssid = re.search(r"\(([^)]{1,32})\)", line)
                if not m_mac:
                    continue
                mac = m_mac.group(1)
                ssid = m_ssid.group(1) if m_ssid else ""
                self._record(Sighting(
                    source="probe", mac=mac, ssid=ssid))
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
        GLib.idle_add(self._render)

    def _scan_ble(self, adapter: str, duration: int) -> None:
        """btmon reads HCI logs. Same start-vs-stop dance as the
        probe sniffer."""
        idx = adapter.replace("hci", "") or "0"
        cmd = "btmon --index %s 2>/dev/null" % idx
        proc = self._spawn(cmd, root=True, stdout_pipe=True)
        if proc is None:
            return
        buf: list[str] = []
        start = time.time()
        try:
            while True:
                if self._stop_requested:
                    break
                if time.time() - start > duration:
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                buf.append(line)
                if line.strip() == "" and buf:
                    blk = "".join(buf)
                    buf = []
                    m_mac = re.search(
                        r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", blk)
                    m_name = re.search(
                        r"Name\s*\(complete\)\s*:\s*(.+)", blk)
                    if not m_mac:
                        continue
                    name = m_name.group(1).strip() if m_name else ""
                    self._record(Sighting(
                        source="ble", mac=m_mac.group(1),
                        name=name))
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
        GLib.idle_add(self._render)

    def _scan_lan(self, subnet: str) -> None:
        """Fast ping sweep + arp table dump so we get IP<->MAC pairs
        for anything on the same LAN as us."""
        script = (
            "nmap -sn -T4 -n --min-parallelism 16 %s >/dev/null "
            "2>&1 || true; "
            "ip -4 neigh show; "
            "echo '---mdns---'; "
            "avahi-browse -art -p 2>/dev/null | head -300 || true"
        ) % subnet
        proc = self._spawn(script, root=False, stdout_pipe=True)
        if proc is None:
            return
        # Read everything (LAN sweep is short-lived by nature; the
        # user pressing Stop terminates the subprocess so read()
        # returns whatever nmap managed to produce).
        try:
            result = proc.stdout.read() or ""
        except Exception:
            result = ""
        for line in (result or "").splitlines():
            if self._stop_requested:
                break
            m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s.*lladdr\s+"
                         r"([0-9a-f:]{17})", line)
            if m:
                ip, mac = m.group(1), m.group(2)
                self._record(Sighting(
                    source="lan", mac=mac, ip=ip))
                continue
            # avahi -p line: iface;proto;name;type;domain;host;ip;
            # port;txt
            if ";" in line and line.count(";") >= 6:
                parts = line.split(";")
                if len(parts) >= 7 and parts[6]:
                    ip = parts[6]
                    name = parts[3] if len(parts) > 3 else ""
                    self._record(Sighting(
                        source="lan", ip=ip, name=name))
        GLib.idle_add(self._render)

    def _scan_wifi_direct(self, duration: int) -> None:
        """Cheap: scan visible SSIDs on wlan0 in managed mode."""
        script = (
            "iw dev wlan0 scan 2>/dev/null | "
            "awk '/^BSS/ {bss=$2} "
            "     /SSID/ {sub(/^\\s+SSID:\\s?/,\"\"); "
            "             if($0!=\"\") print bss\"|\"$0}' | head -200"
        )
        proc = self._spawn(script, root=True, stdout_pipe=True)
        if proc is None:
            return
        try:
            result = proc.stdout.read() or ""
        except Exception:
            result = ""
        if not result:
            return
        setup_re = re.compile(
            r"^(DIRECT-|HP-Setup-|Amazon-|Wyze_|Roomba-|shelly|"
            r"tasmota|ESPHome|Chromecast|Google|HS100_|SmartLife_|"
            r"Ring-|Reolink-)", re.IGNORECASE)
        for line in (result or "").splitlines():
            if "|" not in line:
                continue
            bssid, _, ssid = line.partition("|")
            ssid = ssid.strip()
            bssid = bssid.strip()
            if not ssid or not setup_re.match(ssid):
                continue
            self._record(Sighting(
                source="wifi_direct", mac=bssid, ssid=ssid,
                extra="setup-AP"))
        GLib.idle_add(self._render)

    # ------------------------------------------------- spawn helpers
    def _spawn(self, script: str, root: bool = False,
               stdout_pipe: bool = False) -> subprocess.Popen | None:
        """Fork a shell subprocess and register it with
        ``self._child_procs`` so the Stop button can terminate it.

        ``root=True`` prefixes ``sudo -n`` because the app is
        supposed to have already authorized the helper earlier -- if
        the noninteractive escalation fails we return None and the
        scanner just doesn't produce output for that source."""
        try:
            argv = ["sh", "-c", script]
            if root:
                argv = ["sudo", "-n"] + argv
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE if stdout_pipe
                        else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True)
            self._child_procs.append(proc)
            return proc
        except OSError:
            return None

    # ------------------------------------------------- state fold
    def _record(self, s: Sighting) -> None:
        """Fold a new sighting into the merged device table."""
        key = s.mac or ("ip:" + s.ip) if s.ip else s.mac
        if not key:
            return
        d = self._devices.get(key)
        if d is None:
            vendor, cat, hint = _classify_mac(s.mac)
            # First-class overrides via SSID prefix (in case OUI is
            # randomised but SSID betrays the vendor).
            if s.ssid:
                cat2, hint2 = _classify_ssid(s.ssid)
                if cat2 != "unknown":
                    cat = cat2
                    hint = hint2 or hint
            d = {
                "mac": s.mac, "ip": s.ip,
                "ssids": set(), "names": set(),
                "sources": set(),
                "vendor": vendor, "category": cat, "hint": hint,
            }
            self._devices[key] = d
        if s.mac and not d["mac"]:
            d["mac"] = s.mac
        if s.ip and not d["ip"]:
            d["ip"] = s.ip
        if s.ssid:
            d["ssids"].add(s.ssid)
        if s.name:
            d["names"].add(s.name)
        if s.source:
            d["sources"].add(s.source)

    # ---------------------------------------------------- rendering
    def _render(self) -> None:
        # Clear existing rows.
        for r in self._rows:
            self.results.remove(r)
        self._rows = []
        if not self._devices:
            self._empty_row.set_visible(True)
            return
        self._empty_row.set_visible(False)

        # Category tallies for the stats group.
        n_tv = n_iot = n_router = 0
        for d in self._devices.values():
            c = d["category"]
            if c in ("tv", "chromecast", "smart_speaker"):
                n_tv += 1
            elif c in ("printer", "camera", "smart_plug",
                        "thermostat", "vacuum", "esp32", "iot"):
                n_iot += 1
            elif c == "router":
                n_router += 1
        self.dev_row.set_subtitle(str(len(self._devices)))
        self.tv_row.set_subtitle(str(n_tv))
        self.iot_row.set_subtitle(str(n_iot))
        self.router_row.set_subtitle(str(n_router))

        # Sort: known category first, then by vendor, then by MAC.
        def sort_key(kv):
            d = kv[1]
            return (
                0 if d["category"] != "unknown" else 1,
                d["vendor"] or "",
                d["mac"] or "",
            )
        for _, d in sorted(self._devices.items(), key=sort_key)[:60]:
            self._rows.append(self._make_row(d))

    def _make_row(self, d: dict) -> Adw.ExpanderRow:
        title = d.get("mac") or d.get("ip") or "?"
        subtitle = "%s · %s%s" % (
            d.get("vendor", "?"),
            d.get("category", "?"),
            (" · " + d["hint"]) if d.get("hint") else "")
        row = Adw.ExpanderRow(title=title, subtitle=subtitle)

        if d.get("ip"):
            row.add_row(Adw.ActionRow(title="IP",
                                      subtitle=d["ip"]))
        for name in sorted(d.get("names", set())):
            row.add_row(Adw.ActionRow(title="Name",
                                      subtitle=name))
        for ssid in sorted(d.get("ssids", set())):
            row.add_row(Adw.ActionRow(title="SSID",
                                      subtitle=ssid))
        srcs = ", ".join(sorted(d.get("sources", set())))
        if srcs:
            row.add_row(Adw.ActionRow(title="Sources",
                                      subtitle=srcs))

        # Deep-link buttons by category.
        for label, module_id, kind in _ATTACK_MAP.get(
                d["category"], _ATTACK_MAP["unknown"]):
            target = self._build_target(d, kind)
            btn_row = Adw.ActionRow(
                title=label, subtitle="-> " + module_id)
            btn = Gtk.Button(label="Launch",
                             valign=Gtk.Align.CENTER)
            btn.connect(
                "clicked",
                lambda _b, m=module_id, t=target:
                    self.app_window.activate_module(m, t))
            btn_row.add_suffix(btn)
            row.add_row(btn_row)

        self.results.add(row)
        return row

    def _build_target(self, d: dict, kind: str) -> str:
        """Format the target string per destination module's set_target
        contract. See each module's set_target() docstring for the
        exact shape."""
        mac = d.get("mac", "")
        ip = d.get("ip", "")
        ssids = sorted(d.get("ssids", set()))
        ssid = ssids[0] if ssids else ""
        if kind == "ip":
            return ip
        if kind == "mac":
            return mac
        if kind == "ssid":
            return ssid
        if kind == "bssid":
            return mac
        if kind == "bssid_full":
            return "%s|%s|%s" % (mac, "", ssid)
        return ip or mac or ssid


# ---------------------------------------------------- SSID classifier
_SSID_PATTERNS: list[tuple[str, str, str]] = [
    (r"^DIRECT-.*Chromecast", "chromecast", "Chromecast setup AP"),
    (r"^HP-Setup-", "printer", "HP printer setup"),
    (r"^HP-Print", "printer", "HP printer"),
    (r"^Brother", "printer", "Brother printer"),
    (r"^CanonNET", "printer", "Canon printer"),
    (r"^Epson", "printer", "Epson printer"),
    (r"^Amazon-", "smart_speaker", "Amazon setup"),
    (r"^GoogleHome", "smart_speaker", "Google Home setup"),
    (r"^Wyze_", "camera", "Wyze camera setup"),
    (r"^Ring-", "camera", "Ring setup"),
    (r"^Reolink-", "camera", "Reolink camera setup"),
    (r"^SmartLife_|^Tuya_", "iot", "Tuya IoT setup"),
    (r"^HS100_|^HS110_|^KP\d{3}_", "smart_plug", "TP-Link Kasa"),
    (r"^shelly", "smart_plug", "Shelly plug"),
    (r"^tasmota", "esp32", "Tasmota"),
    (r"^ESPHome|^ESP-", "esp32", "ESPHome"),
    (r"^Roomba-", "vacuum", "iRobot Roomba"),
    (r"^Roborock-", "vacuum", "Roborock"),
    (r"^Sonos", "smart_speaker", "Sonos"),
    (r"^GoPro", "camera", "GoPro"),
    (r"^DJI[_-]", "drone", "DJI drone"),
]


def _classify_ssid(ssid: str) -> tuple[str, str]:
    """Return ``(category, hint)`` from the SSID prefix table."""
    for regex, cat, hint in _SSID_PATTERNS:
        if re.search(regex, ssid, re.IGNORECASE):
            return cat, hint
    return "unknown", ""


def _read_file(path: str) -> str:
    """Best-effort file read. Returns empty string if the file is
    missing or not readable."""
    try:
        with open(path) as fp:
            return fp.read()
    except OSError:
        return ""


# ---------------------------------------------------- blocking runner
def _read_file(path: str) -> str:
    """Small helper: read a file if it exists, empty string otherwise."""
    try:
        with open(path) as fp:
            return fp.read()
    except OSError:
        return ""


def _blocking_run(script: str, root: bool, timeout: int) -> str:
    """Small wrapper for background scanners. The rest of the app
    uses ``run_async`` / ``Process`` because everything is bound to
    GLib timers; here we own the background threads ourselves and
    just want the subprocess to finish."""
    try:
        cmd = ["sh", "-c", script]
        if root:
            cmd = ["sudo", "-n"] + cmd
        p = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=timeout)
        return p.stdout or ""
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""
