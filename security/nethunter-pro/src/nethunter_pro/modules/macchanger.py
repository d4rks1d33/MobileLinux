"""Change a network interface's MAC address.

Direct translation of MacchangerFragment: it read /sys/class/net for the
interface list and current address and shelled out to macchanger. Same here,
minus the Android net.hostname bits which do not apply.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..executor import Result, run_async, which
from ..module import NHModule, register
from ..widgets import OutputView, toast


@register
class Macchanger(NHModule):
    title = "MAC Changer"
    icon = "network-wired-symbolic"
    description = "Randomise or set a network interface MAC"
    required_tools = ["macchanger"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup(title="Interface")
        self.iface_row = Adw.ComboRow(title="Interface")
        self.iface_model = Gtk.StringList()
        self.iface_row.set_model(self.iface_model)
        self.iface_row.connect("notify::selected", lambda *_: self._show_current())
        group.add(self.iface_row)

        self.current = Adw.ActionRow(title="Current MAC", subtitle="—")
        group.add(self.current)
        box.append(group)

        actions = Adw.PreferencesGroup()
        random_row = Adw.ActionRow(
            title="Random MAC", subtitle="Assign a fully random address"
        )
        rb = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        rb.add_css_class("suggested-action")
        rb.connect("clicked", lambda _b: self._change("-r"))
        random_row.add_suffix(rb)
        actions.add(random_row)

        reset_row = Adw.ActionRow(
            title="Reset MAC", subtitle="Restore the permanent hardware address"
        )
        eb = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        eb.connect("clicked", lambda _b: self._change("-p"))
        reset_row.add_suffix(eb)
        actions.add(reset_row)
        box.append(actions)

        self.output = OutputView()
        box.append(self.output)

        self._load_interfaces()
        return box

    def _load_interfaces(self) -> None:
        def done(result: Result) -> None:
            for name in result.stdout.split():
                if name != "lo":
                    self.iface_model.append(name)
            self._show_current()

        run_async(["ls", "/sys/class/net"], done, timeout=10)

    def _selected_iface(self) -> str | None:
        idx = self.iface_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION:
            return None
        item = self.iface_model.get_item(idx)
        return item.get_string() if item else None

    def _show_current(self) -> None:
        iface = self._selected_iface()
        if not iface:
            return

        def done(result: Result) -> None:
            self.current.set_subtitle(result.stdout.strip() or "—")

        run_async(
            ["cat", f"/sys/class/net/{iface}/address"], done, timeout=10
        )

    def _change(self, flag: str) -> None:
        iface = self._selected_iface()
        if not iface:
            toast(self.app_window, "Pick an interface first")
            return
        # macchanger needs the link down; bring it down, change, bring it up.
        cmd = (
            f"sh -c 'ip link set {iface} down && "
            f"macchanger {flag} {iface}; ip link set {iface} up'"
        )
        self.output.append(f"$ macchanger {flag} {iface}\n")

        def done(result: Result) -> None:
            self.output.append(result.stdout or "")
            self.output.append(result.stderr or "")
            self.output.append(f"[exit {result.returncode}]\n\n")
            self._show_current()

        run_async(cmd, done, root=True, timeout=30)
