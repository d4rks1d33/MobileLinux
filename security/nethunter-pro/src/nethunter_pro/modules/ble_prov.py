"""BLE provisioning sniffer -- catch the Wi-Fi PSK during onboarding.

An entire class of IoT devices (Espressif ESP-IDF-based, Tuya, Xiaomi
Mijia, some Nest hardware, various Matter commissioners) uses BLE
GATT for the onboarding step: the vendor's phone app pairs with the
device via BLE and writes the target Wi-Fi SSID and PSK to a
characteristic. Cleartext when the vendor forgot the Proof of
Possession (PoP), or with a weak PoP token like ``abcd1234``.

This module wraps ``bettercap`` in ``ble.recon on`` mode and streams
its output. Every GATT write it captures is logged and saved to a
pcap under the loot tree. We also expose the classic BLE actions
(scan, connect, dump characteristics) so the operator can prod a
suspicious peripheral manually.

Hardware note: the phone's on-board Bluetooth adapter works for
advertisement-scan but *not* for sniffing raw BLE frames from
between two other devices. That requires a Nordic nRF52840 or a
TI CC1352 dongle running the Sniffle firmware -- if one is plugged
in the module will use it via ``btmon``.
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

DEFAULT_ADAPTER = "hci0"

# Bettercap prints one line per BLE event; the interesting ones are:
#   [ble.recon] new_device <mac> <name>
#   [ble.recon] read char ... = <hex>
#   [ble.recon] write char ... = <hex>
# We latch on 'new_device' and 'write' to count peripherals and PSK
# candidates respectively.
_NEW_DEVICE_RE = re.compile(
    r"new device.*?([0-9A-Fa-f:]{17})\s+([^\r\n]+)", re.IGNORECASE)
_WRITE_RE = re.compile(
    r"write.*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE)


@register
class BLEProv(NHModule):
    title = "BLE provisioning"
    icon = "bluetooth-symbolic"
    description = ("Sniff BLE GATT provisioning traffic and extract "
                   "cleartext Wi-Fi PSKs from Tuya / ESP32 / Nest "
                   "onboarding")
    required_tools = ["bettercap"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._devices: dict[str, str] = {}  # mac -> name
        self._writes: int = 0
        self._loot_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['bluetooth']))

        cfg = Adw.PreferencesGroup(
            title="Recon",
            description="Runs bettercap in BLE recon mode. Every "
                        "advertisement / connect / read / write is "
                        "logged to loot.")

        self.adapter = Adw.EntryRow(title="HCI adapter")
        self.adapter.set_text(DEFAULT_ADAPTER)
        cfg.add(self.adapter)

        self.enum_writes = Adw.SwitchRow(
            title="Enumerate characteristics on new devices",
            subtitle="bettercap ble.enum <mac> -- reads every GATT "
                     "characteristic; useful for weak PoP")
        self.enum_writes.set_active(True)
        cfg.add(self.enum_writes)

        run_row = Adw.ActionRow(
            title="Start / Stop",
            subtitle="Peripherals appear below as they advertise")
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

        # ---- stats
        stat_group = Adw.PreferencesGroup(title="Session")
        self.state_row = Adw.ActionRow(
            title="Status", subtitle="idle")
        stat_group.add(self.state_row)
        self.dev_row = Adw.ActionRow(
            title="Unique BLE peripherals", subtitle="0")
        stat_group.add(self.dev_row)
        self.writes_row = Adw.ActionRow(
            title="GATT writes seen", subtitle="0")
        stat_group.add(self.writes_row)
        box.append(stat_group)

        # ---- warnings
        warn_group = Adw.PreferencesGroup(title="Hardware notes")
        warn_group.add(Adw.ActionRow(
            title="Sniffing between two other devices",
            subtitle="Needs an nRF52840 or CC1352 with Sniffle -- "
                     "the phone's HCI adapter only sees traffic to/"
                     "from itself."))
        box.append(warn_group)

        # ---- log
        self.output = OutputView()
        box.append(self.output)
        return box

    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        # bettercap script that turns on ble recon + optionally
        # enumerates writes on every new device (which is the
        # closest we get to "see the PSK" from the phone's own
        # side without a Sniffle sniffer).
        commands = ["ble.recon on"]
        if self.enum_writes.get_active():
            # ble.recon events trigger; ble.enum reads all chars for
            # each new mac. bettercap will fan them out for us.
            commands.append(
                "events.on ble.device.new "
                "ble.enum '{{.Device.Address}}'")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        pcap = loot_path(
            "ble_prov", "ble-prov-%s.pcap" % stamp)
        # bettercap can write a pcap of BLE HCI traffic via
        # events.stream -- but only when the OS has btmon's pipe
        # available. We start btmon in parallel as a safety.
        self._loot_id = get_loot_store().record(
            module="ble_prov", type="ble_pcap",
            target="bettercap ble.recon",
            path=pcap, notes="in progress")
        run_async(
            ["sh", "-c",
             "btmon -w %s >/dev/null 2>&1 &" % pcap],
            lambda _r: None, root=True, timeout=5)

        argv = ["bettercap", "-eval", "; ".join(commands)]
        self.output.append("$ " + " ".join(argv) + "\n")

        self._devices = {}
        self._writes = 0
        self.dev_row.set_subtitle("0")
        self.writes_row.set_subtitle("0")

        def on_line(text: str) -> None:
            self.output.append(text)
            for m in _NEW_DEVICE_RE.finditer(text or ""):
                mac = m.group(1).lower()
                name = m.group(2)
                self._devices[mac] = name
            self._writes += len(_WRITE_RE.findall(text or ""))
            self.dev_row.set_subtitle(str(len(self._devices)))
            self.writes_row.set_subtitle(str(self._writes))

        def on_done(code: int) -> None:
            self.output.append("[bettercap exited: %d]\n" % code)
            self._proc = None
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            self.state_row.set_subtitle("stopped")
            # Kill btmon
            run_async(["sh", "-c", "pkill -f 'btmon -w' || true"],
                      lambda _r: None, root=True, timeout=5)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)
                get_loot_store().append_notes(
                    self._loot_id,
                    "%d devices, %d writes"
                    % (len(self._devices), self._writes))

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
