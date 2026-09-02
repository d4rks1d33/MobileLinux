"""Kernel and system info (KernelFragment, trimmed to what applies)."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..executor import Result, run_async
from ..module import NHModule, register
from ..widgets import OutputView


@register
class SysInfo(NHModule):
    title = "System Info"
    icon = "computer-symbolic"
    description = "Kernel, uptime and hardware overview"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup()
        for label, cmd in (
            ("Kernel", ["uname", "-a"]),
            ("Distribution", ["sh", "-c", ". /etc/os-release; echo $PRETTY_NAME"]),
            ("Uptime", ["uptime", "-p"]),
        ):
            row = Adw.ActionRow(title=label, subtitle="…")
            group.add(row)

            def done(result: Result, r=row) -> None:
                r.set_subtitle(result.stdout.strip() or result.stderr.strip())

            run_async(cmd, done, timeout=10)
        box.append(group)

        self.output = OutputView()
        for label, argv in (
            ("Network interfaces", ["ip", "-brief", "addr"]),
            ("USB devices", ["lsusb"]),
            ("Block devices", ["lsblk"]),
        ):
            btn = Adw.ActionRow(title=label)
            b = Gtk.Button(label="Show", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _w, a=argv, t=label: self._show(t, a))
            btn.add_suffix(b)
            group.add(btn)
        box.append(self.output)
        return box

    def _show(self, title, argv) -> None:
        self.output.append(f"=== {title} ===\n")

        def done(result: Result) -> None:
            self.output.append(result.stdout or result.stderr)
            self.output.append("\n")

        run_async(argv, done, timeout=15)
