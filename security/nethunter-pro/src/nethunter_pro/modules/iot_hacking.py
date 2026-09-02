"""IoT Hacking -- unified discovery + auto-exploitation for LAN IoT.

One module to point at any home / SMB network and get back a categorised
list of discovered devices (routers, TVs, cameras, plugs, bulbs, hubs,
speakers, vacuums, appliances, whatever else advertises itself over
mDNS/SSDP/UDP broadcast), each with the attacks that are available for
it. The heavy lifting (discovery, fingerprinting, playbook execution,
persistence) lives in :mod:`nethunter_pro.iot_hacking`; this file is the
GTK4 / libadwaita glue.

UI shape:

  * a small Radar page at the top with the Scan / Stop buttons and a
    small progress banner;
  * a category-grouped list of everything the last scan surfaced,
    grouped by :class:`~iot_hacking.core.models.DeviceType` (Routers,
    TVs, Smart plugs, Bulbs, Speakers, Vacuums, Appliances, Hubs...);
  * per-device expander rows show model / IP / MAC / fingerprint
    breakdown / open ports / vulnerabilities / a stack of buttons for
    the playbook actions the plugin exposes;
  * an OutputView streams the playbook runner's live NDJSON lines the
    same way the ``blespam`` and ``deauth`` modules already do.

The heavy async work happens in a background asyncio loop owned by the
module. All UI updates are marshalled back via ``GLib.idle_add`` and
GObject signals from the :class:`~iot_hacking.core.registry.DeviceRegistry`
-- the module never touches Gtk from off the main thread.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from ..executor import run_async as executor_run_async
from ..iot_hacking.core.discovery import Discovery
from ..iot_hacking.core.exploit import (
    Playbook,
    PlaybookAction,
    PlaybookLibrary,
    Runner,
)
from ..iot_hacking.core.fingerprint import FingerprintEngine
from ..iot_hacking.core.models import ActionCategory, Device, DeviceType
from ..iot_hacking.core.registry import DeviceRegistry
from ..module import NHModule, register
from ..widgets import OutputView, toast


async def _root_runner(argv, timeout=20):
    """Async bridge to the DBus root helper.

    The executor's ``run_async`` is thread-based with a callback; we
    wrap it in a Future so the Discovery pipeline can ``await`` on it
    like on any other coroutine. Returns the combined stdout+stderr as
    a plain string -- Discovery only needs the arp-scan tabular text.
    """
    fut: asyncio.Future = asyncio.get_event_loop().create_future()

    def done(result) -> None:
        # Deliver from any thread -- schedule on the async loop that
        # created the future.
        loop = fut.get_loop()
        if not fut.done():
            loop.call_soon_threadsafe(
                fut.set_result,
                (result.stdout or "") + (result.stderr or ""))

    executor_run_async(argv, done, root=True, timeout=timeout)
    return await fut

# PRET (Printer Exploitation Toolkit) integration path. Bundled at
# /opt/pret on the phone; the printer_print / printer_discover helpers
# below shell out to `python2 /opt/pret/pret.py`.
PRET_DIR = "/opt/pret"


# Human labels + Adwaita icons for every DeviceType, so the sidebar
# grouping looks like something a person would design.
_TYPE_META: dict[DeviceType, tuple[str, str]] = {
    DeviceType.ROUTER:      ("Routers",           "network-wired-symbolic"),
    DeviceType.TV:          ("TVs",               "video-display-symbolic"),
    DeviceType.STREAMER:    ("Streamers / Casts", "applications-multimedia-symbolic"),
    DeviceType.CAMERA:      ("Cameras",           "camera-web-symbolic"),
    DeviceType.SMART_PLUG:  ("Smart plugs",       "battery-symbolic"),
    DeviceType.SMART_BULB:  ("Smart bulbs",       "weather-clear-symbolic"),
    DeviceType.SMART_SWITCH:("Smart switches",    "system-shutdown-symbolic"),
    DeviceType.THERMOSTAT:  ("Thermostats",       "temperature-symbolic"),
    DeviceType.HVAC:        ("AC / HVAC",         "weather-fog-symbolic"),
    DeviceType.APPLIANCE:   ("Appliances",        "utilities-terminal-symbolic"),
    DeviceType.SPEAKER:     ("Speakers",          "audio-speakers-symbolic"),
    DeviceType.VACUUM:      ("Vacuums",           "cleanup-symbolic"),
    DeviceType.HUB:         ("Hubs / Frameworks", "preferences-desktop-symbolic"),
    DeviceType.PRINTER:     ("Printers",          "printer-symbolic"),
    DeviceType.NAS:         ("NAS",               "drive-harddisk-symbolic"),
    DeviceType.DOORBELL:    ("Doorbells",         "call-start-symbolic"),
    DeviceType.LOCK:        ("Locks",             "channel-secure-symbolic"),
    DeviceType.EV_CHARGER:  ("EV chargers",       "battery-symbolic"),
    DeviceType.SOLAR:       ("Solar",             "weather-clear-symbolic"),
    DeviceType.POOL:        ("Pool controllers",  "weather-showers-symbolic"),
    DeviceType.PHONE:       ("Phones",            "phone-symbolic"),
    DeviceType.COMPUTER:    ("Computers",         "computer-symbolic"),
    DeviceType.UNKNOWN:     ("Unclassified",      "dialog-question-symbolic"),
}


@register
class IoTHacking(NHModule):
    title = "IoT Hacking"
    icon = "network-workgroup-symbolic"
    description = "Discover every IoT device on the LAN and run the attack playbooks that apply to each"
    required_tools = ["arp-scan"]   # everything else is optional

    def __init__(self, app_window):
        super().__init__(app_window)
        # Long-lived services. Constructed once when the module is
        # first shown; kept alive for the process's lifetime so scans
        # accumulate across visits.
        self.registry = DeviceRegistry()
        self.registry.load_all()
        self.fingerprint = FingerprintEngine()
        self.playbooks = PlaybookLibrary()

        # Async loop lives on its own thread so the GTK main loop
        # stays responsive during long scans.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._start_async_loop()

        self._discovery: Discovery | None = None
        self._scan_task: asyncio.Task | None = None
        self._attack_task: asyncio.Task | None = None
        # Per-DeviceType: the ExpanderRow that holds that group's rows.
        self._group_rows: dict[DeviceType, Adw.ExpanderRow] = {}
        # Per-MAC: the ActionRow inside its group.
        self._device_rows: dict[str, Adw.ExpanderRow] = {}

    # ------------------------------------------------------------ build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ---- radar / scan controls -------------------------------
        radar = Adw.PreferencesGroup(
            title="Radar",
            description="Sweeps the local subnet with arp-scan + mDNS + "
                        "SSDP + UDP broadcast probes, then fingerprints "
                        "every reply against the plugin catalogue.")
        iface_row = Adw.EntryRow(title="Interface")
        iface_row.set_text("wlan0")
        self.iface_row = iface_row
        radar.add(iface_row)

        run_row = Adw.ActionRow(
            title="Scan the LAN",
            subtitle="Takes ~20-30 seconds")
        self.scan_btn = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda _b: self._start_scan())
        run_row.add_suffix(self.scan_btn)
        self.stop_btn = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop_scan())
        run_row.add_suffix(self.stop_btn)
        radar.add(run_row)

        # Live progress
        self.progress_row = Adw.ActionRow(
            title="Idle",
            subtitle="press Scan to begin")
        radar.add(self.progress_row)
        box.append(radar)

        # ---- discovered devices, grouped by type -----------------
        self.devices_group = Adw.PreferencesGroup(
            title="Discovered devices",
            description="Grouped by category. Tap any row to expand "
                        "its details and available attacks.")
        self._empty_row = Adw.ActionRow(
            title="No devices yet",
            subtitle="run a scan or wait for previous state to load")
        self.devices_group.add(self._empty_row)
        box.append(self.devices_group)

        # ---- streaming output ------------------------------------
        self.output = OutputView()
        box.append(self.output)

        # Wire registry signals now that the widgets exist.
        self.registry.connect("device-added",
                              lambda _r, mac: self._on_device_changed(mac))
        self.registry.connect("device-updated",
                              lambda _r, mac: self._on_device_changed(mac))
        self.registry.connect("cleared",
                              lambda _r: self._rebuild_devices())

        # Populate from persisted state.
        self._rebuild_devices()
        return box

    # ------------------------------------------------ async loop plumbing
    def _start_async_loop(self) -> None:
        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=worker, daemon=True, name="iot-async-loop")
        self._loop_thread.start()
        # Wait briefly for the loop to spin up so the first
        # run_coroutine_threadsafe call doesn't race.
        for _ in range(20):
            if self._loop is not None:
                return
            import time
            time.sleep(0.05)

    def _submit(self, coro) -> asyncio.Task:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------ scan
    def _start_scan(self) -> None:
        if self._scan_task is not None and not self._scan_task.done():
            return
        iface = self.iface_row.get_text().strip() or "wlan0"
        self._discovery = Discovery(
            self.registry, self.fingerprint, iface=iface,
            root_runner=_root_runner)
        self.output.append("# radar sweep on %s\n" % iface)
        self.scan_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.progress_row.set_title("Scanning…")
        self.progress_row.set_subtitle("starting…")
        self._scan_task = self._submit(self._run_scan())

    def _stop_scan(self) -> None:
        if self._discovery is not None:
            self._discovery.stop()
        self.output.append("[scan stopping]\n")

    async def _run_scan(self) -> None:
        def prog(msg: str) -> None:
            def upd() -> bool:
                self.progress_row.set_subtitle(msg)
                return False
            GLib.idle_add(upd)
            self.output.append("[phase] %s\n" % msg)
        try:
            await self._discovery.run(on_progress=prog)
        except Exception as exc:  # noqa: BLE001
            self.output.append("[error] %s\n" % exc)
        finally:
            def done() -> bool:
                self.scan_btn.set_sensitive(True)
                self.stop_btn.set_sensitive(False)
                self.progress_row.set_title("Idle")
                self.progress_row.set_subtitle(
                    "%d device(s) known" % len(self.registry.snapshot()))
                return False
            GLib.idle_add(done)

    # ------------------------------------------------ device list render
    def _on_device_changed(self, mac: str) -> None:
        dev = self.registry.by_mac(mac)
        if dev is None:
            return
        # Simplest thing that works: rebuild the affected group. For
        # 100+ devices this becomes noticeable; when we get there
        # we'll switch to ListView with a virtualised model.
        self._rebuild_devices()

    def _rebuild_devices(self) -> None:
        # Nuke every existing group + device row, keep the empty state.
        for row in list(self._device_rows.values()):
            self.devices_group.remove(row)
        for row in list(self._group_rows.values()):
            self.devices_group.remove(row)
        self._device_rows.clear()
        self._group_rows.clear()

        devices = self.registry.snapshot()
        if not devices:
            self._empty_row.set_visible(True)
            return
        self._empty_row.set_visible(False)

        # Group by device type, sort types by count desc for the
        # sections the user is most likely to care about first.
        by_type: dict[DeviceType, list[Device]] = {}
        for d in devices:
            by_type.setdefault(d.device_type, []).append(d)
        ordered = sorted(by_type.items(),
                         key=lambda kv: (-len(kv[1]),
                                         list(_TYPE_META).index(kv[0])))
        for dtype, devs in ordered:
            self._render_group(dtype, devs)

    def _render_group(self, dtype: DeviceType,
                      devs: list[Device]) -> None:
        label, icon = _TYPE_META.get(
            dtype, (dtype.value.title(),
                    "dialog-question-symbolic"))
        row = Adw.ExpanderRow(
            title=label,
            subtitle="%d device(s)" % len(devs))
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        self.devices_group.add(row)
        self._group_rows[dtype] = row
        for dev in sorted(devs, key=lambda d: d.ip or d.mac):
            self._render_device(row, dev)

    def _render_device(self, parent: Adw.ExpanderRow,
                       dev: Device) -> None:
        title = dev.label()
        subtitle_bits = [dev.ip or "?"]
        if dev.mac:
            subtitle_bits.append(dev.mac)
        if dev.vendor:
            subtitle_bits.append(dev.vendor)
        # Show nmap OS guess when we have one and the label was falling
        # back to the IP -- that's the most common way an unclassified
        # host lands here.
        os_guess = dev.metadata.get("os", "")
        if os_guess:
            subtitle_bits.append(os_guess)
        subtitle = " -- ".join(subtitle_bits)
        row = Adw.ExpanderRow(title=title, subtitle=subtitle)
        parent.add_row(row)
        self._device_rows[dev.mac] = row

        # -- info block ----
        if dev.vendor or dev.model:
            r = Adw.ActionRow(
                title="Identity",
                subtitle="%s %s" % (dev.vendor or "-",
                                    dev.model or ""))
            row.add_row(r)
        if dev.firmware:
            row.add_row(Adw.ActionRow(
                title="Firmware", subtitle=dev.firmware))
        row.add_row(Adw.ActionRow(
            title="Confidence",
            subtitle="%d%%" % dev.confidence))

        # -- ports ----
        if dev.ports:
            ports_txt = ", ".join(
                "%d/%s" % (p.number, p.protocol) for p in dev.ports[:15])
            row.add_row(Adw.ActionRow(
                title="Open ports", subtitle=ports_txt))

        # -- fingerprints ----
        agg = [f for f in dev.fingerprints
               if f.matcher_name == "_aggregate"]
        for fp in agg[:3]:
            r = Adw.ActionRow(
                title="Fingerprint: " + fp.plugin_id,
                subtitle="confidence %d%%" % fp.confidence)
            row.add_row(r)

        # -- vulns ----
        for vuln in dev.vulns[:5]:
            r = Adw.ActionRow(
                title="Vuln: " + vuln.title,
                subtitle="%s -- %s" %
                (vuln.severity.value, vuln.cve or "-"))
            row.add_row(r)

        # -- attack buttons: one per playbook action -----
        plays = self.playbooks.for_device(dev)
        if not plays:
            row.add_row(Adw.ActionRow(
                title="No playbooks apply yet",
                subtitle="try running a full scan"))
        for pb in plays:
            for action in pb.actions:
                self._render_action_row(row, dev, pb, action)

        # Extra printer-specific tools that don't fit the YAML DSL
        # because they need interactive input (custom text or an image
        # path). Only shown when the device looks like a printer.
        if dev.device_type == DeviceType.PRINTER:
            self._render_pret_actions(row, dev)

    def _render_action_row(self, parent: Adw.ExpanderRow, dev: Device,
                           pb: Playbook, action: PlaybookAction) -> None:
        cat_label = {
            "check_safe": "Check",
            "recon": "Recon",
            "exploit_control": "Control",
            "exploit_destructive": "DANGER",
            "persistence": "Persist",
        }.get(action.category, action.category)
        subtitle = "%s -- %s" % (pb.id, cat_label)
        r = Adw.ActionRow(title=action.label, subtitle=subtitle)
        btn = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
        if action.category == "exploit_destructive":
            btn.add_css_class("destructive-action")
        elif action.category == "exploit_control":
            btn.add_css_class("suggested-action")
        btn.connect(
            "clicked",
            lambda _b, d=dev, p=pb, a=action:
                self._trigger_action(d, p, a))
        r.add_suffix(btn)
        parent.add_row(r)

    def _trigger_action(self, dev: Device, pb: Playbook,
                        action: PlaybookAction) -> None:
        if action.dangerous and action.confirm_prompt:
            dlg = Adw.MessageDialog(
                transient_for=self.app_window,
                heading="Confirm attack",
                body=action.confirm_prompt)
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("run", "Run")
            dlg.set_response_appearance(
                "run", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")

            def on_response(_d, resp: str) -> None:
                if resp == "run":
                    self._launch(dev, pb, action)
            dlg.connect("response", on_response)
            dlg.present()
        else:
            self._launch(dev, pb, action)

    def _launch(self, dev: Device, pb: Playbook,
                action: PlaybookAction) -> None:
        if self._attack_task is not None and not self._attack_task.done():
            toast(self.app_window, "An attack is already running")
            return
        self.output.append("[run] %s :: %s on %s\n" %
                            (pb.id, action.id, dev.label()))
        self._attack_task = self._submit(
            self._run_action(dev, pb, action))

    async def _run_action(self, dev: Device, pb: Playbook,
                          action: PlaybookAction) -> None:
        def emit(text: str) -> None:
            self.output.append(text)
        runner = Runner(on_line=emit)
        try:
            run = await runner.execute(pb, action, dev)
            dev.attack_history.append(run)
            self.registry.audit("user", "run", dev.mac, {
                "playbook": pb.id, "action": action.id,
                "status": run.status,
            })
        except Exception as exc:  # noqa: BLE001
            self.output.append("[error] %s\n" % exc)

    # ------------------------------------------------ PRET printer
    def _render_pret_actions(self, parent: Adw.ExpanderRow,
                             dev: Device) -> None:
        """Extra printer buttons routed through /opt/pret.

        PRET's ``print <file>|"text"`` subcommand converts an image to
        PCL (via ImageMagick + Ghostscript) or sends raw text to the
        printer. It needs the target host and a language; PJL is the
        safest default and works on the vast majority of HP / Brother /
        Canon / Xerox network printers.
        """
        import os
        if not os.path.exists(PRET_DIR):
            parent.add_row(Adw.ActionRow(
                title="PRET not installed at /opt/pret",
                subtitle="clone https://github.com/rub-nds/pret there "
                         "to unlock the print-anything buttons"))
            return

        text_row = Adw.ActionRow(
            title="Print custom text (PRET / PJL)",
            subtitle="Opens a prompt; sends via PJL")
        text_btn = Gtk.Button(label="Print…",
                              valign=Gtk.Align.CENTER)
        text_btn.add_css_class("destructive-action")
        text_btn.connect(
            "clicked",
            lambda _b, d=dev: self._prompt_pret_text(d))
        text_row.add_suffix(text_btn)
        parent.add_row(text_row)

        img_row = Adw.ActionRow(
            title="Print image file (PRET / PCL)",
            subtitle="Pick a file (jpg/png/pdf); ImageMagick converts")
        img_btn = Gtk.Button(label="Choose…",
                             valign=Gtk.Align.CENTER)
        img_btn.add_css_class("destructive-action")
        img_btn.connect(
            "clicked",
            lambda _b, d=dev: self._prompt_pret_image(d))
        img_row.add_suffix(img_btn)
        parent.add_row(img_row)

        disc_row = Adw.ActionRow(
            title="Discover printers via SNMP (PRET)",
            subtitle="Broadcasts SNMP and lists every printer")
        disc_btn = Gtk.Button(label="Scan",
                              valign=Gtk.Align.CENTER)
        disc_btn.connect("clicked",
                         lambda _b: self._run_pret_discover())
        disc_row.add_suffix(disc_btn)
        parent.add_row(disc_row)

    def _prompt_pret_text(self, dev: Device) -> None:
        """Ask the user for a text string, then send it to the printer."""
        dlg = Adw.MessageDialog(
            transient_for=self.app_window,
            heading="Print custom text",
            body="This text will be printed on %s as a physical page. "
                 "Continue?" % dev.label())
        entry = Gtk.Entry(placeholder_text="text to print")
        entry.set_activates_default(True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("print", "Print")
        dlg.set_response_appearance(
            "print", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def on_resp(_d, resp: str) -> None:
            if resp != "print":
                return
            text = entry.get_text().strip()
            if not text:
                return
            self._launch_pret(dev, text=text)
        dlg.connect("response", on_resp)
        dlg.present()

    def _prompt_pret_image(self, dev: Device) -> None:
        """File chooser → PRET print <file>."""
        chooser = Gtk.FileDialog()
        chooser.set_title("Pick image or PDF")

        def on_file(_src, res, _user) -> None:
            try:
                f = chooser.open_finish(res)
            except Exception:  # noqa: BLE001
                return
            if f is None:
                return
            path = f.get_path()
            if path:
                self._launch_pret(dev, file_path=path)
        chooser.open(self.app_window, None, on_file, None)

    def _launch_pret(self, dev: Device,
                     text: str | None = None,
                     file_path: str | None = None) -> None:
        """Run ``pret.py TARGET pjl -q -i cmds.txt``.

        PRET is Python 2. We invoke it via ``python2`` on the phone (kali
        + pmOS both keep the interpreter around under that name for the
        legacy tools). If neither ``python2`` nor ``python2.7`` is
        available we fall back to ``python`` and hope for the best --
        the runner emits a diagnostic if it explodes.
        """
        import shlex
        import tempfile
        # Build the PRET command file. PRET reads it with `load` / -i.
        if text is not None:
            payload = 'print "%s"' % text.replace('"', "'")
        elif file_path is not None:
            payload = "print %s" % file_path
        else:
            return
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".pret", delete=False)
        tmp.write(payload + "\n")
        tmp.write("exit\n")
        tmp.close()

        cmd = ["python2", PRET_DIR + "/pret.py", "-q", "-i", tmp.name,
               dev.ip, "pjl"]
        self.output.append("$ %s\n" % " ".join(shlex.quote(c) for c in cmd))
        self._attack_task = self._submit(self._pret_stream(cmd))

    async def _pret_stream(self, cmd: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=PRET_DIR,
            )
        except FileNotFoundError:
            # Try `python` if python2 isn't there.
            cmd[0] = "python"
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=PRET_DIR,
                )
            except FileNotFoundError:
                self.output.append(
                    "[error] python2 not installed; "
                    "apt install python2 or run PRET manually\n")
                return
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except Exception as e:  # noqa: BLE001
                self.output.append("[read error] %s\n" % e)
                break
            if not line:
                break
            self.output.append(
                line.decode("utf-8", errors="replace"))
        rc = await proc.wait()
        self.output.append("[pret exited: %d]\n" % rc)

    def _run_pret_discover(self) -> None:
        """Run ``pret.py`` with no args -- lists local printers via SNMP.

        PRET's built-in discover mode broadcasts SNMP and emits every
        printer it finds with model + hostname. Handy when the arp-scan
        misses one (e.g. a printer with a static IP outside the DHCP
        pool).
        """
        self.output.append("# PRET SNMP discovery\n")
        cmd = ["python2", PRET_DIR + "/pret.py"]
        self._attack_task = self._submit(self._pret_stream(cmd))
