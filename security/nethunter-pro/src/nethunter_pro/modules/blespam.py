"""Bluetooth LE Spam -- port of the Android tool of the same name.

Floods the air with phantom BLE advertisements (Apple AirPods pop-ups,
iOS 17 crash payload, Samsung Easy Setup, Google Fast Pair, Lovespouse
toys...). The advertisement generator (747 packets across 14 categories)
is ported byte-for-byte from the Android sources; see
:mod:`nethunter_pro.vendor.blespam.payloads` for the actual builders.

The engine runs as a separate root process, invoked through the DBus
helper. Talking to the Bluetooth controller means opening a raw HCI
*user channel*, which needs CAP_NET_RAW *and* wants bluetoothd out of
the way -- both are things the GUI itself cannot do. The runner script
at ``/usr/libexec/nethunter-pro-blespam`` does the radio dance, drives
the SpamEngine, and streams state / packet / error events as newline
JSON so this module can render them live.

The 747-entry list would kill a naive Adw.PreferencesGroup -- there's
one factory-created ExpanderRow per packet is fine on the desktop but
2s of jank on the phone. Instead we render it with Gtk.ListView + a
Gio.ListStore backing model + a SignalListItemFactory, which is the
GTK4 way to do virtualised long lists.
"""
from __future__ import annotations

import json
import os

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..executor import Process
from ..module import NHModule, register
from ..vendor.blespam import payloads
from ..vendor.blespam.engine import (
    DEFAULT_TX_POWER,
    MODE_RANDOM,
    MODE_SEQUENTIAL,
    TX_POWER_BYTES,
)
from ..widgets import OutputView, services_banner, toast

RUNNER = "/usr/libexec/nethunter-pro-blespam"
RECOVER = "/usr/libexec/nethunter-pro-blespam-recover"

# Advertising interval controls how often the radio retransmits the
# currently-loaded payload. 100 ms is the sweet spot -- fast enough for
# iOS/Windows pop-ups to notice, slow enough not to melt the chip.
ADV_INTERVAL_DEFAULT_MS = 100
ADV_INTERVAL_MIN_MS = 20
ADV_INTERVAL_MAX_MS = 1000

# Payload change rate is how often the host asks the chip to swap in a
# new payload. This is where the wedges happened -- the original port
# had it locked to the advertising interval (~20 ms) and the QCA chip
# gave up after a few thousand swaps. 200 ms (5 Hz) matches the pattern
# the Android app uses.
PAYLOAD_CHANGE_DEFAULT_MS = 200
PAYLOAD_CHANGE_MIN_MS = 100
PAYLOAD_CHANGE_MAX_MS = 5000


def _list_adapters() -> list[str]:
    """Return available hciN names sorted by id (['hci0', 'hci1', ...])."""
    names = []
    try:
        for name in os.listdir("/sys/class/bluetooth"):
            if name.startswith("hci") and name[3:].isdigit():
                names.append(name)
    except OSError:
        pass
    return sorted(names, key=lambda n: int(n[3:]))


class PacketItem(GObject.Object):
    """One row in the packet list, wrapping a payloads.Packet.

    Gtk.SignalListItemFactory hands us the same PacketItem for a row
    every time the row is recycled during scroll, so mutating .checked
    directly is fine -- the factory's bind step will re-render it.
    """
    __gtype_name__ = "BlespamPacketItem"

    title = GObject.Property(type=str)
    category = GObject.Property(type=str)
    hex_preview = GObject.Property(type=str)
    checked = GObject.Property(type=bool, default=False)

    def __init__(self, packet):
        super().__init__()
        self._packet = packet
        # A stable preview: render once with the default TX byte. The
        # engine will re-render with fresh randomness at send time.
        adv, _scan = packet.render(0x06)
        self.title = packet.title
        self.category = "?"
        self.hex_preview = adv.hex(" ").upper()

    @property
    def packet(self):
        return self._packet


@register
class Blespam(NHModule):
    title = "Bluetooth LE Spam"
    icon = "bluetooth-symbolic"
    description = "Flood the air with phantom BLE ads (AirPods, Fast Pair, iOS 17, Samsung Easy Setup)"
    # bluetoothctl / hciconfig / rfkill are the userspace pieces the
    # runner depends on. The engine talks to the kernel directly, so
    # there is no python library requirement to check here.
    required_tools = ["hciconfig", "rfkill"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        # Two stores: the master with every packet, and a filter that
        # is what the ListView actually shows. Category dropdown +
        # search entry both write to the filter.
        self._all_items: Gio.ListStore | None = None
        self._filter_model: Gtk.FilterListModel | None = None
        self._filter: Gtk.CustomFilter | None = None
        self._search_text = ""
        self._active_category = "All"
        self._categories: list[tuple[str, list]] = []

    # -------------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['bluetooth'],
            subtitle_extra='This module needs bluetooth.service OFF -- it uses a user-channel HCI socket. Flip the switch to inactive in Kali Services before starting.'))

        # ---- radio + engine options ------------------------------
        opts = Adw.PreferencesGroup(
            title="Radio & engine",
            description="hci0 is the phone's internal controller; hci1+ "
                        "come from any USB / OTG Bluetooth dongle plugged "
                        "in. The internal QCA chip needs a firmware dance "
                        "so preparing hci0 takes ~5 s.")

        self.adapter_combo = Adw.ComboRow(title="Adapter")
        adapters = _list_adapters() or ["hci0"]
        self.adapter_combo.set_model(Gtk.StringList.new(adapters))
        opts.add(self.adapter_combo)

        self.interval = Adw.SpinRow.new_with_range(
            ADV_INTERVAL_MIN_MS, ADV_INTERVAL_MAX_MS, 10)
        self.interval.set_title("Advertising interval (ms)")
        self.interval.set_subtitle(
            "How often the radio re-transmits the loaded payload; "
            "HCI unit 0.625 ms; minimum 20 ms")
        self.interval.set_value(ADV_INTERVAL_DEFAULT_MS)
        opts.add(self.interval)

        # Second knob added after the wedge investigation: how often the
        # host swaps the payload. See engine.py:MIN_PAYLOAD_CHANGE_MS
        # -- <100 ms is where the QCA chip starts wedging. The engine
        # will clamp anyway, but making the field respect the same
        # floor stops the user from thinking they set it lower.
        self.payload_change = Adw.SpinRow.new_with_range(
            PAYLOAD_CHANGE_MIN_MS, PAYLOAD_CHANGE_MAX_MS, 50)
        self.payload_change.set_title("Payload change rate (ms)")
        self.payload_change.set_subtitle(
            "How often the host swaps to the next payload; keep >=100 "
            "to prevent the QCA controller from wedging")
        self.payload_change.set_value(PAYLOAD_CHANGE_DEFAULT_MS)
        opts.add(self.payload_change)

        self.mode = Adw.ComboRow(title="Order")
        self.mode.set_model(Gtk.StringList.new(
            [MODE_SEQUENTIAL, MODE_RANDOM]))
        opts.add(self.mode)

        self.tx = Adw.ComboRow(title="TX power")
        # Order matches list(TX_POWER_BYTES): High/Medium/Low/Ultra Low
        # but presenting from High to Ultra Low is what people expect.
        tx_names = list(TX_POWER_BYTES.keys())
        self.tx.set_model(Gtk.StringList.new(tx_names))
        self.tx.set_selected(tx_names.index(DEFAULT_TX_POWER))
        opts.add(self.tx)

        # Start / Stop
        actions = Adw.ActionRow(
            title="Run",
            subtitle="Preparing the radio takes ~5s the first time")
        self.start_btn = Gtk.Button(
            label="Start", valign=Gtk.Align.CENTER)
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", lambda _b: self._start())
        actions.add_suffix(self.start_btn)
        self.stop_btn = Gtk.Button(
            label="Stop", valign=Gtk.Align.CENTER)
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        actions.add_suffix(self.stop_btn)
        opts.add(actions)

        # Recovery row: if the chip is wedged (Operation not supported
        # 95, or after a hardware error the engine could not recover
        # from), this button runs the serdev unbind/bind dance. That's
        # the one thing short of a reboot that reliably brings the QCA
        # WCN399x back. See helper/blespam-recover for the details.
        recover_row = Adw.ActionRow(
            title="Recover radio",
            subtitle="Unbind/rebind the Bluetooth serdev driver -- use "
                     "if Start fails with 'Operation not supported'")
        self.recover_btn = Gtk.Button(
            label="Recover", valign=Gtk.Align.CENTER)
        self.recover_btn.connect("clicked", lambda _b: self._recover())
        recover_row.add_suffix(self.recover_btn)
        opts.add(recover_row)
        box.append(opts)

        # ---- packet filter / selection helpers -------------------
        filt_group = Adw.PreferencesGroup(
            title="Packets",
            description="Tick which advertisements to cycle. There are "
                        "747 total; use the category and search to narrow.")

        cats = ["All"] + [t for t, _p in payloads.build_categories()]
        self.cat_combo = Adw.ComboRow(title="Category")
        self.cat_combo.set_model(Gtk.StringList.new(cats))
        self.cat_combo.connect("notify::selected",
                               lambda *_: self._on_filter_change())
        filt_group.add(self.cat_combo)

        self.search = Adw.EntryRow(title="Search title")
        self.search.connect("changed",
                            lambda *_: self._on_filter_change())
        filt_group.add(self.search)

        # Check-all / clear-all + count row
        sel_row = Adw.ActionRow(title="Selection")
        check_btn = Gtk.Button(
            label="Check visible", valign=Gtk.Align.CENTER)
        check_btn.connect("clicked",
                         lambda _b: self._toggle_visible(True))
        sel_row.add_suffix(check_btn)
        clear_btn = Gtk.Button(
            label="Clear visible", valign=Gtk.Align.CENTER)
        clear_btn.connect("clicked",
                          lambda _b: self._toggle_visible(False))
        sel_row.add_suffix(clear_btn)
        clear_all_btn = Gtk.Button(
            label="Clear all", valign=Gtk.Align.CENTER)
        clear_all_btn.connect("clicked",
                              lambda _b: self._clear_all())
        sel_row.add_suffix(clear_all_btn)
        filt_group.add(sel_row)

        # Little summary line above the list.
        self.count_row = Adw.ActionRow(
            title="0 selected",
            subtitle="pick at least one packet to start")
        filt_group.add(self.count_row)
        box.append(filt_group)

        # ---- the actual list ------------------------------------
        # Gtk.ListView with a SignalListItemFactory so 747 rows don't
        # kill the phone. Only the visible ones (~15) get real widgets.
        self._all_items = Gio.ListStore.new(PacketItem)
        self._categories = payloads.build_categories()
        for cat_title, packets in self._categories:
            for pkt in packets:
                item = PacketItem(pkt)
                item.category = cat_title
                self._all_items.append(item)

        self._filter = Gtk.CustomFilter.new(self._filter_fn, None)
        self._filter_model = Gtk.FilterListModel.new(
            self._all_items, self._filter)
        selection = Gtk.NoSelection.new(self._filter_model)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._factory_setup)
        factory.connect("bind", self._factory_bind)
        factory.connect("unbind", self._factory_unbind)

        listview = Gtk.ListView.new(selection, factory)
        listview.add_css_class("nh-blespam-list")

        list_scroller = Gtk.ScrolledWindow()
        list_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroller.set_min_content_height(320)
        list_scroller.set_child(listview)
        # A ScrolledWindow inside the outer scroller of the module page
        # would let the phone user get stuck scrolling the wrong one.
        # Cap the child height so it participates in the outer scroll.
        list_scroller.set_max_content_height(360)
        list_scroller.set_propagate_natural_height(False)
        box.append(list_scroller)

        # ---- output log ----------------------------------------
        self.output = OutputView()
        box.append(self.output)

        # First render / count refresh.
        self._refresh_count()
        return box

    # ------------------------------------------------ list factory
    def _factory_setup(self, _f, list_item: Gtk.ListItem) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(4)
        row.set_margin_bottom(4)
        row.set_margin_start(8)
        row.set_margin_end(8)

        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        row.append(check)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                            hexpand=True)
        title = Gtk.Label(halign=Gtk.Align.START, ellipsize=3)
        title.add_css_class("heading")
        hex_lbl = Gtk.Label(halign=Gtk.Align.START, ellipsize=3)
        hex_lbl.add_css_class("dim-label")
        hex_lbl.add_css_class("monospace")
        text_box.append(title)
        text_box.append(hex_lbl)
        row.append(text_box)

        # Stash the widgets on list_item so bind can find them without
        # re-walking the tree.
        list_item.check = check
        list_item.title_lbl = title
        list_item.hex_lbl = hex_lbl
        list_item.handler_id = None
        list_item.set_child(row)

    def _factory_bind(self, _f, list_item: Gtk.ListItem) -> None:
        item: PacketItem = list_item.get_item()
        list_item.title_lbl.set_text(item.title)
        list_item.hex_lbl.set_text(item.hex_preview)
        list_item.check.set_active(item.checked)
        # Toggling the checkbox writes back to the item.
        handler = list_item.check.connect(
            "toggled", self._on_row_toggle, item)
        list_item.handler_id = handler

    def _factory_unbind(self, _f, list_item: Gtk.ListItem) -> None:
        if getattr(list_item, "handler_id", None) is not None:
            list_item.check.disconnect(list_item.handler_id)
            list_item.handler_id = None

    def _on_row_toggle(self, check: Gtk.CheckButton, item: PacketItem
                       ) -> None:
        item.checked = check.get_active()
        self._refresh_count()

    # ------------------------------------------------ filter
    def _filter_fn(self, item: PacketItem, _user) -> bool:
        cat_idx = self.cat_combo.get_selected()
        if cat_idx > 0:
            cat_labels = ["All"] + [
                t for t, _p in self._categories]
            wanted = cat_labels[cat_idx]
            if item.category != wanted:
                return False
        needle = self.search.get_text().strip().lower()
        if needle and needle not in item.title.lower():
            return False
        return True

    def _on_filter_change(self) -> None:
        # CustomFilter re-evaluates when we tell it something changed.
        if self._filter is not None:
            self._filter.changed(Gtk.FilterChange.DIFFERENT)

    def _toggle_visible(self, checked: bool) -> None:
        if self._filter_model is None:
            return
        n = self._filter_model.get_n_items()
        for i in range(n):
            item: PacketItem = self._filter_model.get_item(i)
            item.checked = checked
        self._filter.changed(Gtk.FilterChange.DIFFERENT)
        self._refresh_count()

    def _clear_all(self) -> None:
        if self._all_items is None:
            return
        for i in range(self._all_items.get_n_items()):
            item: PacketItem = self._all_items.get_item(i)
            item.checked = False
        self._refresh_count()

    def _refresh_count(self) -> None:
        if self._all_items is None:
            return
        n = 0
        for i in range(self._all_items.get_n_items()):
            if self._all_items.get_item(i).checked:
                n += 1
        self.count_row.set_title(
            "%d selected" % n)
        if n == 0:
            self.count_row.set_subtitle(
                "pick at least one packet to start")
        else:
            self.count_row.set_subtitle(
                "the engine will cycle these while Start is on")

    def _selected_titles(self) -> list[str]:
        titles: list[str] = []
        if self._all_items is None:
            return titles
        for i in range(self._all_items.get_n_items()):
            item: PacketItem = self._all_items.get_item(i)
            if item.checked:
                titles.append(item.title)
        return titles

    # ------------------------------------------------ run
    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        titles = self._selected_titles()
        if not titles:
            toast(self.app_window, "Tick at least one packet")
            return
        if not os.path.exists(RUNNER):
            self.output.append(
                "[error] runner not installed at %s\n"
                "        re-run install.sh to fix\n" % RUNNER)
            toast(self.app_window, "Runner missing; see log")
            return

        adapter_names = _list_adapters() or ["hci0"]
        adapter = adapter_names[self.adapter_combo.get_selected()]
        dev_id = int(adapter[3:])
        mode_names = [MODE_SEQUENTIAL, MODE_RANDOM]
        mode = mode_names[self.mode.get_selected()]
        tx_names = list(TX_POWER_BYTES.keys())
        tx = tx_names[self.tx.get_selected()]

        cfg = {
            "packet_ids": titles,
            "interval_ms": int(self.interval.get_value()),
            "payload_change_ms": int(self.payload_change.get_value()),
            "mode": mode,
            "tx_power": tx,
            "dev_id": dev_id,
        }
        self.output.append(
            "# starting blespam on %s: %d packets, adv %dms / swap %dms, "
            "%s, TX %s\n"
            % (adapter, len(titles), cfg["interval_ms"],
               cfg["payload_change_ms"], mode, tx))
        self._proc = Process(
            [RUNNER, json.dumps(cfg)],
            self._on_runner_line,
            self._on_runner_done,
            root=True,
        )
        self._proc.start()
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _stop(self) -> None:
        if self._proc is None:
            return
        self.output.append("[stopping]\n")
        self._proc.stop()

    def _on_runner_line(self, text: str) -> None:
        # Runner streams NDJSON. Anything that fails to parse is echoed
        # as-is (radio.prepare_radio() writes plain progress messages to
        # stderr, which the helper mixes into the stream).
        line = text.rstrip("\n")
        if not line:
            return
        if not line.startswith("{"):
            self.output.append(line + "\n")
            return
        try:
            evt = json.loads(line)
        except ValueError:
            self.output.append(line + "\n")
            return
        kind = evt.get("event")
        if kind == "ready":
            self.output.append(
                "[ready] %d packets queued; preparing radio…\n"
                % evt.get("count", 0))
        elif kind == "state":
            state = evt.get("state", "?")
            self.output.append("[state] %s\n" % state)
        elif kind == "packet":
            title = evt.get("title", "?")
            hexs = evt.get("hex", "")
            self.output.append("  %s  --  %s\n" % (title, hexs))
        elif kind == "error":
            self.output.append("[error] %s\n"
                               % evt.get("message", ""))
        elif kind == "recovery":
            # Emitted from the engine's self-heal loop. Not fatal --
            # just surface it so the user sees the chip is unhappy but
            # the run continues. If we ever see "unrecoverable" the
            # runner is about to exit anyway.
            k = evt.get("kind", "?")
            reason = evt.get("reason", "?")
            self.output.append(
                "[self-heal:%s] %s\n" % (k, reason))
        else:
            self.output.append(line + "\n")

    def _on_runner_done(self, code: int) -> None:
        self._proc = None
        self.start_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        self.output.append("[runner exited: %d]\n" % code)

    # -------------------------------------------------------- recover
    def _recover(self) -> None:
        """Fire the serdev unbind/bind helper as root.

        Only makes sense when the chip is wedged -- if the runner is
        currently up, killing the radio out from under it would leave
        the engine deadlocked. So we refuse to run in that case.
        """
        if self._proc is not None and self._proc.running:
            toast(self.app_window,
                  "Stop the run first, then recover")
            return
        if not os.path.exists(RECOVER):
            self.output.append(
                "[error] recover helper not installed at %s\n" % RECOVER)
            return
        self.output.append("# recovering hci0 via serdev unbind/bind\n")
        self.recover_btn.set_sensitive(False)

        # A one-shot Process (no streaming needed for a ~5s helper,
        # but Process gives us the DBus-root path for free).
        proc = Process(
            [RECOVER],
            self.output.append,
            self._on_recover_done,
            root=True,
        )
        proc.start()
        # Stash it so on_recover_done can null it and re-enable the
        # button.
        self._recover_proc = proc

    def _on_recover_done(self, code: int) -> None:
        self.recover_btn.set_sensitive(True)
        if code == 0:
            self.output.append(
                "[recover] hci0 is UP RUNNING; Start should work now\n")
            toast(self.app_window, "Radio recovered")
        else:
            self.output.append(
                "[recover] exit %d -- try again, or reboot the phone\n"
                % code)
