"""HID keyboard injection (HidFragment): type text into a connected host.

Uses the bundled HID writer (nethunter-pro-hid), so it does not depend on any
external tool. Needs USB device mode and a HID gadget at /dev/hidg0.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner

HID_DEVICE = "/dev/hidg0"
HID_TOOL = "/usr/libexec/nethunter-pro-hid"


@register
class Hid(NHModule):
    title = "HID Keyboard"
    icon = "input-keyboard-symbolic"
    description = "Type text into a connected host"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        box.append(Adw.Banner(
            title=f"Needs USB device mode and a HID gadget at {HID_DEVICE}",
            revealed=True,
        ))

        group = Adw.PreferencesGroup(title="Type text")
        self.device = Adw.EntryRow(title="HID device")
        self.device.set_text(HID_DEVICE)
        group.add(self.device)
        self.text = Adw.EntryRow(title="Text to type on the host")
        group.add(self.text)
        row = Adw.ActionRow(title="Send keystrokes")
        b = Gtk.Button(label="Type", valign=Gtk.Align.CENTER)
        b.add_css_class("suggested-action")
        b.connect("clicked", self._type)
        row.add_suffix(b)
        group.add(row)
        box.append(group)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _type(self, _b: Gtk.Button) -> None:
        text = self.text.get_text()
        if not text:
            return
        dev = self.device.get_text().strip() or HID_DEVICE
        self.runner.run([HID_TOOL, dev, "text", text], root=True)
