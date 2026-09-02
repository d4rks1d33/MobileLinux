"""Turn pwnagotchi on and off from the UI.

pwnagotchi runs as three systemd services on this port (bettercap, pwngrid-peer
and the agent), wired so starting the agent pulls the other two up. This screen
is just the on/off switch plus a manual/auto choice, a link to the web UI, a
live tail of the log, and the count of captured handshakes -- so the user never
has to open a terminal.

OTG power lives on the USB and Radio screen, so it is not duplicated here.

The important behaviour is on the OFF path: stopping puts the external adapter
back to a normal managed "wlan1", by name, and never touches the internal
wlan0. All of that is in rhodep-pwn-monstop, which the service's ExecStop calls.
"""
from __future__ import annotations

import os

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async, which
from ..module import NHModule, register
from ..widgets import OutputView, toast

AGENT = "rhodep-pwnagotchi.service"
UNITS = ("rhodep-pwn-bettercap.service", "rhodep-pwngrid-peer.service", AGENT)
LOG = "/etc/pwnagotchi/log/pwnagotchi.log"
HANDSHAKES = "/etc/pwnagotchi/handshakes"
UI_URL = "http://127.0.0.1:8080"
CONFIG = "/etc/pwnagotchi/config.toml"
POTFILE = "/etc/pwnagotchi/handshakes/wpa-sec.cracked.potfile"
# Where the exported wordlists land. ~/handshakes so the user owns them.
EXPORT_DIR = os.path.expanduser("~/pwnagotchi-cracked")


@register
class Pwnagotchi(NHModule):
    title = "Pwnagotchi"
    icon = "nethunter-pwnagotchi-symbolic"
    description = "WPA handshake capture on the external adapter (wlan1)"
    # The agent runs from a venv, so pwnagotchi is not on PATH; gate on the
    # things that must be present for the screen to do anything.
    required_tools = ["systemctl", "bettercap"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._tail = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        # ---- power + mode ------------------------------------------------
        g = Adw.PreferencesGroup(
            title="Pwnagotchi",
            description="Captures on wlan1 (the USB TP-Link). Needs OTG on and "
            "the adapter plugged in — see the USB and Radio screen.",
        )
        self.power = Adw.SwitchRow(
            title="Enable pwnagotchi",
            subtitle="Starts bettercap, pwngrid and the agent",
        )
        self.power.connect("notify::active", self._on_power)
        g.add(self.power)

        self.mode = Adw.ComboRow(title="Mode")
        self.mode.set_model(Gtk.StringList.new([
            "Manual (listen only, never transmits)",
            "Auto (associate / deauth / capture)",
        ]))
        self.mode.set_subtitle("Auto transmits. Manual is safe for testing.")
        self.mode.connect("notify::selected", self._on_mode)
        g.add(self.mode)
        box.append(g)

        # ---- status ------------------------------------------------------
        s = Adw.PreferencesGroup(title="Status")
        self.state_row = Adw.ActionRow(title="Service", subtitle="checking…")
        self.state_icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
        self.state_row.add_prefix(self.state_icon)
        s.add(self.state_row)

        self.hs_row = Adw.ActionRow(title="Handshakes captured", subtitle="—")
        s.add(self.hs_row)

        ui_btn = Gtk.Button(label="Open web UI", valign=Gtk.Align.CENTER)
        ui_btn.connect("clicked", lambda _b: self._open_ui())
        ui_row = Adw.ActionRow(
            title="Web UI", subtitle="http://<phone>:8080 (user/pass in config.toml)")
        ui_row.add_suffix(ui_btn)
        s.add(ui_row)
        box.append(s)

        # ---- capture settings -------------------------------------------
        cfg = Adw.PreferencesGroup(
            title="Capture settings",
            description="Changes are written to config.toml. Restart pwnagotchi "
            "(toggle it off and on) to apply.",
        )

        self.deauth = Adw.SwitchRow(
            title="Deauth",
            subtitle="Send deauth frames to force handshakes (auto mode)")
        self.deauth.connect("notify::active", self._on_deauth)
        cfg.add(self.deauth)

        self.associate = Adw.SwitchRow(
            title="Associate",
            subtitle="Send association frames to nearby APs (auto mode)")
        self.associate.connect("notify::active", self._on_associate)
        cfg.add(self.associate)

        self.bands = Adw.ComboRow(title="Bands")
        self.bands.set_model(Gtk.StringList.new([
            "2.4 GHz only",
            "5 GHz only (needs a dual-band adapter)",
            "2.4 + 5 GHz (needs a dual-band adapter)",
        ]))
        self.bands.set_subtitle("Which channels to hop")
        self.bands.connect("notify::selected", self._on_bands)
        cfg.add(self.bands)

        self.autoupload = Adw.SwitchRow(
            title="wpa-sec auto-upload",
            subtitle="Upload captured handshakes to wpa-sec automatically")
        self.autoupload.connect("notify::active", self._on_autoupload)
        cfg.add(self.autoupload)
        box.append(cfg)

        # ---- handshakes: upload now, and clear the confirmed ones -------
        hg = Adw.PreferencesGroup(
            title="Handshakes",
            description="Send captures to wpa-sec on demand, and free the space "
            "they take once wpa-sec has confirmed it has them.")

        self.upload_row = Adw.ActionRow(
            title="Upload to wpa-sec now",
            subtitle="Send every handshake not yet uploaded")
        self.upload_btn = Gtk.Button(label="Upload", valign=Gtk.Align.CENTER)
        self.upload_btn.add_css_class("suggested-action")
        self.upload_btn.connect("clicked", lambda _b: self._upload_now())
        self.upload_row.add_suffix(self.upload_btn)
        hg.add(self.upload_row)

        # Deleting captures is irreversible, so it removes only files wpa-sec
        # has already dealt with: those it confirmed it holds (status
        # SUCCESSFULL) and those it rejected as unusable (status INVALID -- a
        # pcap with no real handshake in it, which will never crack and only
        # takes space). Anything still pending upload is always kept, so nothing
        # is thrown away before wpa-sec has seen it.
        self.clear_row = Adw.ActionRow(
            title="Delete handled handshakes",
            subtitle="Uploaded ones and ones wpa-sec rejected as unusable. "
            "Pending captures are kept.")
        self.clear_btn = Gtk.Button(label="Free space",
                                    valign=Gtk.Align.CENTER)
        self.clear_btn.add_css_class("destructive-action")
        self.clear_btn.connect("clicked", lambda _b: self._clear_uploaded())
        self.clear_row.add_suffix(self.clear_btn)
        hg.add(self.clear_row)
        box.append(hg)

        # ---- wpa-sec API key --------------------------------------------
        k = Adw.PreferencesGroup(
            title="wpa-sec API key",
            description="Your key from wpa-sec.stanev.org. Needed to upload "
            "handshakes and download cracked keys.",
        )
        # For safety the current key is never read back into the app; the row
        # only shows whether one is set. Editing replaces it with a new value.
        self.key_entry = Adw.PasswordEntryRow(title="API key")
        self.key_entry.set_show_apply_button(True)
        self.key_entry.connect("apply", self._on_key_apply)
        k.add(self.key_entry)

        self.key_edit_btn = Gtk.Button(label="Edit", valign=Gtk.Align.CENTER)
        self.key_edit_btn.add_css_class("flat")
        self.key_edit_btn.connect("clicked", lambda _b: self._key_edit_mode())
        self.key_entry.add_suffix(self.key_edit_btn)
        box.append(k)

        # ---- whitelist ---------------------------------------------------
        self.wl_group = Adw.PreferencesGroup(
            title="Whitelist",
            description="Networks pwnagotchi will never attack (by SSID or BSSID).")
        self.wl_entry = Adw.EntryRow(title="Add SSID or BSSID")
        self.wl_entry.set_show_apply_button(True)
        self.wl_entry.connect("apply", self._on_wl_add)
        self.wl_group.add(self.wl_entry)
        self._wl_rows: list[Adw.ActionRow] = []
        box.append(self.wl_group)

        # ---- cracked passwords (wpa-sec) --------------------------------
        c = Adw.PreferencesGroup(
            title="Cracked passwords (wpa-sec)",
            description="Download the keys wpa-sec has cracked from your uploaded "
            "handshakes, and export them as a wordlist.",
        )
        self.cracked_row = Adw.ActionRow(title="Cracked keys", subtitle="—")
        dl = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
        dl.connect("clicked", lambda _b: self._download_cracked())
        self.cracked_row.add_suffix(dl)
        c.add(self.cracked_row)

        exp = Adw.ActionRow(
            title="Export wordlist",
            subtitle="passwords.txt (unique) + cracked.txt (ssid:password)")
        eb = Gtk.Button(label="Export .txt", valign=Gtk.Align.CENTER)
        eb.add_css_class("suggested-action")
        eb.connect("clicked", lambda _b: self._export_wordlist())
        exp.add_suffix(eb)
        c.add(exp)
        box.append(c)

        # ---- live log ----------------------------------------------------
        self.output = OutputView()
        box.append(self.output)

        self._refresh()
        self._refresh_cracked()
        self._load_config()
        # keep status fresh while the screen is open
        self._poll_id = GLib.timeout_add_seconds(5, self._refresh_tick)
        return box

    # ---------------------------------------------------------------- power
    def _on_power(self, row: Adw.SwitchRow, _param) -> None:
        if row.get_active():
            self._set_mode_in_unit()
            self.output.append("$ systemctl start %s\n" % AGENT)
            run_async(["systemctl", "start", AGENT], self._after_action,
                      root=True, timeout=90)
            self._start_tail()
        else:
            self.output.append("$ systemctl stop %s\n" % " ".join(UNITS))
            # Stop the agent (and its Requires) and the two providers explicitly,
            # so nothing is left running and monstop returns wlan1 to managed.
            run_async(["systemctl", "stop", *UNITS], self._after_action,
                      root=True, timeout=60)
            self._stop_tail()

    def _after_action(self, result: Result) -> None:
        if result.stderr:
            self.output.append(result.stderr)
        self._refresh()

    # ------------------------------------------------------ handshakes
    def _upload_now(self) -> None:
        """Ask the wpa-sec plugin to upload everything outstanding, now.

        The plugin uploads on its own when it sees internet, but "now" is what
        the button promises. It scans the handshake directory for .pcap files
        the plugin's own db has not yet marked SUCCESSFULL, POSTs each to
        wpa-sec, and records the result in the same db the plugin uses -- so the
        Delete button downstream sees a consistent picture. The API key and URL
        are read from config so this needs no second copy of them.
        """
        self.upload_btn.set_sensitive(False)
        self.output.append("Uploading handshakes to wpa-sec…\n")
        script = r'''
import os, sqlite3, tomllib, glob, sys
import urllib.request, urllib.error

CFG = "/etc/pwnagotchi/config.toml"
DB = "/etc/pwnagotchi/.wpa_sec_db"
HS = "/etc/pwnagotchi/handshakes"

d = tomllib.load(open(CFG, "rb"))
w = d.get("main", {}).get("plugins", {}).get("wpa-sec", {})
key = w.get("api_key") or ""
url = (w.get("api_url") or "https://wpa-sec.stanev.org").rstrip("/")
if not key:
    print("ERR no API key set"); sys.exit(0)

con = sqlite3.connect(DB)
con.execute("CREATE TABLE IF NOT EXISTS handshakes (path TEXT PRIMARY KEY, status INTEGER)")
done = {r[0] for r in con.execute("SELECT path FROM handshakes WHERE status = 2")}

files = sorted(glob.glob(os.path.join(HS, "*.pcap")))
todo = [f for f in files if f not in done]
if not todo:
    print("OK nothing to upload (%d already confirmed)" % len(done)); sys.exit(0)

ok = bad = 0
for f in todo:
    try:
        with open(f, "rb") as fh:
            data = fh.read()
        boundary = "----nhpro"
        body = (
            ("--%s\r\n" % boundary)
            + 'Content-Disposition: form-data; name="file"; filename="%s"\r\n' % os.path.basename(f)
            + "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + ("\r\n--%s--\r\n" % boundary).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                     "Cookie": "key=%s" % key})
        resp = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        status = 2 if resp.startswith("hcxpcapngtool") else 1
        con.execute(
            "INSERT INTO handshakes(path, status) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET status = excluded.status", (f, status))
        con.commit()
        if status == 2: ok += 1
        else: bad += 1
    except Exception as e:
        print("skip %s: %s" % (os.path.basename(f), e))
con.close()
print("OK uploaded %d, invalid %d, of %d" % (ok, bad, len(todo)))
'''
        run_async(["python3", "-c", script], self._on_upload_done,
                  root=True, timeout=300)

    def _on_upload_done(self, result: Result) -> None:
        out = (result.stdout or "").strip()
        self.output.append(out + "\n")
        self.upload_btn.set_sensitive(True)
        if out.startswith("OK"):
            toast(self.app_window, out[3:].strip() or "Upload finished")
        elif out.startswith("ERR"):
            toast(self.app_window, "wpa-sec: " + out[4:].strip())
        else:
            toast(self.app_window, "Upload finished with problems; see the log")

    def _clear_uploaded(self) -> None:
        """Delete handshakes wpa-sec has finished with: uploaded or rejected.

        Reads the plugin's db for status 2 (SUCCESSFULL) and status 1 (INVALID),
        removes those files, and drops their rows so the db does not point at
        files that are gone. Pending captures (status 0, or not yet in the db)
        are never touched, so nothing is deleted before wpa-sec has seen it.
        Guarded behind a confirmation dialog because it is irreversible.
        """
        dlg = Adw.MessageDialog(
            transient_for=self.app_window,
            heading="Free space from handled handshakes?",
            body="This removes handshakes wpa-sec has uploaded, and ones it "
            "rejected as unusable. Captures still waiting to upload are kept. "
            "This cannot be undone.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.connect("response", self._clear_uploaded_confirmed)
        dlg.present()

    def _clear_uploaded_confirmed(self, _dlg, response: str) -> None:
        if response != "delete":
            return
        self.clear_btn.set_sensitive(False)
        script = r'''
import os, sqlite3
DB = "/etc/pwnagotchi/.wpa_sec_db"
con = sqlite3.connect(DB)
# status 2 = uploaded and confirmed, 1 = rejected as unusable. Both are done
# with; status 0 (pending) and anything not in the db are left alone.
rows = [r[0] for r in con.execute("SELECT path FROM handshakes WHERE status IN (1, 2)")]
up = bad = freed = 0
for p in rows:
    try:
        st = con.execute("SELECT status FROM handshakes WHERE path = ?", (p,)).fetchone()[0]
        if os.path.exists(p):
            freed += os.path.getsize(p)
            os.remove(p)
        con.execute("DELETE FROM handshakes WHERE path = ?", (p,))
        if st == 2: up += 1
        else: bad += 1
    except Exception as e:
        print("skip %s: %s" % (os.path.basename(p), e))
con.commit(); con.close()
print("OK deleted %d uploaded and %d unusable, freed %d KB" % (up, bad, freed // 1024))
'''
        run_async(["python3", "-c", script], self._on_clear_done,
                  root=True, timeout=60)

    def _on_clear_done(self, result: Result) -> None:
        out = (result.stdout or "").strip()
        self.output.append(out + "\n")
        self.clear_btn.set_sensitive(True)
        if out.startswith("OK"):
            toast(self.app_window, out[3:].strip())
        else:
            toast(self.app_window, "Nothing deleted; see the log")
        self._refresh()

    # ---------------------------------------------------------------- mode
    def _on_mode(self, row: Adw.ComboRow, _param) -> None:
        self._set_mode_in_unit()
        if self.power.get_active():
            toast(self.app_window, "Restart pwnagotchi to apply the mode change")

    def _set_mode_in_unit(self) -> None:
        # The mode is the launcher argument in the unit's ExecStart. Write a
        # drop-in rather than editing the unit, so an upgrade of the package
        # does not fight us.
        arg = "auto" if self.mode.get_selected() == 1 else "manual"
        dropin = (
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=/usr/local/sbin/rhodep-pwn-launcher %s\n" % arg
        )
        script = (
            "install -d /etc/systemd/system/%s.d && "
            "printf '%%s' %s > /etc/systemd/system/%s.d/10-mode.conf && "
            "systemctl daemon-reload"
        ) % (AGENT, GLib.shell_quote(dropin), AGENT)
        run_async(["sh", "-c", script], lambda _r: None, root=True, timeout=20)

    # -------------------------------------------------------------- config
    # config.toml is root-owned. All reads/writes go through a small tomlkit
    # script in a root shell (tomlkit is present on the device and preserves
    # comments/formatting, which sed cannot for nested tables and arrays).
    #
    # 2.4 GHz + the non-DFS 5 GHz channels (DFS is no-IR: cannot transmit).
    _CH_24 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    _CH_5 = [36, 40, 44, 48, 149, 153, 157, 161, 165]

    def _cfg_read(self, done) -> None:
        # Emit one line per value so the callback can parse without importing
        # anything: has_key, deauth, associate, dualband, autoupload, then the
        # whitelist entries, one per line, prefixed "wl:".
        script = r'''
import tomllib
try:
    d = tomllib.load(open(%r, "rb"))
except Exception:
    d = {}
main = d.get("main", {})
per = main.get("personality", d.get("personality", {}))
plugins = main.get("plugins", {})
wpasec = plugins.get("wpa-sec", {})
def b(v): return "1" if v else "0"
print("has_key:" + b(bool(wpasec.get("api_key"))))
print("deauth:" + b(per.get("deauth", True)))
print("associate:" + b(per.get("associate", True)))
chans = per.get("channels", [])
print("has_24:" + b(any(c <= 14 for c in chans)))
print("has_5:" + b(any(c >= 36 for c in chans)))
print("autoupload:" + b(wpasec.get("enabled", False)))
for n in main.get("whitelist", []):
    print("wl:" + str(n))
''' % CONFIG
        run_async(["python3", "-c", script], done, root=True, timeout=15)

    def _cfg_write(self, py_body: str, done=None) -> None:
        # py_body operates on `d` (the tomlkit doc) and `main`/`per`/`wpasec`
        # tables, then the wrapper writes it back atomically.
        script = (
            "import tomlkit\n"
            "f = %r\n"
            "d = tomlkit.loads(open(f).read())\n"
            "main = d.setdefault('main', tomlkit.table())\n"
            "per = d.get('personality') or main.setdefault('personality', tomlkit.table())\n"
            "plugins = main.setdefault('plugins', tomlkit.table())\n"
            "wpasec = plugins.setdefault('wpa-sec', tomlkit.table())\n"
            "%s\n"
            "open(f, 'w').write(tomlkit.dumps(d))\n"
        ) % (CONFIG, py_body)
        run_async(["python3", "-c", script],
                  done or (lambda _r: None), root=True, timeout=15)

    def _load_config(self) -> None:
        def done(result: Result) -> None:
            vals = {}
            wl = []
            for line in (result.stdout or "").splitlines():
                if line.startswith("wl:"):
                    wl.append(line[3:])
                elif ":" in line:
                    key, v = line.split(":", 1)
                    vals[key] = v
            # sync switches without firing their handlers
            self._set_switch(self.deauth, self._on_deauth, vals.get("deauth") == "1")
            self._set_switch(self.associate, self._on_associate,
                             vals.get("associate") == "1")
            self._set_switch(self.autoupload, self._on_autoupload,
                             vals.get("autoupload") == "1")
            self.bands.handler_block_by_func(self._on_bands)
            has_24 = vals.get("has_24") == "1"
            has_5 = vals.get("has_5") == "1"
            # 0 = 2.4 only, 1 = 5 only, 2 = both
            sel = 2 if (has_24 and has_5) else (1 if has_5 else 0)
            self.bands.set_selected(sel)
            self.bands.handler_unblock_by_func(self._on_bands)
            # api key: only whether one is set, never the value
            if vals.get("has_key") == "1":
                self._key_locked()
            else:
                self._key_unlocked()
            self._render_whitelist(wl)
        self._cfg_read(done)

    def _set_switch(self, row, handler, value: bool) -> None:
        row.handler_block_by_func(handler)
        row.set_active(value)
        row.handler_unblock_by_func(handler)

    def _apply_toast(self) -> None:
        if self.power.get_active():
            toast(self.app_window, "Restart pwnagotchi to apply")

    # -- toggles
    def _on_deauth(self, row, _p) -> None:
        self._cfg_write("per['deauth'] = %s" % bool(row.get_active()),
                        lambda _r: self._apply_toast())

    def _on_associate(self, row, _p) -> None:
        self._cfg_write("per['associate'] = %s" % bool(row.get_active()),
                        lambda _r: self._apply_toast())

    def _on_autoupload(self, row, _p) -> None:
        self._cfg_write("wpasec['enabled'] = %s" % bool(row.get_active()),
                        lambda _r: self._apply_toast())

    def _on_bands(self, row, _p) -> None:
        sel = row.get_selected()
        if sel == 1:
            chans = self._CH_5
        elif sel == 2:
            chans = self._CH_24 + self._CH_5
        else:
            chans = self._CH_24
        self._cfg_write("per['channels'] = %r" % chans,
                        lambda _r: self._apply_toast())

    # -- api key
    # Lock the *text field* (set_editable), never the whole row, so the Edit
    # button stays clickable. The value is never read back; a set key just
    # shows placeholder dots and a "set" title.
    def _key_locked(self) -> None:
        # A set key is shown as a row of placeholder dots rather than an empty
        # field. The value is still never read back from config -- the dots are
        # fixed filler -- but an empty box reads as "not configured" even with a
        # "set" title, which is exactly the confusion this avoids. Editing
        # clears them and lets a new value be typed.
        self.key_entry.set_editable(False)
        self.key_entry.set_text("\u2022" * 12)
        self.key_entry.set_title("API key — set (tap Edit to change)")
        self.key_edit_btn.set_visible(True)

    def _key_unlocked(self) -> None:
        self.key_entry.set_editable(True)
        self.key_entry.set_text("")
        self.key_entry.set_title("API key — not set")
        self.key_edit_btn.set_visible(False)

    def _key_edit_mode(self) -> None:
        self.key_entry.set_editable(True)
        self.key_entry.set_title("New API key")
        self.key_entry.set_text("")
        self.key_edit_btn.set_visible(False)
        self.key_entry.grab_focus()

    def _on_key_apply(self, row) -> None:
        val = row.get_text().strip()
        if not val:
            return
        # write it, then lock the field again and forget the value
        self._cfg_write("wpasec['api_key'] = %r" % val, self._key_saved)

    def _key_saved(self, result: Result) -> None:
        if result.ok:
            toast(self.app_window, "API key saved")
            self._key_locked()
        else:
            toast(self.app_window, "Could not save the key")
            self.output.append(result.stderr or "")

    # -- whitelist
    def _render_whitelist(self, names: list[str]) -> None:
        for r in self._wl_rows:
            self.wl_group.remove(r)
        self._wl_rows = []
        for name in names:
            row = Adw.ActionRow(title=name)
            btn = Gtk.Button(icon_name="user-trash-symbolic",
                             valign=Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, n=name: self._on_wl_remove(n))
            row.add_suffix(btn)
            self.wl_group.add(row)
            self._wl_rows.append(row)

    def _on_wl_add(self, row) -> None:
        name = row.get_text().strip()
        if not name:
            return
        row.set_text("")
        body = (
            "wl = main.get('whitelist') or main.setdefault('whitelist', tomlkit.array())\n"
            "if %r not in list(wl): wl.append(%r)\n"
        ) % (name, name)
        self._cfg_write(body, lambda _r: self._load_config())

    def _on_wl_remove(self, name: str) -> None:
        body = (
            "wl = main.get('whitelist') or tomlkit.array()\n"
            "vals = [x for x in list(wl) if x != %r]\n"
            "main['whitelist'] = vals\n"
        ) % name
        self._cfg_write(body, lambda _r: self._load_config())

    # -------------------------------------------------------------- status
    def _refresh_tick(self) -> bool:
        self._refresh()
        return True

    def _refresh(self) -> None:
        def done(result: Result) -> None:
            active = result.stdout.strip() == "active"
            self.power.handler_block_by_func(self._on_power)
            self.power.set_active(active)
            self.power.handler_unblock_by_func(self._on_power)
            if active:
                self.state_row.set_subtitle("running")
                self.state_icon.set_from_icon_name("emblem-ok-symbolic")
                if self._tail is None:
                    self._start_tail()
            else:
                self.state_row.set_subtitle("stopped")
                self.state_icon.set_from_icon_name("media-playback-stop-symbolic")
        run_async(["systemctl", "is-active", AGENT], done, root=False, timeout=10)

        def hs(result: Result) -> None:
            n = result.stdout.strip() or "0"
            self.hs_row.set_subtitle("%s in %s" % (n, HANDSHAKES))
        run_async(
            ["sh", "-c",
             "ls %s/*.pcap 2>/dev/null | wc -l" % GLib.shell_quote(HANDSHAKES)],
            hs, root=False, timeout=10)

    # ----------------------------------------------------------------- log
    def _start_tail(self) -> None:
        if self._tail is not None:
            return
        if not os.path.exists(LOG):
            return
        self.output.append("— following %s —\n" % LOG)
        self._tail = Process(
            ["tail", "-n", "20", "-F", LOG],
            self.output.append,
            self._tail_done,
            root=True,   # the log is root-owned
        )
        self._tail.start()

    def _tail_done(self, _code: int) -> None:
        self._tail = None

    def _stop_tail(self) -> None:
        if self._tail is not None:
            self._tail.stop()
            self._tail = None

    # -------------------------------------------------- cracked / wordlist
    def _download_cracked(self) -> None:
        # The api_key lives in config.toml, which is root-readable only, and the
        # download hits wpa-sec with it. Do the whole thing in one root shell so
        # the key never passes through this process: read the key, curl the
        # potfile, print how many lines came back.
        self.output.append("$ downloading cracked keys from wpa-sec…\n")
        script = r'''
key=$(sed -n 's/^[[:space:]]*api_key[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' %s | head -1)
url=$(sed -n 's/^[[:space:]]*api_url[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' %s | head -1)
[ -n "$url" ] || url="https://wpa-sec.stanev.org"
[ -n "$key" ] || { echo "no api_key in config.toml"; exit 1; }
curl -s --max-time 40 --cookie "key=$key" "${url%%/}/?api&dl=1" -o %s || { echo "download failed"; exit 1; }
echo "downloaded $(wc -l < %s) cracked entries to %s"
''' % (CONFIG, CONFIG, POTFILE, POTFILE, POTFILE)

        def done(result: Result) -> None:
            self.output.append(result.stdout or "")
            self.output.append(result.stderr or "")
            self._refresh_cracked()

        run_async(["sh", "-c", script], done, root=True, timeout=60)

    def _refresh_cracked(self) -> None:
        # Count entries in the potfile (root-owned), report on the row.
        def done(result: Result) -> None:
            n = (result.stdout or "0").strip()
            self.cracked_row.set_subtitle(
                "%s keys in potfile" % n if n and n != "0"
                else "none yet — download, or capture more")
        run_async(
            ["sh", "-c",
             "test -f %s && grep -c ':' %s || echo 0" % (POTFILE, POTFILE)],
            done, root=True, timeout=10)

    def _export_wordlist(self) -> None:
        # potfile format: bssid:station_mac:ssid:password. Produce two files the
        # user owns: passwords.txt (unique passwords, a real wordlist) and
        # cracked.txt (ssid:password, human-readable). Done in a root shell
        # because the potfile is root-owned, then chowned to the invoking user.
        user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "kali"
        script = r'''
set -e
POT=%s
OUT=%s
USER=%s
[ -f "$POT" ] || { echo "no potfile; download first"; exit 1; }
mkdir -p "$OUT"
# passwords.txt: field 4 onward (passwords can contain ':'), non-empty, unique,
# preserving first-seen order.
awk -F: 'NF>=4 { p=$4; for(i=5;i<=NF;i++) p=p":"$i; if(p!="" && !seen[p]++) print p }' "$POT" > "$OUT/passwords.txt"
# cracked.txt: ssid:password, readable.
awk -F: 'NF>=4 { p=$4; for(i=5;i<=NF;i++) p=p":"$i; if(p!="") print $3":"p }' "$POT" > "$OUT/cracked.txt"
# hand ownership to the user so they can read/edit without root.
chown -R "$USER":"$USER" "$OUT" 2>/dev/null || true
echo "wrote $(wc -l < "$OUT/passwords.txt") unique passwords to $OUT/passwords.txt"
echo "wrote $(wc -l < "$OUT/cracked.txt") entries to $OUT/cracked.txt"
''' % (POTFILE, EXPORT_DIR, user)

        self.output.append("$ exporting wordlist…\n")

        def done(result: Result) -> None:
            self.output.append(result.stdout or "")
            self.output.append(result.stderr or "")
            if result.ok:
                toast(self.app_window, "Wordlist written to %s" % EXPORT_DIR)

        run_async(["sh", "-c", script], done, root=True, timeout=30)

    # ------------------------------------------------------------------ ui
    def _open_ui(self) -> None:
        try:
            Gtk.UriLauncher.new(UI_URL).launch(self.app_window, None, None, None)
        except Exception:
            toast(self.app_window, "Open %s in a browser" % UI_URL)
