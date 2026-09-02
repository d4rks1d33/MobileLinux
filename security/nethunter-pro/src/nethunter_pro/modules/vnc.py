"""VNC server control (VNCFragment)."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner


@register
class Vnc(NHModule):
    title = "VNC"
    icon = "preferences-desktop-remote-desktop-symbolic"
    description = "Start and stop a VNC server"
    required_tools = ["vncserver"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup(title="Server")
        self.display = Adw.EntryRow(title="Display")
        self.display.set_text(":1")
        group.add(self.display)
        self._row(group, "Start server", lambda: f"vncserver {self.display.get_text().strip()}",
                  "Start", suggested=True)
        self._row(group, "Stop server", lambda: f"vncserver -kill {self.display.get_text().strip()}",
                  "Stop")
        self._row(group, "List running", lambda: "vncserver -list", "List")
        box.append(group)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _row(self, group, title, cmd_fn, label, *, suggested=False):
        r = Adw.ActionRow(title=title)
        btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        if suggested:
            btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda _b: self.runner.run(cmd_fn()))
        r.add_suffix(btn)
        group.add(r)
