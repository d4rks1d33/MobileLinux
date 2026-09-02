"""DuckHunter / BadUSB (DuckHunterFragment, BadusbFragment).

Runs a DuckyScript against a connected host using the bundled HID writer.
Needs USB device mode and a HID gadget at /dev/hidg0.
"""
from __future__ import annotations

import base64

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner

HID_DEVICE = "/dev/hidg0"
HID_TOOL = "/usr/libexec/nethunter-pro-hid"

EXAMPLE = """REM Example: open a terminal and echo a message
DELAY 500
GUI r
DELAY 300
STRING notepad
ENTER
DELAY 500
STRING Hello from NetHunter Pro
"""


@register
class DuckHunter(NHModule):
    title = "DuckHunter (BadUSB)"
    icon = "input-tablet-symbolic"
    description = "Run a DuckyScript against a connected host"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        box.append(Adw.Banner(
            title=f"Needs USB device mode and a HID gadget at {HID_DEVICE}",
            revealed=True,
        ))

        group = Adw.PreferencesGroup(title="DuckyScript")
        self.device = Adw.EntryRow(title="HID device")
        self.device.set_text(HID_DEVICE)
        group.add(self.device)
        box.append(group)

        self.script = Gtk.TextView(monospace=True, top_margin=6, bottom_margin=6,
                                   left_margin=6, right_margin=6,
                                   wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.script.get_buffer().set_text(EXAMPLE)
        sc = Gtk.ScrolledWindow(child=self.script, min_content_height=180)
        sc.add_css_class("card")
        box.append(sc)

        run = Gtk.Button(label="Run payload", halign=Gtk.Align.START)
        run.add_css_class("suggested-action")
        run.connect("clicked", self._run)
        box.append(run)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _run(self, _b: Gtk.Button) -> None:
        buf = self.script.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        dev = self.device.get_text().strip() or HID_DEVICE
        b64 = base64.b64encode(text.encode()).decode()
        # Decode the script and pipe it into the HID writer's ducky mode.
        cmd = f"sh -c 'echo {b64} | base64 -d | {HID_TOOL} {dev} ducky'"
        self.runner.run(cmd, root=True)
