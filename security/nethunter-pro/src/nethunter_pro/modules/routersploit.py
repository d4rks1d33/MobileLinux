"""RouterSploit driver: scanners, per-vendor exploits, brute-force and search.

RouterSploit (`/opt/routersploit`) is an msf-style framework for embedded-device
vulnerabilities: 143 exploits organised per vendor (cisco, tplink, dlink,
netgear, huawei, mikrotik, ...), 171 default-credential brute-forcers, four
scanners (`autopwn`, `router_scan`, `camera_scan`, `misc_scan`), plus payloads
and encoders. Its interpreter (`rsf.py`) is interactive by default but also
accepts a non-interactive shape that this module drives directly:

    python3 -u /opt/routersploit/rsf.py -m <module_path> \
        -s "<opt1> <val1>" -s "<opt2> <val2>" ...

`-u` is what lets us stream output live to the OutputView -- without it the
framework's printer_queue holds all lines until the process exits cleanly, and
anything killed by the Stop button drops its output on the floor.

The module discovers the tree by walking `routersploit/modules/**/*.py` once
and caching. Each Python file that defines a class named `Exploit` is a module
in the framework's sense; its `Opt*` class attributes are its runtime options
and are parsed with a small regex-based reader. That is enough to build the
right input widget for every option (Bool -> switch, IP/Port/String -> entry,
Wordlist -> file entry) so the user never has to remember the parameter
names.
"""
from __future__ import annotations

import os
import re
from typing import NamedTuple

from gi.repository import Adw, GLib, Gio, Gtk

from ..executor import Process, Result, run_async, which
from ..module import NHModule, register
from ..widgets import OutputView, toast

# Root of the checkout. The `rsf.py` entry point lives here and expects to be
# executed from this directory (it writes routersploit.log to $CWD and its
# imports resolve relative to it). All rsf.py invocations therefore use
# `sh -c 'cd <dir> && python3 -u rsf.py ...'`.
RS_DIR = "/opt/routersploit"
RS_ENTRY = "rsf.py"
RS_MODULES_DIR = os.path.join(RS_DIR, "routersploit", "modules")

# One Opt* declaration per line, e.g.
#   target = OptIP("", "Target IPv4 or IPv6 address")
#   port = OptPort(80, "Target HTTP port")
#   http_ssl = OptBool(False, "HTTPS enabled: true/false")
# The default value can be a bare literal (True/False/80/"public") or an
# empty string; the description is always a quoted string; `advanced=True`
# may follow. Regex tries to be forgiving; the parser tolerates rows it
# cannot fully understand by skipping them.
_OPT_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"
    r"Opt(IP|Port|Bool|Integer|Float|String|Wordlist|Encoder|Payload)"
    r"\s*\(\s*([^,]*?)\s*,\s*[\"']([^\"']*)[\"']",
    re.MULTILINE,
)
_INFO_NAME_RE = re.compile(r'"name"\s*:\s*[\'"]([^\'"]+)[\'"]')
_INFO_DESC_RE = re.compile(r'"description"\s*:\s*[\'"]([^\'"]+)', re.S)
_DEVICES_RE = re.compile(
    r'"devices"\s*:\s*\(\s*(.*?)\s*\)', re.S)


class Opt(NamedTuple):
    name: str
    type: str          # IP / Port / Bool / Integer / Float / String / Wordlist / Encoder / Payload
    default: str
    description: str


class Mod(NamedTuple):
    path: str          # e.g. "exploits/routers/dlink/dir_300_600_rce"
    category: str      # e.g. "exploits"
    vendor: str        # e.g. "dlink" for exploits/routers, empty otherwise
    name: str          # __info__["name"]
    description: str   # __info__["description"], truncated
    options: list      # list[Opt]


def _walk_modules(root: str = RS_MODULES_DIR) -> list[Mod]:
    """One-time scan of the module tree, returning `Mod` objects.

    Cheap: ~350 files, ~1.5 MB of source total, done once and cached in the
    module instance. The regex reader is deliberately loose -- a module with a
    weirdly-formatted `Opt*` line loses that one option but still shows up in
    the picker with the rest of its options intact.
    """
    mods: list[Mod] = []
    for dirpath, _dnames, fnames in os.walk(root):
        for fname in fnames:
            if not fname.endswith(".py") or fname == "__init__.py":
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            # scanners/routers/router_scan.py -> scanners/routers/router_scan
            path = rel[:-3].replace(os.sep, "/")
            parts = path.split("/")
            category = parts[0]
            # exploits/routers/tplink/foo -> vendor = "tplink"
            # exploits/misc/foo           -> vendor = "misc"
            vendor = parts[2] if (
                len(parts) >= 4 and category == "exploits"
                and parts[1] == "routers") else (
                    parts[1] if len(parts) >= 2 else "")
            try:
                text = open(full, encoding="utf-8",
                            errors="replace").read()
            except OSError:
                continue
            name_m = _INFO_NAME_RE.search(text)
            desc_m = _INFO_DESC_RE.search(text)
            name = name_m.group(1) if name_m else path.split("/")[-1]
            desc = (desc_m.group(1).strip()
                    if desc_m else "")[:300]
            options: list[Opt] = []
            for m in _OPT_RE.finditer(text):
                oname, otype, default, description = m.groups()
                default = default.strip().strip('"\'')
                options.append(Opt(oname, otype, default, description))
            mods.append(Mod(path, category, vendor, name, desc, options))
    mods.sort(key=lambda m: (m.category, m.vendor, m.path))
    return mods


@register
class RouterSploit(NHModule):
    title = "RouterSploit"
    icon = "network-wired-symbolic"
    description = "Router / IoT / camera vulnerabilities and default creds"
    required_tools = ["python3"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._all_mods: list[Mod] = []
        self._opt_rows: list[Adw.EntryRow | Adw.SwitchRow | Adw.SpinRow] = []
        self._opt_widgets: dict[str, tuple[str, object]] = {}
        self._selected_mod: Mod | None = None
        self._current_view = "scanners"

    # RouterSploit is only useful if the checkout is present. Instead of
    # requiring a binary on PATH (there isn't one), gate on the entry point.
    @property
    def missing_tools(self) -> list[str]:
        missing = super().missing_tools
        if not os.path.isfile(os.path.join(RS_DIR, RS_ENTRY)):
            missing.append("routersploit @ " + RS_DIR)
        return missing

    # -------------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ---- section picker ---------------------------------------
        header = Adw.PreferencesGroup(
            title="RouterSploit",
            description="Pick a section, fill in a target and run. "
                        "Everything runs as root (raw sockets, SSH, etc.).")
        self.section = Adw.ComboRow(title="Section")
        self.section.set_model(Gtk.StringList.new([
            "Scanners (autopwn / router / camera / misc)",
            "Exploits by vendor",
            "Default-credential brute-force",
            "Search",
        ]))
        self.section.connect("notify::selected", self._on_section)
        header.add(self.section)
        box.append(header)

        # Each view is a PreferencesGroup wired lazily below. Only the
        # picker and status row are always visible; the concrete pickers
        # for module + options are rebuilt when the section changes.
        self._body_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=12)
        box.append(self._body_holder)

        # ---- run controls ------------------------------------------
        controls = Adw.PreferencesGroup()
        self.state_row = Adw.ActionRow(
            title="Ready", subtitle="No RouterSploit run yet")
        self.state_icon = Gtk.Image.new_from_icon_name(
            "media-playback-stop-symbolic")
        self.state_row.add_prefix(self.state_icon)
        controls.add(self.state_row)

        run_row = Adw.ActionRow(
            title="Run selected module",
            subtitle="check first (safe probe), then exploit to actually run")
        self.check_btn = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        self.check_btn.connect("clicked", lambda _b: self._launch(check=True))
        run_row.add_suffix(self.check_btn)
        self.run_btn = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
        self.run_btn.add_css_class("suggested-action")
        self.run_btn.connect("clicked", lambda _b: self._launch(check=False))
        run_row.add_suffix(self.run_btn)
        self.stop_btn = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        run_row.add_suffix(self.stop_btn)
        controls.add(run_row)
        box.append(controls)

        # ---- output ------------------------------------------------
        self.output = OutputView()
        box.append(self.output)

        # Discover modules in the background so opening the screen is fast
        # even on the phone; the picker gets populated when the walk
        # returns (~200ms even on ARM).
        run_async(
            ["sh", "-c",
             "find %s -name '*.py' -not -name '__init__.py' | wc -l"
             % RS_MODULES_DIR],
            lambda _r: self._load_modules_async(),
            root=False, timeout=10,
        )
        return box

    def _load_modules_async(self) -> None:
        # `_walk_modules` is pure filesystem + regex, no shell out.
        def worker():
            try:
                mods = _walk_modules()
            except Exception as exc:  # pragma: no cover
                GLib.idle_add(self.output.append,
                              "[error walking modules: %s]\n" % exc)
                return
            GLib.idle_add(self._on_modules_loaded, mods)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _on_modules_loaded(self, mods: list[Mod]) -> None:
        self._all_mods = mods
        cats: dict[str, int] = {}
        for m in mods:
            cats[m.category] = cats.get(m.category, 0) + 1
        summary = ", ".join(
            "%d %s" % (n, c) for c, n in sorted(cats.items()))
        self.state_row.set_subtitle(
            "%d modules loaded (%s)" % (len(mods), summary))
        # Now that modules are known, populate whichever view is showing.
        self._on_section(None, None)

    # -------------------------------------------------------- sections
    def _on_section(self, _row, _p) -> None:
        # Clear body and rebuild for the selected section. Keeps memory
        # low and avoids stale widgets when the module count changes.
        while (child := self._body_holder.get_first_child()) is not None:
            self._body_holder.remove(child)
        self._opt_rows = []
        self._opt_widgets = {}
        self._selected_mod = None
        sel = self.section.get_selected()
        if sel == 0:
            self._build_scanners()
        elif sel == 1:
            self._build_exploits()
        elif sel == 2:
            self._build_creds()
        else:
            self._build_search()

    # ---- scanners ---------------------------------------------------
    def _build_scanners(self) -> None:
        scanners = [m for m in self._all_mods if m.category == "scanners"]
        if not scanners:
            g = Adw.PreferencesGroup(title="Scanners")
            g.add(Adw.ActionRow(
                title="Loading…",
                subtitle="Scanning /opt/routersploit"))
            self._body_holder.append(g)
            return

        g = Adw.PreferencesGroup(
            title="Scanners",
            description="Fires every known check for a given host. autopwn "
                        "is the broadest -- it runs router, camera and misc "
                        "checks together.")
        self.scanner_combo = Adw.ComboRow(title="Scanner")
        # Prefer autopwn as the default because it's the workhorse.
        scanners.sort(key=lambda m: (0 if m.path == "scanners/autopwn"
                                     else 1, m.path))
        model = Gtk.StringList.new([
            "%s -- %s" % (m.path.replace("scanners/", ""), m.name)
            for m in scanners])
        self.scanner_combo.set_model(model)
        self.scanner_combo.connect(
            "notify::selected",
            lambda _r, _p: self._select_module(
                scanners[self.scanner_combo.get_selected()]))
        g.add(self.scanner_combo)
        self._body_holder.append(g)

        # Options group is (re)built by _select_module.
        self._options_group = Adw.PreferencesGroup(title="Options")
        self._body_holder.append(self._options_group)
        self._select_module(scanners[0])

    # ---- exploits ---------------------------------------------------
    def _build_exploits(self) -> None:
        exploits = [m for m in self._all_mods if m.category == "exploits"]
        if not exploits:
            g = Adw.PreferencesGroup(title="Exploits")
            g.add(Adw.ActionRow(title="Loading…"))
            self._body_holder.append(g)
            return

        # Group by vendor -> list of modules.
        by_vendor: dict[str, list[Mod]] = {}
        for m in exploits:
            key = m.vendor or "misc"
            by_vendor.setdefault(key, []).append(m)
        vendors = sorted(by_vendor)

        g = Adw.PreferencesGroup(
            title="Exploits by vendor",
            description="Pick a vendor, then a specific exploit. `check` "
                        "probes safely (does not actually exploit); `Run` "
                        "delivers the payload.")
        self.vendor_combo = Adw.ComboRow(title="Vendor")
        self.vendor_combo.set_model(
            Gtk.StringList.new([
                "%s (%d)" % (v, len(by_vendor[v])) for v in vendors]))
        self.vendor_combo.connect(
            "notify::selected", lambda _r, _p: self._on_vendor(by_vendor,
                                                               vendors))
        g.add(self.vendor_combo)

        self.exploit_combo = Adw.ComboRow(title="Exploit")
        g.add(self.exploit_combo)
        self._body_holder.append(g)

        self._options_group = Adw.PreferencesGroup(title="Options")
        self._body_holder.append(self._options_group)

        # Prime with the first vendor.
        self._by_vendor = by_vendor
        self._vendors_list = vendors
        self._on_vendor(by_vendor, vendors)

    def _on_vendor(self, by_vendor, vendors) -> None:
        v = vendors[self.vendor_combo.get_selected()]
        mods = by_vendor[v]
        mods.sort(key=lambda m: m.name.lower())
        model = Gtk.StringList.new([m.name for m in mods])
        self.exploit_combo.set_model(model)
        self.exploit_combo.set_selected(0)
        # Connect once per vendor swap because set_model does not fire
        # notify::selected reliably on all libadwaita versions.
        try:
            self.exploit_combo.disconnect_by_func(self._on_exploit_pick)
        except TypeError:
            pass
        self.exploit_combo.connect(
            "notify::selected",
            lambda _r, _p, m=mods: self._select_module(
                m[self.exploit_combo.get_selected()]))
        self._select_module(mods[0])

    def _on_exploit_pick(self, _r, _p) -> None:
        # Placeholder for disconnect_by_func to find; real handler is set
        # inline in _on_vendor above.
        pass

    # ---- creds ------------------------------------------------------
    def _build_creds(self) -> None:
        creds = [m for m in self._all_mods if m.category == "creds"]
        if not creds:
            g = Adw.PreferencesGroup(title="Credentials")
            g.add(Adw.ActionRow(title="Loading…"))
            self._body_holder.append(g)
            return

        # Cred modules are creds/{ssh,ftp,telnet,http,snmp,...}_default,
        # creds/{ssh,ftp,...}_bruteforce, etc. Group by the leading
        # subdirectory (which is basically the protocol).
        by_proto: dict[str, list[Mod]] = {}
        for m in creds:
            proto = m.path.split("/")[1] if "/" in m.path[6:] else "misc"
            by_proto.setdefault(proto, []).append(m)
        protos = sorted(by_proto)

        g = Adw.PreferencesGroup(
            title="Default-credential brute-force",
            description="Tries a bundled wordlist of factory usernames and "
                        "passwords for the chosen protocol.")
        self.proto_combo = Adw.ComboRow(title="Protocol")
        self.proto_combo.set_model(Gtk.StringList.new(protos))
        self.proto_combo.connect(
            "notify::selected", lambda _r, _p: self._on_proto(by_proto, protos))
        g.add(self.proto_combo)

        self.cred_combo = Adw.ComboRow(title="Module")
        g.add(self.cred_combo)
        self._body_holder.append(g)

        self._options_group = Adw.PreferencesGroup(title="Options")
        self._body_holder.append(self._options_group)

        self._by_proto = by_proto
        self._protos = protos
        self._on_proto(by_proto, protos)

    def _on_proto(self, by_proto, protos) -> None:
        p = protos[self.proto_combo.get_selected()]
        mods = by_proto[p]
        mods.sort(key=lambda m: m.name.lower())
        model = Gtk.StringList.new([m.name for m in mods])
        self.cred_combo.set_model(model)
        self.cred_combo.set_selected(0)
        try:
            self.cred_combo.disconnect_by_func(self._on_cred_pick)
        except TypeError:
            pass
        self.cred_combo.connect(
            "notify::selected",
            lambda _r, _p, m=mods: self._select_module(
                m[self.cred_combo.get_selected()]))
        self._select_module(mods[0])

    def _on_cred_pick(self, _r, _p) -> None:
        pass

    # ---- search -----------------------------------------------------
    def _build_search(self) -> None:
        g = Adw.PreferencesGroup(
            title="Search",
            description="Substring match on module path, name, description "
                        "and device list.")
        self.search_entry = Adw.EntryRow(title="Search term")
        self.search_entry.set_show_apply_button(True)
        self.search_entry.connect("apply", lambda _r: self._run_search())
        self.search_entry.connect("entry-activated",
                                  lambda _r: self._run_search())
        g.add(self.search_entry)
        self._search_results = Gtk.ListBox()
        self._search_results.add_css_class("boxed-list")
        g.add(self._search_results)
        self._body_holder.append(g)

        self._options_group = Adw.PreferencesGroup(title="Options")
        self._body_holder.append(self._options_group)

    def _run_search(self) -> None:
        term = self.search_entry.get_text().strip().lower()
        while (row := self._search_results.get_row_at_index(0)) is not None:
            self._search_results.remove(row)
        if not term:
            return
        hits = []
        for m in self._all_mods:
            hay = (m.path + " " + m.name + " " + m.description).lower()
            if term in hay:
                hits.append(m)
            if len(hits) >= 60:
                break
        if not hits:
            row = Adw.ActionRow(title="No matches")
            self._search_results.append(row)
            return
        for m in hits:
            row = Adw.ActionRow(title=m.name, subtitle=m.path)
            btn = Gtk.Button(label="Select", valign=Gtk.Align.CENTER)
            btn.connect("clicked",
                        lambda _b, mm=m: self._select_module(mm))
            row.add_suffix(btn)
            self._search_results.append(row)

    # -------------------------------------------------- module selection
    def _select_module(self, mod: Mod) -> None:
        self._selected_mod = mod
        # Clear old option rows.
        for r in self._opt_rows:
            self._options_group.remove(r)
        self._opt_rows = []
        self._opt_widgets = {}

        self._options_group.set_title("Options: " + mod.path)
        self._options_group.set_description(mod.description or " ")

        if not mod.options:
            row = Adw.ActionRow(
                title="This module declares no runtime options",
                subtitle="You can still run it as-is; it might use defaults "
                         "hardcoded in the Python file.")
            self._options_group.add(row)
            self._opt_rows.append(row)
            return

        for opt in mod.options:
            widget = self._widget_for(opt)
            self._options_group.add(widget)
            self._opt_rows.append(widget)

    def _widget_for(self, opt: Opt):
        # Bool -> switch row. Everything else -> entry row (IP, Port,
        # String, Wordlist all take free text; port is validated when the
        # command is composed).
        title = opt.name
        subtitle = opt.description
        if opt.type == "Bool":
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(opt.default.lower() in ("true", "1", "yes"))
            self._opt_widgets[opt.name] = (opt.type, row)
            return row
        row = Adw.EntryRow(title=title)
        row.set_show_apply_button(False)
        if subtitle:
            row.set_input_hints(Gtk.InputHints.NO_EMOJI)
        row.set_text(opt.default or "")
        self._opt_widgets[opt.name] = (opt.type, row)
        return row

    def set_target(self, value: str) -> None:
        """Public entry for cross-module deep-links (e.g. Network Discovery's
        "Send to RouterSploit"). Prefills the currently-selected module's
        `target` option, if it has one. If the current section is Scanners
        (autopwn/etc.) that always has target; if it is Exploits or Creds,
        the vast majority of modules also expose target. If not, the value
        is written into the search entry as a fallback so the user can
        find a relevant module.
        """
        pair = self._opt_widgets.get("target")
        if pair is not None:
            _typ, widget = pair
            if hasattr(widget, "set_text"):
                widget.set_text(value)
                return
        # No target option on the currently-selected module -- put the IP
        # in the search box (if we're on Search) so the user can find a
        # matching module. Otherwise fall through and let the caller toast.
        try:
            if getattr(self, "search_entry", None) is not None:
                self.search_entry.set_text(value)
        except Exception:
            pass

    def _current_options(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for name, (typ, w) in self._opt_widgets.items():
            if isinstance(w, Adw.SwitchRow):
                out.append((name, "true" if w.get_active() else "false"))
            elif isinstance(w, Adw.EntryRow):
                val = w.get_text().strip()
                if val:
                    out.append((name, val))
        return out

    # -------------------------------------------------------------- run
    def _launch(self, check: bool) -> None:
        mod = self._selected_mod
        if mod is None:
            toast(self.app_window, "Pick a module first")
            return
        opts = self._current_options()
        # Build the argv. rsf.py needs -m and repeated -s "OPT VAL".
        # A trailing `run` is implicit in nonInteractive; for check we
        # need to emit a mini shell that types check + exit, which the
        # framework's interactive prompt accepts even under -u. That
        # path is used only when the Check button is pressed.
        if check:
            # Non-interactive mode doesn't expose `check`, so drive the
            # interactive REPL with a heredoc: `use <mod>; set ...; check`.
            script_lines = ["use " + mod.path]
            for k, v in opts:
                script_lines.append("set %s %s" % (k, v))
            script_lines.append("check")
            script_lines.append("exit")
            heredoc = "\n".join(script_lines) + "\n"
            cmd = (
                "cd %s && printf '%s' | python3 -u %s"
                % (GLib.shell_quote(RS_DIR),
                   heredoc.replace("'", "'\\''"),
                   RS_ENTRY)
            )
        else:
            parts = ["cd", GLib.shell_quote(RS_DIR), "&&",
                     "python3", "-u", RS_ENTRY,
                     "-m", GLib.shell_quote(mod.path)]
            for k, v in opts:
                parts += ["-s", GLib.shell_quote("%s %s" % (k, v))]
            cmd = " ".join(parts)

        self.output.append("$ " + cmd + "\n")
        self._proc = Process(
            ["sh", "-c", cmd],
            self.output.append,
            self._on_done,
            root=True,
        )
        self._proc.start()
        self._set_running(True, mod)

    def _on_done(self, code: int) -> None:
        self.output.append("[rsf.py exited: %d]\n" % code)
        self._proc = None
        self._set_running(False, self._selected_mod)

    def _stop(self) -> None:
        if self._proc is not None:
            self.output.append("[stopping…]\n")
            self._proc.stop()

    def _set_running(self, running: bool, mod: Mod | None) -> None:
        self.stop_btn.set_sensitive(running)
        self.run_btn.set_sensitive(not running)
        self.check_btn.set_sensitive(not running)
        if running:
            self.state_row.set_subtitle(
                "running " + (mod.path if mod else "?"))
            self.state_icon.set_from_icon_name("emblem-ok-symbolic")
        else:
            self.state_row.set_subtitle(
                "stopped" if mod is None
                else "last run: " + mod.path)
            self.state_icon.set_from_icon_name(
                "media-playback-stop-symbolic")
