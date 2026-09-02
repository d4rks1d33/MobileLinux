"""BlueDucky Bluetooth HID attack (/opt/BlueDucky).

Builds a BlueDucky command from the GUI with sensible defaults, then runs it.
With a target MAC and a payload it is non-interactive; without a target it can
auto-pick the first device found. Payloads are discovered from the payloads/
folder at runtime.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..executor import Result, run_async
from ..module import NHModule, register
from ..widgets import ToolRunner

BLUEDUCKY_DIR = "/opt/BlueDucky"
BLUEDUCKY = f"python3 {BLUEDUCKY_DIR}/BlueDucky.py"
LAYOUTS = ["us", "es", "de", "fr", "it", "pt-br", "uk", "ru"]


@register
class BlueDucky(NHModule):
    title = "BlueDucky"
    icon = "bluetooth-symbolic"
    description = "Bluetooth HID injection attack"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ["bluetooth"]))

        target = Adw.PreferencesGroup(
            title="Target",
            description="Set a MAC for a direct attack, or auto-target to pick "
            "the first device found.",
        )
        self.mac = Adw.EntryRow(title="Target MAC (blank = choose/auto)")
        target.add(self.mac)
        self.auto = Adw.SwitchRow(
            title="Auto-target", subtitle="--auto-target: scan and pick the first")
        target.add(self.auto)
        self.adapter = Adw.EntryRow(title="Adapter (blank = auto-detect)")
        target.add(self.adapter)
        box.append(target)

        payload = Adw.PreferencesGroup(title="Payload")
        self.payload = Adw.ComboRow(title="Payload")
        self.payload_model = Gtk.StringList()
        self.payload.set_model(self.payload_model)
        payload.add(self.payload)
        refresh = Adw.ActionRow(title="Refresh payloads", subtitle=f"{BLUEDUCKY_DIR}/payloads")
        rb = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        rb.connect("clicked", lambda _b: self._load_payloads())
        refresh.add_suffix(rb)
        payload.add(refresh)
        box.append(payload)

        mode = Adw.PreferencesGroup(
            title="Delivery mode",
            description="Over Bluetooth by default, or as a wired USB HID "
            "keyboard with --usb (no Bluetooth, uses the USB gadget).",
        )
        self.usb = Adw.SwitchRow(
            title="USB gadget mode (--usb)",
            subtitle="Phone acts as a USB HID keyboard; needs USB device mode")
        mode.add(self.usb)
        self.layout = Adw.ComboRow(
            title="Host keyboard layout (--layout)",
            model=Gtk.StringList.new(LAYOUTS),
        )
        mode.add(self.layout)
        box.append(mode)

        opts = Adw.PreferencesGroup(title="Options")
        self.yes = Adw.SwitchRow(
            title="Non-interactive", subtitle="-y: no confirmation prompts")
        self.yes.set_active(True)
        opts.add(self.yes)
        self.windows = Adw.SwitchRow(
            title="Windows target (--btk)",
            subtitle="btk_server SDP record and report layout")
        opts.add(self.windows)
        self.listen = Adw.SwitchRow(
            title="Host-initiated (--listen)",
            subtitle="Advertise a keyboard and wait for the host (Windows)")
        opts.add(self.listen)
        self.no_ssp = Adw.SwitchRow(
            title="Keep SSP", subtitle="--no-ssp: do not force SSP off")
        opts.add(self.no_ssp)
        self.unpair = Adw.SwitchRow(
            title="Unpair after run", subtitle="--unpair")
        opts.add(self.unpair)
        self.name = Adw.EntryRow(title="Adapter alias/name (--name, optional)")
        opts.add(self.name)
        box.append(opts)

        actions = Adw.PreferencesGroup()
        run = Adw.ActionRow(title="Run BlueDucky", subtitle="In-app, with live output")
        rb2 = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
        rb2.add_css_class("suggested-action")
        rb2.connect("clicked", lambda _b: self._run(terminal=False))
        run.add_suffix(rb2)
        actions.add(run)
        term = Adw.ActionRow(title="Run in terminal",
                             subtitle="For the interactive target menu")
        tb = Gtk.Button(label="Terminal", valign=Gtk.Align.CENTER)
        tb.connect("clicked", lambda _b: self._run(terminal=True))
        term.add_suffix(tb)
        actions.add(term)
        box.append(actions)

        self.runner = ToolRunner()
        box.append(self.runner)

        self._load_payloads()
        return box

    def _load_payloads(self) -> None:
        def done(result: Result) -> None:
            self.payload_model.splice(0, self.payload_model.get_n_items(), None)
            self.payload_model.append("(none)")
            for name in sorted(result.stdout.split()):
                if name:
                    self.payload_model.append(name)

        run_async(
            ["sh", "-c", f"ls -1 {BLUEDUCKY_DIR}/payloads 2>/dev/null"],
            done, timeout=10,
        )

    def _build_cmd(self) -> list[str]:
        cmd = BLUEDUCKY.split()
        mac = self.mac.get_text().strip()
        if mac:
            cmd += ["-t", mac]
        if self.auto.get_active():
            cmd.append("--auto-target")
        if self.adapter.get_text().strip():
            cmd += ["--adapter", self.adapter.get_text().strip()]
        pidx = self.payload.get_selected()
        if pidx > 0:
            item = self.payload_model.get_item(pidx)
            if item:
                cmd += ["-p", item.get_string()]
        if self.usb.get_active():
            cmd.append("--usb")
        cmd += ["--layout", LAYOUTS[self.layout.get_selected()]]
        if self.name.get_text().strip():
            cmd += ["--name", self.name.get_text().strip()]
        if self.yes.get_active():
            cmd.append("-y")
        if self.windows.get_active():
            cmd.append("--btk")
        if self.listen.get_active():
            cmd.append("--listen")
        if self.no_ssp.get_active():
            cmd.append("--no-ssp")
        if self.unpair.get_active():
            cmd.append("--unpair")
        return cmd

    def _run(self, *, terminal: bool) -> None:
        cmd = self._build_cmd()
        if terminal:
            self.runner.run_in_terminal(cmd, root=True)
        else:
            self.runner.run(cmd, root=True)
