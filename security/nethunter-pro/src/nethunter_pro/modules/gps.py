"""Kali GPS service (KaliGpsServiceFragment).

Shares a GPS source over the network for tools that expect gpsd. The Android
version checked for nexmon; here it drives gpsd against a serial device.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner


@register
class Gps(NHModule):
    title = "GPS"
    icon = "find-location-symbolic"
    description = "Share a GPS device over gpsd"
    required_tools = ["gpsd"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup(title="gpsd")
        self.dev = Adw.EntryRow(title="Serial device")
        self.dev.set_text("/dev/ttyUSB0")
        group.add(self.dev)
        self._row(group, "Start gpsd", "gpsd on the device above",
                  lambda: f"gpsd -N -n {self.dev.get_text().strip()}",
                  "Start", suggested=True)
        self._row(group, "Live location", "gpspipe -w (press Stop to end)",
                  lambda: "gpspipe -w", "Watch")
        box.append(group)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _row(self, group, title, subtitle, cmd_fn, label, *, suggested=False):
        r = Adw.ActionRow(title=title, subtitle=subtitle)
        btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        if suggested:
            btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda _b: self.runner.run(cmd_fn(), root=True))
        r.add_suffix(btn)
        group.add(r)
