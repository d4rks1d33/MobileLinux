"""Loot -- central browser for every capture / hash / credential file
the app produces.

Every module that generates something worth keeping (PMKID pcap, WPA
handshake pcap, EAP hash file, probe CSV, cracked-plaintext KB from
Kr00k, BLE provisioning PSK, etc.) writes an on-disk artifact under
``~/loot/<module>/`` and calls ``loot_store.record(...)`` to index it.

This screen is where the operator sees them all in one table with the
filters we care about: module, type, date. It exposes the actions that
make sense on aggregate loot:

* Delete selected -- rm the file + drop the row, so disk space frees up
  the moment we don't need something anymore.
* Delete all older than N days -- housekeeping for long engagements.
* Submit to wpa-sec.stanev.org -- for pcap-type rows, uploads to the
  distributed WPA cracker so the phone doesn't have to run hashcat
  (there is no GPU here anyway). The site does the work; a periodic
  refresh pulls back cracked results.
* Open loot folder -- xdg-open a file manager on ``~/loot`` for when
  the user wants to work with the raw files.

The wpa-sec API key is a Gio.Settings string ("wpasec-key") under the
NetHunterPro schema so it survives restarts and lives out of the DB.
If the schema does not expose that key we fall back to a plain-text
file at ``~/.config/nethunter-pro/wpasec.key`` -- simpler than adding
a gschema entry.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import run_async
from ..loot_store import (
    LootEntry, get_loot_store, wpasec_fetch_cracks, wpasec_submit,
    LOOT_ROOT,
)
from ..module import NHModule, register
from ..widgets import toast

WPASEC_KEY_FILE = Path(
    os.environ.get("XDG_CONFIG_HOME")
    or os.path.expanduser("~/.config")) / "nethunter-pro" / "wpasec.key"

# On a Kali system that already runs Pwnagotchi, the wpa-sec API key
# will be sitting in the Pwnagotchi TOML config. Cheapest possible
# key-provisioning is to read it from there the first time Loot is
# opened, so the user doesn't have to hunt for it in two places.
# We only *read* Pwnagotchi's config; we never write it.
_PWNAGOTCHI_CONFIGS = (
    "/etc/pwnagotchi/config.toml",
    "/etc/pwnagotchi/default.toml",
)

# Types we know can be submitted to wpa-sec (pcap-based WPA/PMKID
# captures). Other rows still show but the Submit button is disabled.
SUBMITTABLE_TYPES = {
    "pmkid_pcap",
    "handshake_pcap",
    "wifi_capture",
}


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%d %s" % (n, unit)
        n //= 1024
    return "%d PB" % n


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _pwnagotchi_wpasec_key() -> str:
    """Scrape Pwnagotchi's TOML for its wpa-sec API key.

    The config typically has a block like::

        [main.plugins.wpa-sec]
        enabled = true
        api_key = "0123456789abcdef0123456789abcdef"

    We grep the two well-known config paths in order (most-specific
    first) and return the first non-empty api_key that sits inside
    the ``[main.plugins.wpa-sec]`` section. We do NOT touch the file.
    Returns "" if nothing usable is found.
    """
    section_re = re.compile(r"^\s*\[main\.plugins\.wpa-sec\]",
                             re.IGNORECASE)
    key_re = re.compile(
        r"^\s*api_key\s*=\s*[\"']([0-9a-fA-F]{16,64})[\"']")
    for path in _PWNAGOTCHI_CONFIGS:
        try:
            in_section = False
            with open(path) as fp:
                for line in fp:
                    if line.lstrip().startswith("["):
                        # Any new section terminates ours.
                        in_section = bool(section_re.match(line))
                        continue
                    if in_section:
                        m = key_re.match(line)
                        if m:
                            return m.group(1)
        except OSError:
            continue
    return ""


def _load_wpasec_key() -> str:
    """Return our own wpa-sec API key. Falls back to Pwnagotchi's
    config so a system that already scrapes into wpa-sec has one
    fewer setting to enter."""
    try:
        own = WPASEC_KEY_FILE.read_text().strip()
        if own:
            return own
    except OSError:
        pass
    return _pwnagotchi_wpasec_key()


def _save_wpasec_key(key: str) -> None:
    WPASEC_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    WPASEC_KEY_FILE.write_text(key.strip() + "\n")
    try:
        os.chmod(WPASEC_KEY_FILE, 0o600)
    except OSError:
        pass


@register
class Loot(NHModule):
    title = "Loot"
    icon = "folder-download-symbolic"
    description = ("Captures, hashes and credentials produced by every "
                   "module; submit to wpa-sec, clean up old files")

    def __init__(self, app_window):
        super().__init__(app_window)
        self.store = get_loot_store()
        self._filter_module: str | None = None
        self._filter_type: str | None = None
        # Rows currently displayed, in the order they are in the list
        # box. We keep the actual LootEntry objects so button callbacks
        # can access them without re-reading the DB.
        self._rows: list[LootEntry] = []

    # ------------------------------------------------------------ build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ---- summary group
        summary = Adw.PreferencesGroup(title="Overview")
        self.summary_row = Adw.ActionRow(
            title="Items", subtitle="0")
        summary.add(self.summary_row)
        self.size_row = Adw.ActionRow(
            title="Disk usage", subtitle="0 B")
        summary.add(self.size_row)
        folder_row = Adw.ActionRow(
            title="Loot folder", subtitle=str(LOOT_ROOT))
        open_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda _b: self._open_folder())
        folder_row.add_suffix(open_btn)
        summary.add(folder_row)
        box.append(summary)

        # ---- wpa-sec integration group
        wp = Adw.PreferencesGroup(
            title="wpa-sec.stanev.org",
            description="Distributed WPA/PMKID cracker. Submit a "
                        "capture, wait for the community to crack it, "
                        "then delete the local pcap. No hashcat "
                        "required.")
        self.wpasec_key_row = Adw.PasswordEntryRow(
            title="API key (Cookie: key=...)")
        self.wpasec_key_row.set_show_apply_button(True)
        current_key = _load_wpasec_key()
        self.wpasec_key_row.set_text(current_key)
        self.wpasec_key_row.connect("apply", self._on_key_apply)
        wp.add(self.wpasec_key_row)
        # If we pulled the key out of Pwnagotchi's config rather
        # than our own file, surface that in a sibling row so the
        # user can confirm it. Also expose an explicit "Import"
        # button that writes it into our own wpasec.key -- some
        # people want the two configs decoupled from that point on.
        pw_key = _pwnagotchi_wpasec_key()
        own_key = ""
        try:
            own_key = WPASEC_KEY_FILE.read_text().strip()
        except OSError:
            pass
        if pw_key and not own_key:
            src_row = Adw.ActionRow(
                title="Pwnagotchi key detected",
                subtitle="Loaded " + pw_key[:8] + "… from "
                          "/etc/pwnagotchi/config.toml")
            src_btn = Gtk.Button(label="Import",
                                 valign=Gtk.Align.CENTER)
            src_btn.add_css_class("suggested-action")
            src_btn.connect(
                "clicked",
                lambda _b: (
                    _save_wpasec_key(pw_key),
                    toast(self.app_window,
                          "Pwnagotchi key imported")))
            src_row.add_suffix(src_btn)
            wp.add(src_row)

        key_link_row = Adw.ActionRow(
            title="Don't have a key?",
            subtitle="Get one at wpa-sec.stanev.org (Get key)")
        key_btn = Gtk.Button(label="Open site",
                             valign=Gtk.Align.CENTER)
        key_btn.connect(
            "clicked",
            lambda _b: Gtk.UriLauncher.new(
                "https://wpa-sec.stanev.org/?get_key"
            ).launch(self.app_window, None, None, None))
        key_link_row.add_suffix(key_btn)
        wp.add(key_link_row)

        refresh_row = Adw.ActionRow(
            title="Check cracked results",
            subtitle="Poll the site for PSKs it recovered from our "
                     "submissions")
        refresh_btn = Gtk.Button(label="Refresh",
                                 valign=Gtk.Align.CENTER)
        refresh_btn.connect("clicked",
                            lambda _b: self._refresh_cracks())
        refresh_row.add_suffix(refresh_btn)
        wp.add(refresh_row)
        box.append(wp)

        # ---- cleanup group
        clean = Adw.PreferencesGroup(
            title="Cleanup",
            description="Free disk space when you don't need old "
                        "captures anymore.")
        self.age_row = Adw.SpinRow.new_with_range(1, 365, 1)
        self.age_row.set_title("Delete entries older than (days)")
        self.age_row.set_value(30)
        clean.add(self.age_row)
        prune_row = Adw.ActionRow(
            title="Prune old entries",
            subtitle="Removes the DB rows and the files behind them")
        prune_btn = Gtk.Button(label="Prune",
                               valign=Gtk.Align.CENTER)
        prune_btn.add_css_class("destructive-action")
        prune_btn.connect("clicked", lambda _b: self._prune_old())
        prune_row.add_suffix(prune_btn)
        clean.add(prune_row)
        box.append(clean)

        # ---- filter bar
        filter_grp = Adw.PreferencesGroup(title="Filter")
        self.module_combo = Adw.ComboRow(title="Module")
        self.module_combo.connect(
            "notify::selected", self._on_filter_module)
        filter_grp.add(self.module_combo)

        self.type_combo = Adw.ComboRow(title="Type")
        self.type_combo.connect(
            "notify::selected", self._on_filter_type)
        filter_grp.add(self.type_combo)
        box.append(filter_grp)

        # ---- rows
        # We drive the rows through an explicit Gtk.ListBox that we own,
        # rather than adding straight to a PreferencesGroup, because
        # PreferencesGroup does not expose a clean way to remove all its
        # rows on every reload. A ListBox with .remove_all() (or the
        # equivalent iteration) is trivial. The visual result is close
        # enough thanks to the "boxed-list" style class.
        entries_group = Adw.PreferencesGroup(title="Entries")
        refresh_hdr = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic")
        refresh_hdr.set_tooltip_text("Reload")
        refresh_hdr.add_css_class("flat")
        refresh_hdr.connect("clicked", lambda _b: self._reload())
        entries_group.set_header_suffix(refresh_hdr)

        self.entries_box = Gtk.ListBox()
        self.entries_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.entries_box.add_css_class("boxed-list")

        self.empty_row = Adw.ActionRow(
            title="No loot yet",
            subtitle="Modules will populate this table as they run")
        self.entries_box.append(self.empty_row)
        entries_group.add(self.entries_box)
        box.append(entries_group)

        self._reload()
        return box

    # ------------------------------------------------------------ reload
    def _reload(self) -> None:
        # Refresh combo boxes with the current distinct values from
        # the DB, preserving the user's selection when possible.
        prev_mod = self._filter_module
        prev_type = self._filter_type
        modules = ["(any)"] + self.store.distinct_modules()
        types = ["(any)"] + self.store.distinct_types()
        self._fill_combo(self.module_combo, modules, prev_mod)
        self._fill_combo(self.type_combo, types, prev_type)

        # Fetch and paint.
        entries = self.store.list(
            module=self._filter_module,
            type=self._filter_type)
        # Clear the listbox.
        row = self.entries_box.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            self.entries_box.remove(row)
            row = nxt
        self._rows = entries
        if not entries:
            self.entries_box.append(Adw.ActionRow(
                title="No loot yet",
                subtitle="Modules will populate this table as they run"
            ))
        for e in entries:
            self.entries_box.append(self._build_entry_row(e))

        self.summary_row.set_subtitle(str(self.store.count()))
        self.size_row.set_subtitle(_human_size(self.store.total_size()))

    def _fill_combo(self, combo: Adw.ComboRow,
                    values: list[str], selected: str | None) -> None:
        # Setting the model resets the selection index to 0, which
        # fires `notify::selected` -- and *that* handler calls
        # ``_reload`` again -> infinite loop. Block both filter
        # handlers around the model swap so we can rebuild the UI
        # without recursing back into ourselves.
        model = Gtk.StringList.new(values)
        try:
            combo.handler_block_by_func(self._on_filter_module)
        except TypeError:
            pass
        try:
            combo.handler_block_by_func(self._on_filter_type)
        except TypeError:
            pass
        combo.set_model(model)
        idx = 0
        if selected and selected in values:
            idx = values.index(selected)
        combo.set_selected(idx)
        try:
            combo.handler_unblock_by_func(self._on_filter_module)
        except TypeError:
            pass
        try:
            combo.handler_unblock_by_func(self._on_filter_type)
        except TypeError:
            pass

    def _build_entry_row(self, e: LootEntry) -> Adw.ActionRow:
        # Compose a subtitle: type · target · size · ts · wpa-sec status
        parts = [e.type, e.target, _human_size(e.size),
                 _fmt_ts(e.ts)]
        if e.wpasec_status == "cracked":
            parts.append("wpa-sec: %s" % e.wpasec_psk)
        elif e.wpasec_status:
            parts.append("wpa-sec: " + e.wpasec_status)
        subtitle = " · ".join(p for p in parts if p)
        row = Adw.ActionRow(title=e.module + " #" + str(e.id),
                            subtitle=subtitle)

        # Actions differ by type. Submit only for pcap-shaped rows,
        # Delete always.
        if e.type in SUBMITTABLE_TYPES and not e.wpasec_status:
            sub_btn = Gtk.Button(label="Submit",
                                 valign=Gtk.Align.CENTER)
            sub_btn.add_css_class("suggested-action")
            sub_btn.set_tooltip_text(
                "Upload to wpa-sec.stanev.org for distributed cracking")
            sub_btn.connect(
                "clicked",
                lambda _b, entry=e: self._submit_entry(entry))
            row.add_suffix(sub_btn)

        del_btn = Gtk.Button(label="Delete",
                             valign=Gtk.Align.CENTER)
        del_btn.add_css_class("destructive-action")
        del_btn.connect(
            "clicked",
            lambda _b, entry=e: self._delete_entry(entry))
        row.add_suffix(del_btn)
        return row

    # -------------------------------------------------------- filter cbs
    def _on_filter_module(self, combo: Adw.ComboRow, _p) -> None:
        idx = combo.get_selected()
        model = combo.get_model()
        val = model.get_string(idx) if model else "(any)"
        self._filter_module = None if val == "(any)" else val
        self._reload()

    def _on_filter_type(self, combo: Adw.ComboRow, _p) -> None:
        idx = combo.get_selected()
        model = combo.get_model()
        val = model.get_string(idx) if model else "(any)"
        self._filter_type = None if val == "(any)" else val
        self._reload()

    # ------------------------------------------------------- wpa-sec cbs
    def _on_key_apply(self, row: Adw.PasswordEntryRow) -> None:
        _save_wpasec_key(row.get_text())
        toast(self.app_window, "wpa-sec key saved")

    def _submit_entry(self, e: LootEntry) -> None:
        api_key = _load_wpasec_key()
        if not api_key:
            toast(self.app_window,
                  "Set a wpa-sec API key first")
            return
        toast(self.app_window,
              "Uploading %s to wpa-sec…" % os.path.basename(e.path))

        def worker():
            ok, msg = wpasec_submit(e.path, api_key)
            status = "submitted" if ok else "error"
            self.store.set_wpasec(e.id, status, notes=msg)
            GLib.idle_add(self._after_submit, e, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _after_submit(self, e: LootEntry, ok: bool, msg: str) -> bool:
        if ok:
            toast(self.app_window,
                  "wpa-sec accepted #%d: %s" % (e.id, msg))
        else:
            toast(self.app_window,
                  "wpa-sec upload failed for #%d: %s" % (e.id, msg))
        self._reload()
        return False

    def _refresh_cracks(self) -> None:
        api_key = _load_wpasec_key()
        if not api_key:
            toast(self.app_window, "Set a wpa-sec API key first")
            return
        toast(self.app_window, "Checking wpa-sec…")

        def worker():
            cracks = wpasec_fetch_cracks(api_key)
            matched = 0
            for entry in self.store.list():
                # Match by SSID substring in any potfile line. The
                # potfile contains SSID as a field, so a plain-text
                # match on ``target`` is the cheapest identifier.
                if not entry.target:
                    continue
                if entry.wpasec_status == "cracked":
                    continue
                for line, psk in cracks.items():
                    if entry.target.lower() in line.lower():
                        self.store.set_wpasec(
                            entry.id, "cracked", psk=psk,
                            notes="matched: " + line[:200])
                        matched += 1
                        break
            GLib.idle_add(self._after_refresh, matched)

        threading.Thread(target=worker, daemon=True).start()

    def _after_refresh(self, matched: int) -> bool:
        toast(self.app_window,
              "wpa-sec: %d entries updated" % matched)
        self._reload()
        return False

    # --------------------------------------------------------- delete cbs
    def _delete_entry(self, e: LootEntry) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self.app_window,
            heading="Delete entry?",
            body="Removes the DB row and the file at:\n%s" % e.path)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance(
            "delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def on_resp(_d, resp: str) -> None:
            if resp != "delete":
                return
            self.store.delete(e.id, unlink=True)
            toast(self.app_window, "Deleted #%d" % e.id)
            self._reload()
        dlg.connect("response", on_resp)
        dlg.present()

    def _prune_old(self) -> None:
        days = int(self.age_row.get_value())
        dlg = Adw.MessageDialog(
            transient_for=self.app_window,
            heading="Prune old entries?",
            body="Deletes every entry older than %d days, and the "
                 "files behind them. Can't be undone." % days)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("prune", "Prune")
        dlg.set_response_appearance(
            "prune", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def on_resp(_d, resp: str) -> None:
            if resp != "prune":
                return
            n = self.store.delete_older_than(days * 86400, unlink=True)
            toast(self.app_window, "Pruned %d entries" % n)
            self._reload()
        dlg.connect("response", on_resp)
        dlg.present()

    # ---------------------------------------------------------- open dir
    def _open_folder(self) -> None:
        # xdg-open under the login user's session so it routes to the
        # actual file manager (nautilus / files / whatever).
        script = 'mkdir -p %s; xdg-open %s >/dev/null 2>&1 &' % (
            GLib.shell_quote(str(LOOT_ROOT)),
            GLib.shell_quote(str(LOOT_ROOT)),
        )
        run_async(["sh", "-c", script], lambda _r: None,
                  root=False, timeout=5)

    # ---- deep-link contract used by other modules ---------------------
    def set_target(self, target: str) -> None:
        """Called by the deep-link machinery. We interpret ``target``
        as a filter string in the form ``module:<name>`` or
        ``type:<name>`` so a caller can jump straight to their rows.
        Anything else is ignored quietly."""
        if not target:
            return
        if target.startswith("module:"):
            val = target.split(":", 1)[1]
            self._filter_module = val or None
        elif target.startswith("type:"):
            val = target.split(":", 1)[1]
            self._filter_type = val or None
        self._reload()
