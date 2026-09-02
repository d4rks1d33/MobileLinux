"""driftnet on the wire: pull images out of unencrypted HTTP the AP relays.

driftnet in adjunct mode (`-a`) does not open its own X window: it writes each
captured image to a temp directory and prints its filename on stdout. That is
what this screen uses -- a GTK app cannot rely on driftnet's own display, and
the temp directory it manages would delete images to keep itself small anyway.
A small watchdog reads those filenames and hard-links each one into a
permanent folder the user owns (~/Pictures/driftnet-images by default), so the
images survive both driftnet's rotation and the module being stopped.

The HTTP viewer (`-w`) is exposed as an alternative mode: driftnet then serves
a live-updating gallery at http://<phone>:9090, which is nicer than opening a
file manager if you want to watch captures roll in from another device on the
network. It cannot be combined with adjunct mode -- driftnet accepts one output
at a time -- so this screen picks one.

TLS killed most of this attack ten years ago: only unencrypted traffic that
crosses the interface shows up. It stays useful against captive-portal walled
gardens (the AP's HTTP redirector traffic to `connectivitycheck.gstatic.com`
and friends), and against clients that fall back to HTTP -- e.g. old apps and
some old CDNs. Pair it with the phishkin3 AP for a live view of what victims'
devices are trying to load in the background.
"""
from __future__ import annotations

import os

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async, which
from ..module import NHModule, register
from ..widgets import OutputView, toast

DRIFTNET = "driftnet"
# User-owned save directory. driftnet in adjunct mode manages its OWN temp
# directory under /tmp; the watchdog links out of there into this one so the
# images are still around after driftnet is stopped and after driftnet's own
# rotation (-m below) has evicted them from the temp dir. `~` is expanded at
# runtime to the invoking user's home even though this module runs as root:
# see the SUDO_USER dance in _start_cmd.
DEFAULT_SAVE_DIR = "~/Pictures/driftnet-images"
# Cap on how many images driftnet keeps in its OWN temp dir. This is not a cap
# on the save directory -- the watchdog links out of the temp dir before
# driftnet evicts, so nothing is lost. 500 keeps the tempfs footprint bounded
# on a phone; well beyond driftnet's default of 100.
TEMP_MAX = 500
# Port for the -w viewer mode. driftnet's default is 9090; kept in sync so the
# "Open web UI" button lands on the right URL.
HTTP_PORT = 9090


@register
class Driftnet(NHModule):
    title = "driftnet"
    icon = "camera-photo-symbolic"
    description = "Grab images from unencrypted HTTP on the wire"
    required_tools = ["driftnet"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._count_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        # ---- capture --------------------------------------------------
        cap = Adw.PreferencesGroup(
            title="Capture",
            description=(
                "Sniffs image bytes off UNENCRYPTED HTTP only. HTTPS -- "
                "which is almost everything today, including Google Images -- "
                "does not show up. It stays useful against captive-portal "
                "walled gardens, old apps, and clients that fall back to "
                "HTTP; pair with the phishkin3 AP so victim traffic transits "
                "the capture interface."),
        )

        # Blank = driftnet's own "all interfaces" default. Pre-fill with the
        # AP interface since that is the common lab use.
        self.iface = Adw.EntryRow(title="Interface")
        self.iface.set_text("wlan1")
        cap.add(self.iface)

        # tcpdump filter appended to driftnet's own `tcp and (…)` wrapper. Kept
        # optional and mentioned as such in the subtitle so the field can be
        # left empty for the common "everything" case.
        self.filter_entry = Adw.EntryRow(
            title="Extra filter (tcpdump syntax, optional)")
        cap.add(self.filter_entry)

        self.save_entry = Adw.EntryRow(title="Save images to")
        self.save_entry.set_text(DEFAULT_SAVE_DIR)
        cap.add(self.save_entry)

        self.mode = Adw.ComboRow(title="Output")
        self.mode.set_model(Gtk.StringList.new([
            "Save to folder (headless)",
            "Web viewer on :%d" % HTTP_PORT,
        ]))
        self.mode.set_subtitle(
            "Headless is the simple case; the viewer is a live gallery you can "
            "open on any device on the network.")
        cap.add(self.mode)
        box.append(cap)

        # ---- controls -------------------------------------------------
        actions = Adw.PreferencesGroup()

        self.state_row = Adw.ActionRow(
            title="driftnet", subtitle="stopped")
        self.state_icon = Gtk.Image.new_from_icon_name(
            "media-playback-stop-symbolic")
        self.state_row.add_prefix(self.state_icon)
        actions.add(self.state_row)

        self.count_row = Adw.ActionRow(
            title="Images captured this session",
            subtitle="—")
        actions.add(self.count_row)

        self.toggle_btn = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.toggle_btn.add_css_class("suggested-action")
        self.toggle_btn.connect("clicked", self._on_toggle)
        toggle_row = Adw.ActionRow(
            title="Run driftnet",
            subtitle="Starts the sniffer and the save-to-folder watchdog")
        toggle_row.add_suffix(self.toggle_btn)
        actions.add(toggle_row)

        open_folder = Adw.ActionRow(
            title="Open images folder",
            subtitle="Show the save directory in Files")
        of_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        of_btn.connect("clicked", lambda _b: self._open_folder())
        open_folder.add_suffix(of_btn)
        actions.add(open_folder)

        self.open_web = Adw.ActionRow(
            title="Open web viewer",
            subtitle="http://127.0.0.1:%d" % HTTP_PORT)
        ow_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        ow_btn.connect("clicked", lambda _b: self._open_web())
        self.open_web.add_suffix(ow_btn)
        # Only meaningful in web mode WHILE driftnet is running; kept hidden
        # otherwise so the user does not try to open a URL that will 404
        # (which was the first UX confusion this module hit).
        self.open_web.set_visible(False)
        actions.add(self.open_web)

        box.append(actions)

        # ---- live log --------------------------------------------------
        self.output = OutputView()
        box.append(self.output)
        return box

    # ---------------------------------------------------------- lifecycle
    def _on_toggle(self, _b) -> None:
        if self._proc is not None and self._proc.running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        iface = self.iface.get_text().strip()
        filt = self.filter_entry.get_text().strip()
        save = self.save_entry.get_text().strip() or DEFAULT_SAVE_DIR
        web_mode = self.mode.get_selected() == 1

        cmd = self._start_cmd(iface, filt, save, web_mode)
        self.output.append("$ %s\n" % cmd)
        self._proc = Process(
            ["sh", "-c", cmd],
            self.output.append,
            self._on_done,
            root=True,   # pcap + writing to the user's home need it
        )
        self._proc.start()

        self.state_row.set_subtitle(
            "running (web viewer on :%d)" % HTTP_PORT if web_mode
            else "running (saving to %s)" % save)
        self.state_icon.set_from_icon_name("emblem-ok-symbolic")
        self.toggle_btn.set_label("Stop")
        self.toggle_btn.remove_css_class("suggested-action")
        self.toggle_btn.add_css_class("destructive-action")
        # Reveal the web-viewer shortcut only when it will actually work:
        # driftnet's -w server is running, and only then.
        self.open_web.set_visible(web_mode)
        # poll the save dir for the count; the web viewer has no local count
        if not web_mode:
            self._count_id = GLib.timeout_add_seconds(
                3, self._tick_count, save)
            self._tick_count(save)

    def _on_done(self, code: int) -> None:
        self.output.append("[driftnet exited: %d]\n" % code)
        self._proc = None
        if self._count_id is not None:
            GLib.source_remove(self._count_id)
            self._count_id = None
        self.state_row.set_subtitle("stopped")
        self.state_icon.set_from_icon_name("media-playback-stop-symbolic")
        self.toggle_btn.set_label("Start")
        self.toggle_btn.remove_css_class("destructive-action")
        self.toggle_btn.add_css_class("suggested-action")
        self.open_web.set_visible(False)

    def _stop(self) -> None:
        if self._proc is not None:
            self.output.append("[stopping…]\n")
            self._proc.stop()

    # ---- shell composition ---------------------------------------------
    def _start_cmd(
        self, iface: str, filt: str, save: str, web_mode: bool
    ) -> str:
        """Compose the driftnet+watchdog shell one-liner.

        Root-side setup: pick the invoking user's home for `~` expansion via
        SUDO_USER (the helper streams as root but records the original user),
        create the save dir owned by that user, and launch driftnet in either
        adjunct mode (headless) or HTTP mode. In adjunct mode the watchdog
        reads stdout and links every announced file into the save dir with a
        timestamp prefix so filenames stay unique across runs; it also chowns
        each new file to the user so the folder is not root-owned.
        """
        # Resolve the target user's home even though we run as root. The DBus
        # helper preserves SUDO_USER when it invokes the wrapped command; the
        # `getent` lookup is portable across "kali" (home under /home) and
        # anything else. `USER_HOME` is the fully expanded home dir.
        user_setup = (
            'U="${SUDO_USER:-kali}"; '
            'USER_HOME=$(getent passwd "$U" | cut -d: -f6); '
            '[ -n "$USER_HOME" ] || USER_HOME=/home/"$U"; '
        )
        # Expand ~ against USER_HOME rather than root's. Users typing an
        # absolute path (or a subdir of ~) get the sensible result. POSIX
        # parameter expansion does NOT interpret `~` as a special character
        # in a pattern; `${SAVE#~}` strips a literal leading `~` byte, but
        # only if that byte is unquoted here. The safer form uses `case` to
        # detect the prefix and then a plain substring cut with `expr` (POSIX)
        # to drop the leading `~` -- avoiding the `${VAR#~}` foot-gun where
        # some shells refuse to treat `~` as an ordinary character in the
        # pattern.
        save_esc = GLib.shell_quote(save)
        expand_save = (
            'SAVE=%s; '
            'case "$SAVE" in '
            '"~") SAVE="$USER_HOME" ;; '
            '"~/"*) SAVE="$USER_HOME/$(expr "$SAVE" : "~/\\(.*\\)")" ;; '
            'esac; '
            'mkdir -p "$SAVE"; chown "$U":"$U" "$SAVE" 2>/dev/null || true; '
        ) % save_esc

        # driftnet flags.
        # -a  : adjunct mode, no GTK window; announces filenames on stdout.
        # -m  : cap on driftnet's OWN temp dir; the watchdog links out of it
        #       into $SAVE before driftnet rotates, so the cap does not
        #       shrink the permanent collection.
        # -d  : explicit temp dir so the watchdog knows where to find files.
        # -i  : capture interface; blank means driftnet's default (all).
        # -v  : verbose start-up banner to the log.
        # For -w mode: driftnet serves an HTML/JSON gallery on HTTP_PORT and
        # ignores -a/-d; we still pass -i and the filter.
        iface_flag = "-i " + GLib.shell_quote(iface) if iface else ""
        # driftnet appends its own `tcp and (…)` around the filter, so a bare
        # tcpdump-style expression is fine here; guard against empty which
        # would produce `tcp and ()`.
        filt_arg = " " + filt if filt else ""

        if web_mode:
            # Foreground driftnet: the helper wraps stdout back to us and
            # `Process.stop()` kills the process group when the user hits Stop.
            return (
                user_setup
                + expand_save
                + "exec driftnet -v -w -W %d %s%s"
                % (HTTP_PORT, iface_flag, filt_arg)
            )

        # Adjunct + watchdog. driftnet writes files to $TMP and prints each
        # new filename on its own stdout line (that is how -a is defined in
        # the man page). The watchdog reads stdin (piped from driftnet) and
        # copies each announced file into $SAVE with a timestamp prefix so
        # nothing collides across runs. `ln -f` first because a hard link is
        # instant and avoids doubling disk use inside the same filesystem;
        # cp -p is the fallback across filesystems. The final `wait` keeps
        # the shell alive so `Process.stop()` -> SIGTERM stops the pipeline
        # cleanly.
        return (
            user_setup
            + expand_save
            + "TMP=$(mktemp -d /tmp/driftnet-XXXXXX); "
              "trap 'rm -rf \"$TMP\"' EXIT; "
              "echo \"[temp: $TMP]\"; echo \"[save: $SAVE]\"; "
              "driftnet -v -a -m %d -d \"$TMP\" %s%s | "
              "while IFS= read -r f; do "
              "  case \"$f\" in "
              "    \"$TMP\"/*) "
              "      base=$(basename \"$f\"); "
              "      dst=\"$SAVE/$(date +%%Y%%m%%d-%%H%%M%%S)-$base\"; "
              "      ln -f \"$f\" \"$dst\" 2>/dev/null "
              "        || cp -p \"$f\" \"$dst\" 2>/dev/null || continue; "
              "      chown \"$U\":\"$U\" \"$dst\" 2>/dev/null; "
              "      echo \"[saved $(basename \"$dst\")]\"; "
              "      ;; "
              "    *) echo \"$f\";; "
              "  esac; "
              "done"
              % (TEMP_MAX, iface_flag, filt_arg)
        )

    # ---- helpers --------------------------------------------------------
    def _tick_count(self, save: str) -> bool:
        # Count images in the save dir. Runs as the login user (no root
        # needed to read the user's own home); the dir is world-readable in
        # the common case, and if it is not, the count silently reads 0.
        script = (
            'S=%s; '
            'case "$S" in '
            '"~") S="$HOME" ;; '
            '"~/"*) S="$HOME/$(expr "$S" : "~/\\(.*\\)")" ;; '
            'esac; '
            'find "$S" -maxdepth 1 -type f \\( -name "*.jpg" -o '
            '-name "*.jpeg" -o -name "*.gif" -o -name "*.png" -o '
            '-name "*.webp" -o -name "*.bmp" \\) 2>/dev/null | wc -l'
        ) % GLib.shell_quote(save)

        def done(result: Result) -> None:
            n = (result.stdout or "").strip() or "0"
            self.count_row.set_subtitle("%s in %s" % (n, save))

        run_async(["sh", "-c", script], done, root=False, timeout=8)
        return self._proc is not None and self._proc.running

    def _open_folder(self) -> None:
        # Use xdg-open in the login user's session; opening from root under
        # pkexec would not know which desktop file to route to.
        save = (self.save_entry.get_text().strip() or DEFAULT_SAVE_DIR)
        script = (
            'S=%s; '
            'case "$S" in '
            '"~") S="$HOME" ;; '
            '"~/"*) S="$HOME/$(expr "$S" : "~/\\(.*\\)")" ;; '
            'esac; '
            'mkdir -p "$S"; xdg-open "$S" >/dev/null 2>&1 &'
        ) % GLib.shell_quote(save)
        run_async(["sh", "-c", script], lambda _r: None,
                  root=False, timeout=5)

    def _open_web(self) -> None:
        url = "http://127.0.0.1:%d" % HTTP_PORT
        try:
            Gtk.UriLauncher.new(url).launch(self.app_window, None, None, None)
        except Exception:
            toast(self.app_window, "Open %s in a browser" % url)
