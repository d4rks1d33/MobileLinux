"""Saved custom commands (CustomCommandsFragment).

The Android app let you store frequently used commands. Here they persist to a
small JSON file in the user's config dir and run with live output.
"""
from __future__ import annotations

import json
from pathlib import Path

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner

STORE = Path.home() / ".config" / "nethunter-pro" / "commands.json"


@register
class CustomCommands(NHModule):
    title = "Custom Commands"
    icon = "starred-symbolic"
    description = "Save and run your own commands"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        add = Adw.PreferencesGroup(title="Add a command")
        self.name = Adw.EntryRow(title="Name")
        add.add(self.name)
        self.cmd = Adw.EntryRow(title="Command")
        add.add(self.cmd)
        self.root = Adw.SwitchRow(title="Run as root")
        add.add(self.root)
        addrow = Adw.ActionRow(title="Save")
        ab = Gtk.Button(label="Save", valign=Gtk.Align.CENTER)
        ab.add_css_class("suggested-action")
        ab.connect("clicked", self._save)
        addrow.add_suffix(ab)
        add.add(addrow)
        box.append(add)

        self.saved_group = Adw.PreferencesGroup(title="Saved")
        box.append(self.saved_group)

        self.runner = ToolRunner()
        box.append(self.runner)

        self._reload()
        return box

    def _load(self) -> list[dict]:
        if STORE.exists():
            try:
                return json.loads(STORE.read_text())
            except Exception:
                return []
        return []

    def _write(self, items: list[dict]) -> None:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(items, indent=2))

    def _save(self, _b: Gtk.Button) -> None:
        name = self.name.get_text().strip()
        cmd = self.cmd.get_text().strip()
        if not name or not cmd:
            return
        items = self._load()
        items.append({"name": name, "cmd": cmd, "root": self.root.get_active()})
        self._write(items)
        self.name.set_text("")
        self.cmd.set_text("")
        self._reload()

    def _reload(self) -> None:
        child = self.saved_group.get_first_child()
        # Clear existing rows (keep the group header).
        rows = []
        item = self.saved_group
        # Rebuild from scratch by removing known rows we added.
        for entry in getattr(self, "_rows", []):
            self.saved_group.remove(entry)
        self._rows = []
        for it in self._load():
            row = Adw.ActionRow(
                title=it["name"],
                subtitle=("# " if it["root"] else "$ ") + it["cmd"],
            )
            run = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
            run.connect("clicked", lambda _b, i=it: self.runner.run(i["cmd"], root=i["root"]))
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _b, i=it: self._delete(i))
            row.add_suffix(run)
            row.add_suffix(delete)
            self.saved_group.add(row)
            self._rows.append(row)

    def _delete(self, item: dict) -> None:
        items = [i for i in self._load() if i != item]
        self._write(items)
        self._reload()
