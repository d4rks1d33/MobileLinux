"""USB Arsenal (USBArsenalFragment): USB gadget functions over configfs.

Switches what the phone presents to a host it is plugged into: HID keyboard,
mass storage, RNDIS network. On NetHunter Pro the gadget is driven through
configfs; this needs the port in USB device mode (USB and Radio: OTG off).
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner

GADGET = "/sys/kernel/config/usb_gadget/g1"


@register
class UsbArsenal(NHModule):
    title = "USB Arsenal"
    icon = "drive-harddisk-usb-symbolic"
    description = "Choose what the USB gadget presents to a host"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        box.append(Adw.Banner(
            title="Needs USB device mode (USB and Radio: OTG off)",
            revealed=True,
        ))

        status = Adw.PreferencesGroup(title="Gadget")
        self._row(status, "Show current functions", "list the configured gadget",
                  f"sh -c 'ls {GADGET}/functions 2>/dev/null; "
                  f"echo; echo UDC:; cat {GADGET}/UDC 2>/dev/null'", "Show")
        box.append(status)

        storage = Adw.PreferencesGroup(
            title="Mass storage",
            description="Expose an image file as a USB drive to the host.",
        )
        self.image = Adw.EntryRow(title="Backing image path")
        self.image.set_text("/root/usb_storage.img")
        storage.add(self.image)
        self._row(storage, "Attach mass storage",
                  "add a mass_storage function backed by the image",
                  lambda: self._mass_storage_cmd(), "Attach", suggested=True)
        box.append(storage)

        net = Adw.PreferencesGroup(
            title="Network (RNDIS)",
            description="Present a USB network interface to the host.",
        )
        self._row(net, "Enable RNDIS", "share networking over USB",
                  self._rndis_cmd, "Enable")
        box.append(net)

        reset = Adw.PreferencesGroup()
        self._row(reset, "Reset gadget", "unbind and clear functions",
                  f"sh -c 'echo > {GADGET}/UDC 2>/dev/null; echo reset'", "Reset")
        box.append(reset)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _row(self, group, title, subtitle, cmd, label, *, suggested=False):
        r = Adw.ActionRow(title=title, subtitle=subtitle)
        btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        if suggested:
            btn.add_css_class("suggested-action")
        resolved = (lambda: cmd) if isinstance(cmd, str) else cmd
        btn.connect("clicked", lambda _b: self.runner.run(resolved(), root=True))
        r.add_suffix(btn)
        group.add(r)

    def _mass_storage_cmd(self) -> str:
        img = self.image.get_text().strip()
        return (
            f"sh -c 'cd {GADGET} && "
            f"mkdir -p functions/mass_storage.0 && "
            f"echo {img} > functions/mass_storage.0/lun.0/file && "
            f"ln -sf functions/mass_storage.0 configs/c.1/ && "
            f"ls /sys/class/udc > UDC'"
        )

    def _rndis_cmd(self) -> str:
        return (
            f"sh -c 'cd {GADGET} && "
            f"mkdir -p functions/rndis.0 && "
            f"ln -sf functions/rndis.0 configs/c.1/ && "
            f"ls /sys/class/udc > UDC'"
        )
