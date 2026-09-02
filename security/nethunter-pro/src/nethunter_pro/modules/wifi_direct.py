"""Wi-Fi Direct / P2P recon and hijack.

Wi-Fi Direct is a Wi-Fi Alliance protocol where devices act as their
own soft-APs (Group Owners) and pair peer-to-peer without touching an
existing SSID. Real-world exposures:

* **Miracast sinks** -- old smart TV dongles accept PBC pairings
  automatically. If a Miracast display sits in the office/lobby you
  can cast to it without credentials.
* **Chromecast in setup mode** -- factory reset or first boot exposes
  a ``DIRECT-XX-Chromecast`` soft-AP with the Cast setup HTTPS API
  and no client-side authentication.
* **Printers with "Wi-Fi Direct"** -- HP, Brother, Canon typically
  open a ``DIRECT-*`` SSID with WPS-PBC as the only auth. Once
  associated we get IPP/PJL over :9100 and a web admin.
* **Action cameras** (GoPro / DJI / Sony Alpha) -- soft-AP with a
  known-vendor default PSK, RTSP or vendor RPC in the clear.

The screen scans with ``wpa_supplicant -Bs`` + ``iw p2p find`` and
lists everything, tagged by category. From a row you can associate
directly to the target soft-AP (if we can guess the auth) or hand
off to another module -- IoT Hacking's PRET harness for printers,
Bettercap for cameras.
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

DEFAULT_IFACE = "wlan1"

# Categorisation table: SSID prefix regex -> (category, hint).
# Ordered longest-prefix-first so 'DIRECT-*Chromecast' beats
# a bare 'DIRECT-*'.
_CATEGORIES: list[tuple[str, str, str]] = [
    (r"^DIRECT-[A-Z0-9]{2}-Chromecast", "Chromecast",
     "Chromecast setup AP: open HTTPS setup on 8443"),
    (r"^DIRECT-\S*-Setup", "IoT setup", "Generic Wi-Fi Direct setup"),
    (r"^HP-Setup-", "HP printer setup",
     "HP printer setup AP; IPP :631"),
    (r"^HP-Print-", "HP printer",
     "HP printer P2P AP; try PRET"),
    (r"^Brother\s", "Brother printer",
     "Brother printer P2P; SNMP + web admin"),
    (r"^CanonNET", "Canon printer",
     "Canon printer P2P"),
    (r"^ESP[_-]?\w+", "ESP32/8266",
     "Generic ESPHome/Tasmota AP; usually open, HTTP admin"),
    (r"^tasmota", "Tasmota", "Tasmota open AP; captive setup page"),
    (r"^ESPHome", "ESPHome", "ESPHome captive setup"),
    (r"^HS100_", "TP-Link Kasa", "Kasa smart-plug AP; HTTP raw"),
    (r"^SmartLife_", "Tuya", "Tuya BLE+Wi-Fi hybrid; sometimes P2P"),
    (r"^shelly", "Shelly", "Shelly device open AP"),
    (r"^Roomba-", "Roomba", "iRobot Roomba open AP"),
    (r"^Wyze_", "Wyze camera", "Wyze setup AP"),
    (r"^GoPro", "GoPro camera",
     "GoPro AP; usually known-default PSK; RTSP/RTP"),
    (r"^DJI[_-]", "DJI drone/camera",
     "DJI drone/camera AP; vendor RPC"),
    (r"^ILCE|^HDR", "Sony Alpha",
     "Sony camera Wi-Fi; HTTP + FTP"),
    (r"^Sonos", "Sonos", "Sonos setup"),
    (r"^Nest-", "Nest", "Nest thermostat/camera setup"),
    (r"^Amazon-", "Amazon Echo",
     "Amazon device setup; HTTP local"),
    (r"^GoogleHome", "Google Home", "Google Home setup"),
    (r"^ring-", "Ring", "Ring doorbell/camera setup"),
    (r"^Reolink-", "Reolink camera", "Reolink camera setup"),
    (r"^DIRECT-", "Wi-Fi Direct",
     "Generic P2P; Miracast / phone-to-phone"),
]


def _classify(ssid: str) -> tuple[str, str]:
    """Return ``(category, hint)`` from the SSID prefix table."""
    for regex, cat, hint in _CATEGORIES:
        if re.search(regex, ssid, re.IGNORECASE):
            return cat, hint
    return "Unknown", ""


@register
class WifiDirect(NHModule):
    title = "Wi-Fi Direct / P2P"
    icon = "network-wireless-hotspot-symbolic"
    description = ("Discover Miracast / printers / cameras / IoT "
                   "soft-APs and pivot into them without the target "
                   "LAN's credentials.")
    # We drive the scan with wpa_cli (from wpasupplicant) and iw. Both
    # are always present on Kali.
    required_tools = ["wpa_cli", "iw"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._aps: dict[str, dict] = {}
        self._ap_rows: list[Adw.ExpanderRow] = []
        # Timer id for the continuous sweep loop; None when idle.
        self._sweep_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['NetworkManager']))

        cfg = Adw.PreferencesGroup(
            title="Scan",
            description="Managed interface only (not monitor). We "
                        "scan the visible SSID list and match "
                        "against a table of vendor patterns.")
        self.iface = Adw.EntryRow(title="Interface")
        self.iface.set_text(DEFAULT_IFACE)
        cfg.add(self.iface)

        self.continuous = Adw.SwitchRow(
            title="Continuous sweep",
            subtitle="Re-scan every 5 s until Stop is pressed")
        cfg.add(self.continuous)

        row = Adw.ActionRow(
            title="Sweep",
            subtitle="`iw scan` + P2P discovery")
        self.scan_btn = Gtk.Button(label="Start",
                                   valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked",
                              lambda _b: self._start_scan())
        row.add_suffix(self.scan_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked",
                              lambda _b: self._stop_scan())
        row.add_suffix(self.stop_btn)
        cfg.add(row)
        box.append(cfg)

        # ---- results
        self.results_group = Adw.PreferencesGroup(
            title="P2P / soft-APs in range",
            description="Rows expand with an attach action")
        self._empty_row = Adw.ActionRow(
            title="No sweep yet",
            subtitle="Press Sweep and wait a few seconds")
        self.results_group.add(self._empty_row)
        box.append(self.results_group)

        self.output = OutputView()
        box.append(self.output)
        return box

    def _start_scan(self) -> None:
        self.scan_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self._do_one_sweep()
        # Continuous mode chains another sweep every 5 s; single-shot
        # mode drops the Stop back to False in the on_done handler.
        if self.continuous.get_active():
            self._sweep_id = GLib.timeout_add_seconds(
                5, self._do_one_sweep)

    def _stop_scan(self) -> None:
        if self._sweep_id is not None:
            try:
                GLib.source_remove(self._sweep_id)
            except Exception:
                pass
            self._sweep_id = None
        self.scan_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)

    def _do_one_sweep(self) -> bool:
        iface = self.iface.get_text().strip() or DEFAULT_IFACE

        # Two sources of info:
        #  1) `iw dev <if> scan` -- classic scan for regular SSIDs
        #     (soft-APs of Chromecast, printers, etc show here).
        #  2) `iw dev <if> p2p_find` + `p2p_peers` (needs P2P-capable
        #     iface) -- proper Wi-Fi Direct peer discovery.
        # We prefer wpa_cli p2p_find + p2p_peers because they aggregate
        # cleanly. Fall back to `iw scan` if p2p is unsupported.
        script = (
            "IF=%s; "
            "iw dev $IF scan 2>/dev/null | "
            "awk '/^BSS/ {bss=$2} "
            "     /SSID/ {sub(/^\\s+SSID:\\s?/,\\\"\\\"); "
            "             print bss\\\"|\\\" $0}' | head -200"
        ) % iface
        self.output.append("# scanning P2P / soft-APs\n")

        def done(r: Result) -> None:
            # Single-shot mode: flip the buttons back to idle. In
            # continuous mode the caller keeps them locked and the
            # 5 s timer will call us again.
            if self._sweep_id is None:
                self.scan_btn.set_sensitive(True)
                self.stop_btn.set_sensitive(False)
            aps = self._parse_scan(r.stdout or "")
            for row in self._ap_rows:
                self.results_group.remove(row)
            self._ap_rows = []
            self._empty_row.set_visible(not aps)
            for bssid, ssid in aps:
                cat, hint = _classify(ssid)
                self._aps[bssid] = {
                    "ssid": ssid, "cat": cat, "hint": hint}
                self._ap_rows.append(
                    self._make_row(bssid, ssid, cat, hint))

        run_async(["sh", "-c", script], done,
                  root=True, timeout=30)
        # Keep the GLib timer alive as long as continuous mode is on.
        return self._sweep_id is not None

    def _parse_scan(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line in text.splitlines():
            if "|" not in line:
                continue
            bssid, _, ssid = line.partition("|")
            bssid = bssid.strip().lower()
            ssid = ssid.strip()
            if not bssid or not ssid:
                continue
            k = (bssid, ssid)
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
        return out

    def _make_row(self, bssid: str, ssid: str,
                  cat: str, hint: str) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(
            title=ssid,
            subtitle="%s · %s" % (bssid, cat))
        if hint:
            row.add_row(Adw.ActionRow(title="Hint",
                                      subtitle=hint))

        # Attach: nmcli connect to the SSID (open networks only; PBC
        # would need a real WPS interaction that nmcli does not do).
        att_row = Adw.ActionRow(
            title="Attach (open only)",
            subtitle="`nmcli device wifi connect`")
        att_btn = Gtk.Button(label="Attach",
                             valign=Gtk.Align.CENTER)
        att_btn.add_css_class("suggested-action")
        att_btn.connect(
            "clicked",
            lambda _b, s=ssid: self._attach(s))
        att_row.add_suffix(att_btn)
        row.add_row(att_row)

        # Hand off actions by category.
        if "printer" in cat.lower() or "HP" in cat:
            pret_row = Adw.ActionRow(
                title="PRET",
                subtitle="Send to IoT Hacking's PRET helper")
            pret_btn = Gtk.Button(label="PRET",
                                  valign=Gtk.Align.CENTER)
            pret_btn.connect(
                "clicked",
                lambda _b: self.app_window.activate_module(
                    "iot_hacking", ssid))
            pret_row.add_suffix(pret_btn)
            row.add_row(pret_row)
        if "chromecast" in cat.lower():
            web_row = Adw.ActionRow(
                title="Open setup UI (once attached)",
                subtitle="https://192.168.255.249:8443")
            open_btn = Gtk.Button(label="Open",
                                  valign=Gtk.Align.CENTER)
            open_btn.connect(
                "clicked",
                lambda _b: Gtk.UriLauncher.new(
                    "https://192.168.255.249:8443"
                ).launch(self.app_window, None, None, None))
            web_row.add_suffix(open_btn)
            row.add_row(web_row)
        self.results_group.add(row)
        return row

    def _attach(self, ssid: str) -> None:
        # nmcli connect on the phone's own managed interface (wlan0)
        # so we don't have to know the iface here. This DOES kick the
        # phone off its current Wi-Fi -- ok since the point is to
        # associate to the target soft-AP.
        script = ("nmcli device wifi connect %s || true; "
                  "nmcli connection show --active | head -20"
                  ) % Gtk.g_shell_quote(ssid) \
                    if hasattr(Gtk, "g_shell_quote") \
                    else ("nmcli device wifi connect '%s' || true; "
                          "nmcli connection show --active | head -20"
                          % ssid.replace("'", ""))
        self.output.append("$ " + script + "\n")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)

        run_async(["sh", "-c", script], done,
                  root=True, timeout=15)
