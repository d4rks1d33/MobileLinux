"""Port-specific controls that the Android app has no equivalent for.

This device has a single USB-C port and no mainline Type-C role driver, so the
port's `otg` helper switches between charging and USB host with VBUS boost.
Monitor mode is offered for the external USB adapter, since the internal
WCN3990 cannot do it.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..executor import Result, run_async, which
from ..module import NHModule, register
from ..widgets import OutputView, toast


@register
class NetControl(NHModule):
    title = "USB and Radio"
    icon = "media-playback-start-symbolic"
    description = "OTG power and monitor mode (rhodep-specific)"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ["NetworkManager"]))

        otg = Adw.PreferencesGroup(
            title="USB-C role",
            description="Single port: charge or host, not both.",
        )
        self.otg_row = Adw.SwitchRow(
            title="USB host (OTG) + VBUS", subtitle="Off = charging"
        )
        self.otg_row.connect("notify::active", self._on_otg)
        otg.add(self.otg_row)
        status_btn = Gtk.Button(label="Status", valign=Gtk.Align.CENTER)
        status_btn.connect("clicked", lambda _b: self._otg_status())
        status_row = Adw.ActionRow(title="OTG status")
        status_row.add_suffix(status_btn)
        otg.add(status_row)
        box.append(otg)

        mon = Adw.PreferencesGroup(
            title="Monitor mode",
            description="For the external USB adapter (wlan1); the internal "
            "radio cannot do this.",
        )
        self.iface = Adw.EntryRow(title="Interface")
        self.iface.set_text("wlan1")
        mon.add(self.iface)
        start = Adw.ActionRow(title="Start monitor mode", subtitle="airmon-ng start")
        sb = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        sb.add_css_class("suggested-action")
        sb.connect("clicked", lambda _b: self._airmon("start"))
        start.add_suffix(sb)
        mon.add(start)
        stop = Adw.ActionRow(title="Stop monitor mode", subtitle="airmon-ng stop")
        xb = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        xb.connect("clicked", lambda _b: self._airmon("stop"))
        stop.add_suffix(xb)
        mon.add(stop)
        box.append(mon)

        self.output = OutputView()
        box.append(self.output)
        self._otg_status()
        return box

    def _run(self, cmd, root=True):
        self.output.append(f"$ {cmd if isinstance(cmd, str) else ' '.join(cmd)}\n")

        def done(result: Result) -> None:
            self.output.append(result.stdout or "")
            self.output.append(result.stderr or "")
            self.output.append(f"[exit {result.returncode}]\n\n")

        run_async(cmd, done, root=root, timeout=30)

    def _on_otg(self, row: Adw.SwitchRow, _param) -> None:
        self._run(["otg", "on" if row.get_active() else "off"])

    def _otg_status(self) -> None:
        def done(result: Result) -> None:
            self.output.append("$ otg status\n")
            self.output.append(result.stdout or result.stderr or "")
            self.output.append("\n")

        run_async(["otg", "status"], done, root=True, timeout=15)

    def _airmon(self, action: str) -> None:
        iface = self.iface.get_text().strip() or "wlan1"
        self._run(["airmon-ng", action, iface])
