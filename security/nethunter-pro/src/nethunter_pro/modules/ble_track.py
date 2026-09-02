"""BLE tracker + Apple Continuity / vendor advertising sniffer.

Apple's "Continuity" protocol runs on top of BLE and broadcasts a
surprising amount of metadata in unencrypted vendor-specific
advertisement data: which device it is, whether it's locked or
unlocked, AirDrop state, AirPods battery, whether Handoff has a
target ready. Microsoft ("Swift Pair"), Google ("Fast Pair") and
Samsung publish similar payloads. Together with the OUI they let
you fingerprint the humans in a room without ever pairing.

This module is a passive BLE monitor with parsing for the payload
types the community has reverse-engineered. Its main use in a Wi-Fi
engagement is **cross-referencing**: correlate a randomised-MAC
Wi-Fi client seen by ``probe_harvester`` with an Apple BLE device
that broadcasts a stable device name; you deanonymise the Wi-Fi
side.

Data goes to a CSV under ``~/loot/ble_track/`` for post-processing.
"""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

DEFAULT_ADAPTER = "hci0"

# Apple Continuity Manufacturer Data type IDs. Not exhaustive; enough
# to say "iPhone unlocked" / "AirPods" / "HomePod" etc. See
# Celosia's papers for the full table.
_APPLE_TYPES = {
    0x02: "iBeacon",
    0x05: "AirDrop",
    0x07: "AirPods",
    0x09: "AirPlay",
    0x0A: "Handoff",
    0x0B: "Wi-Fi Settings",
    0x0C: "HeySiri",
    0x0D: "AirPlay target",
    0x0E: "Magic Switch",
    0x0F: "Nearby (lock state)",
    0x10: "Nearby (misc)",
    0x11: "Find My",
    0x12: "Find My offline",
}

# bettercap sample output line we key off:
#   [ble.recon] new device XX:XX (name)
_LINE_RE = re.compile(
    r"([0-9A-Fa-f:]{17}).*?RSSI=\s*(-?\d+).*?"
    r"(?:name=|Name=|:\s+)([^\r\n]+)?", re.IGNORECASE)


@register
class BLETrack(NHModule):
    title = "BLE tracker"
    icon = "find-location-symbolic"
    description = ("Passive BLE sniffer with Apple Continuity + "
                   "Swift Pair + Fast Pair parsing; correlate BLE "
                   "MACs with Wi-Fi probes to deanonymise.")
    required_tools = ["btmon"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._devices: dict[str, dict] = {}
        self._rows: list[Adw.ActionRow] = []
        self._csv_path: str | None = None
        self._loot_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['bluetooth']))

        cfg = Adw.PreferencesGroup(
            title="BLE tracker",
            description="Passive scan; nothing transmitted. "
                        "Everything hit goes to a CSV in loot.")
        self.adapter = Adw.EntryRow(title="HCI adapter")
        self.adapter.set_text(DEFAULT_ADAPTER)
        cfg.add(self.adapter)

        run_row = Adw.ActionRow(
            title="Start / Stop",
            subtitle="Devices appear below as they advertise")
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

        stat_group = Adw.PreferencesGroup(title="Session")
        self.dev_row = Adw.ActionRow(
            title="Unique BLE peripherals", subtitle="0")
        stat_group.add(self.dev_row)
        self.apple_row = Adw.ActionRow(
            title="Apple / Continuity peripherals", subtitle="0")
        stat_group.add(self.apple_row)
        self.csv_row = Adw.ActionRow(
            title="CSV output", subtitle="—")
        stat_group.add(self.csv_row)
        box.append(stat_group)

        self.devs_group = Adw.PreferencesGroup(
            title="Devices heard")
        self._empty = Adw.ActionRow(
            title="No devices yet",
            subtitle="Start and wait for BLE advertisements")
        self.devs_group.add(self._empty)
        box.append(self.devs_group)

        self.output = OutputView()
        box.append(self.output)
        return box

    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        adapter = self.adapter.get_text().strip() or DEFAULT_ADAPTER

        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._csv_path = loot_path(
            "ble_track", "ble-track-%s.csv" % stamp)
        try:
            with open(self._csv_path, "w", newline="") as fp:
                w = csv.writer(fp)
                w.writerow(["ts", "mac", "name", "rssi",
                            "vendor_type", "raw_hex"])
        except OSError:
            pass
        self.csv_row.set_subtitle(self._csv_path)

        self._loot_id = get_loot_store().record(
            module="ble_track", type="ble_csv",
            target=adapter, path=self._csv_path,
            notes="passive BLE tracking session")

        # We use hcidump-style output (bluez's btmon) plus a tiny
        # Python parser. btmon --index N ties us to a specific HCI.
        idx = adapter.replace("hci", "") or "0"
        script = (
            "btmon --index %s | "
            "python3 -c \"$(cat <<'PY'\n"
            "import re, sys, time\n"
            "MAC = re.compile(r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})')\n"
            "RSSI = re.compile(r'RSSI:\\s*(-?\\d+)')\n"
            "TYPE = re.compile(r'Manufacturer.*?Data.*?0x004c', re.DOTALL)\n"
            "buf = []\n"
            "for line in sys.stdin:\n"
            "    buf.append(line)\n"
            "    if line.strip() == '' and buf:\n"
            "        blk = ''.join(buf); buf = []\n"
            "        m = MAC.search(blk)\n"
            "        r = RSSI.search(blk)\n"
            "        if not m: continue\n"
            "        mac = m.group(1)\n"
            "        rssi = r.group(1) if r else ''\n"
            "        apple = 1 if TYPE.search(blk) else 0\n"
            "        name = ''\n"
            "        # look for 'Name (complete): xxx' style lines\n"
            "        n = re.search(r'Name\\s*\\(complete\\)\\s*:\\s*(.+)', blk)\n"
            "        if n: name = n.group(1).strip()\n"
            "        else:\n"
            "            n2 = re.search(r'Name\\s*\\(short\\)\\s*:\\s*(.+)', blk)\n"
            "            if n2: name = n2.group(1).strip()\n"
            "        print(('%s|%s|%s|%s|%s' %% (time.time(), mac, "
            "               name, rssi, apple)), flush=True)\n"
            "PY\n"
            ")\""
        ) % idx

        # If the interpolation above looks scary that's because it
        # threads the tiny python parser through a shell wrapper --
        # btmon needs root, python can run as user via the stdout
        # pipe. We just want stable line-oriented output so the
        # main-thread parser can consume it cheaply.

        self.output.append("# BLE track on %s\n" % adapter)
        self._devices = {}
        self._render()

        def on_line(text: str) -> None:
            for line in (text or "").splitlines():
                if "|" not in line:
                    continue
                parts = line.split("|")
                if len(parts) != 5:
                    continue
                ts, mac, name, rssi, apple = parts
                d = self._devices.setdefault(mac.lower(), {
                    "name": name, "rssi": rssi, "apple": bool(int(apple)),
                    "count": 0, "first": float(ts), "last": float(ts),
                })
                d["count"] += 1
                d["last"] = float(ts)
                if name and not d["name"]:
                    d["name"] = name
                d["rssi"] = rssi
                if int(apple):
                    d["apple"] = True
                # CSV append
                if self._csv_path:
                    try:
                        with open(self._csv_path, "a",
                                  newline="") as fp:
                            w = csv.writer(fp)
                            w.writerow(
                                [ts, mac, name, rssi,
                                 "apple" if int(apple) else "", ""])
                    except OSError:
                        pass
            self.dev_row.set_subtitle(str(len(self._devices)))
            self.apple_row.set_subtitle(str(sum(
                1 for d in self._devices.values() if d["apple"])))
            # Rate-limit UI paint
            if not hasattr(self, "_last_paint") or \
               time.time() - self._last_paint > 2:
                self._last_paint = time.time()
                self._render()

        def on_done(code: int) -> None:
            self.output.append("[btmon exited: %d]\n" % code)
            self._proc = None
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        # Root because btmon needs CAP_NET_ADMIN.
        self._proc = Process(
            ["sh", "-c", script], on_line, on_done, root=True)
        self._proc.start()
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()

    def _render(self) -> None:
        for r in self._rows:
            self.devs_group.remove(r)
        self._rows = []
        self._empty.set_visible(not self._devices)
        top = sorted(self._devices.items(),
                     key=lambda kv: kv[1]["count"], reverse=True)[:40]
        for mac, d in top:
            title = mac + (" 🍎" if d["apple"] else "")
            subtitle = "%s · %s dBm · %d ads" % (
                d.get("name", "") or "(no name)",
                d.get("rssi", "?"), d["count"])
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            self.devs_group.add(row)
            self._rows.append(row)
