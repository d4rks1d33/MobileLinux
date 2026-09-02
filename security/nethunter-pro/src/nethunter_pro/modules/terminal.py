"""One-off command runner with live output and Stop (TerminalFragment)."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner


@register
class Terminal(NHModule):
    title = "Run Command"
    icon = "utilities-terminal-symbolic"
    description = "Run any shell command"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup()
        self.entry = Adw.EntryRow(title="Command")
        self.entry.connect("entry-activated", self._run)
        group.add(self.entry)
        self.as_root = Adw.SwitchRow(title="Run as root")
        group.add(self.as_root)
        box.append(group)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        run = Gtk.Button(label="Run")
        run.add_css_class("suggested-action")
        run.connect("clicked", self._run)
        buttons.append(run)
        term = Gtk.Button(label="Run in terminal")
        term.set_tooltip_text("For interactive commands (menus, prompts)")
        term.connect("clicked", self._run_terminal)
        buttons.append(term)
        box.append(buttons)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _run(self, _w) -> None:
        cmd = self.entry.get_text().strip()
        if cmd:
            self.runner.run(cmd, root=self.as_root.get_active())

    def _run_terminal(self, _w) -> None:
        cmd = self.entry.get_text().strip()
        if cmd:
            self.runner.run_in_terminal(cmd, root=self.as_root.get_active())
