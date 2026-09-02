"""CAN bus arsenal (CARsenalFragment) - full can-utils toolset."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner


@register
class CARsenal(NHModule):
    title = "CARsenal"
    icon = "nethunter-carsenal-symbolic"
    description = "CAN bus tools (can-utils)"
    required_tools = ["candump"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        iface_group = Adw.PreferencesGroup(title="CAN interface")
        self.iface = Adw.EntryRow(title="Interface")
        self.iface.set_text("can0")
        iface_group.add(self.iface)
        self.bitrate = Adw.EntryRow(title="Bitrate (for bring-up)")
        self.bitrate.set_text("500000")
        iface_group.add(self.bitrate)
        box.append(iface_group)

        # Interface bring-up, from the slcan/ip link parts of the original.
        setup = Adw.PreferencesGroup(title="Interface")
        self._action(setup, "Bring up", "Set bitrate and bring the link up",
                     lambda: f"ip link set {self._if()} up type can bitrate {self._br()}",
                     "Up", root=True, suggested=True)
        self._action(setup, "Bring down", "Take the link down",
                     lambda: f"ip link set {self._if()} down", "Down", root=True)
        self._action(setup, "Details", "ip -details link",
                     lambda: f"ip -details link show {self._if()}", "Info")
        box.append(setup)

        # Traffic tools.
        traffic = Adw.PreferencesGroup(title="Traffic")
        self._action(traffic, "Dump", "candump (live; press Stop to end)",
                     lambda: f"candump {self._if()}", "Dump", root=True, suggested=True)
        # cansniffer is a full-screen interactive view, so it opens in a
        # terminal; the rest stream in-app.
        sniff = Adw.ActionRow(title="Sniffer",
                              subtitle="cansniffer (interactive, opens a terminal)")
        sb = Gtk.Button(label="Sniff", valign=Gtk.Align.CENTER)
        sb.connect("clicked", lambda _b: self.runner.run_in_terminal(
            f"cansniffer {self._if()}", root=True))
        sniff.add_suffix(sb)
        traffic.add(sniff)
        self._action(traffic, "Generate traffic", "cangen (random frames)",
                     lambda: f"cangen {self._if()} -g 10", "Gen", root=True)
        box.append(traffic)

        # Send a specific frame.
        send = Adw.PreferencesGroup(title="Send frame")
        self.frame = Adw.EntryRow(title="Frame (e.g. 123#DEADBEEF)")
        send.add(self.frame)
        srow = Adw.ActionRow(title="Send", subtitle="cansend")
        sbtn = Gtk.Button(label="Send", valign=Gtk.Align.CENTER)
        sbtn.add_css_class("suggested-action")
        sbtn.connect("clicked", lambda _b: self.runner.run(
            f"cansend {self._if()} {self.frame.get_text().strip()}", root=True))
        srow.add_suffix(sbtn)
        send.add(srow)
        box.append(send)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _if(self) -> str:
        return self.iface.get_text().strip() or "can0"

    def _br(self) -> str:
        return self.bitrate.get_text().strip() or "500000"

    def _action(self, group, title, subtitle, cmd_fn, label, *, root=False, suggested=False):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        if suggested:
            btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda _b: self.runner.run(cmd_fn(), root=root))
        row.add_suffix(btn)
        group.add(row)
