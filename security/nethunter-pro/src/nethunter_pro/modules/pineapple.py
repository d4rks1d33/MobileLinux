"""WiFi Pineapple connector (PineappleFragment).

Sets up the interface bridging to talk to a Hak5 WiFi Pineapple over USB.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner


@register
class Pineapple(NHModule):
    title = "WiFi Pineapple"
    icon = "network-wired-symbolic"
    description = "Connect to a Hak5 WiFi Pineapple"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup(
            title="Pineapple",
            description="The Pineapple appears as a USB network interface; give "
            "it an address and route so its web UI is reachable.",
        )
        self.iface = Adw.EntryRow(title="Pineapple interface")
        self.iface.set_text("eth1")
        group.add(self.iface)
        self.addr = Adw.EntryRow(title="Host address")
        self.addr.set_text("172.16.42.42/24")
        group.add(self.addr)
        box.append(group)

        actions = Adw.PreferencesGroup()
        up = Adw.ActionRow(title="Bring up connection", subtitle="ip addr + up")
        b = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
        b.add_css_class("suggested-action")
        b.connect("clicked", self._connect)
        up.add_suffix(b)
        actions.add(up)
        box.append(actions)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _connect(self, _b: Gtk.Button) -> None:
        iface = self.iface.get_text().strip() or "eth1"
        addr = self.addr.get_text().strip()
        self.runner.run(
            f"sh -c 'ip addr add {addr} dev {iface}; ip link set {iface} up'",
            root=True,
        )
