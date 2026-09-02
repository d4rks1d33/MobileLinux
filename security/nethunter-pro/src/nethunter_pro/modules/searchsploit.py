"""Search Exploit-DB from the command line (SearchSploitFragment)."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..executor import Result, run_async
from ..module import NHModule, register
from ..widgets import OutputView


@register
class SearchSploit(NHModule):
    title = "SearchSploit"
    icon = "system-search-symbolic"
    description = "Search the local Exploit-DB copy"
    required_tools = ["searchsploit"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup()
        self.term = Adw.EntryRow(title="Search terms")
        group.add(self.term)
        box.append(group)

        btn = Gtk.Button(label="Search", halign=Gtk.Align.START)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self._on_search)
        box.append(btn)

        self.output = OutputView()
        box.append(self.output)
        return box

    def _on_search(self, _b: Gtk.Button) -> None:
        term = self.term.get_text().strip()
        if not term:
            return
        self.output.clear()
        self.output.append(f"$ searchsploit {term}\n")

        def done(result: Result) -> None:
            self.output.append(result.stdout or result.stderr)

        run_async(["searchsploit", *term.split()], done, timeout=60)
