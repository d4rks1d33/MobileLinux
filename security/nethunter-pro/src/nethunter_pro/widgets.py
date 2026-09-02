"""Small reusable pieces so the modules stay short and consistent."""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from .executor import Result, run_async


class CommandButton(Adw.ActionRow):
    """A row with a title, a subtitle and a run button.

    Runs a command and drops its output into the page's terminal-style view.
    """

    def __init__(
        self,
        title: str,
        subtitle: str,
        command: str,
        output: "OutputView",
        *,
        root: bool = False,
        button_label: str = "Run",
    ) -> None:
        super().__init__(title=title, subtitle=subtitle)
        self.command = command
        self.output = output
        self.root = root

        self.button = Gtk.Button(label=button_label, valign=Gtk.Align.CENTER)
        self.button.add_css_class("suggested-action")
        self.button.connect("clicked", self._on_clicked)
        self.add_suffix(self.button)
        self.set_activatable_widget(self.button)

    def _on_clicked(self, _button: Gtk.Button) -> None:
        self.button.set_sensitive(False)
        self.output.append(f"$ {self.command}\n")

        def done(result: Result) -> None:
            if result.stdout:
                self.output.append(result.stdout)
            if result.stderr:
                self.output.append(result.stderr)
            self.output.append(
                f"[exit {result.returncode}]\n\n"
            )
            self.button.set_sensitive(True)

        run_async(self.command, done, root=self.root)


class OutputView(Gtk.ScrolledWindow):
    """A monospace, dark, read-only view that command output is appended to.

    Auto-scrolls to the bottom as output arrives, unless the user has scrolled
    up to read something, in which case it leaves the view where it is.
    """

    def __init__(self) -> None:
        # A fixed, generous height rather than vexpand: the whole page is now
        # inside an outer scrolled window, and an expanding child there would
        # have no defined height. The output scrolls within this height and the
        # page scrolls around it.
        super().__init__()
        self.set_min_content_height(240)
        self.set_max_content_height(240)
        self.buffer = Gtk.TextBuffer()
        self.view = Gtk.TextView(
            buffer=self.buffer,
            editable=False,
            monospace=True,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self.view.add_css_class("nh-output")
        self.view.set_top_margin(8)
        self.view.set_bottom_margin(8)
        self.view.set_left_margin(8)
        self.view.set_right_margin(8)
        self.set_child(self.view)

        # Tags for prompt lines and error-ish lines, for readability.
        self.tag_prompt = self.buffer.create_tag("prompt", weight=700)
        self.tag_meta = self.buffer.create_tag("meta", style=2)  # italic

    def append(self, text: str) -> None:
        end = self.buffer.get_end_iter()
        if text.startswith("$ ") or text.startswith("# "):
            self.buffer.insert_with_tags(end, text, self.tag_prompt)
        elif text.startswith("[") and text.rstrip().endswith("]"):
            self.buffer.insert_with_tags(end, text, self.tag_meta)
        else:
            self.buffer.insert(end, text)
        self._autoscroll()

    def _autoscroll(self) -> None:
        adj = self.get_vadjustment()
        # Only stick to the bottom if we are already near it.
        if adj is None:
            return
        at_bottom = adj.get_value() >= (
            adj.get_upper() - adj.get_page_size() - 80
        )
        if at_bottom:
            def scroll():
                a = self.get_vadjustment()
                if a is not None:
                    a.set_value(a.get_upper() - a.get_page_size())
                return False
            from gi.repository import GLib
            GLib.idle_add(scroll)

    def clear(self) -> None:
        self.buffer.set_text("")


def toast(window, message: str) -> None:
    window.toasts.add_toast(Adw.Toast(title=message, timeout=3))


class ToolRunner(Gtk.Box):
    """Live output with a Run/Stop button, plus a "Run in terminal" option.

    Screens build a command and call run() for streamed output inside the app,
    or run_in_terminal() to open it in the system terminal for tools that need
    interaction (menus, prompts). The command is built the same way for both.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.output = OutputView()
        self._proc = None

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self.stop())
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", lambda _b: self.output.clear())
        controls.append(self.stop_btn)
        controls.append(clear_btn)
        self.append(controls)
        self.append(self.output)

    def run(self, command, *, root: bool = False) -> None:
        from .executor import Process

        if self._proc and self._proc.running:
            self.output.append("[a command is already running; stop it first]\n\n")
            return

        shown = command if isinstance(command, str) else " ".join(command)
        self.output.append(f"$ {shown}\n")
        self.stop_btn.set_sensitive(True)

        def on_line(text: str) -> None:
            self.output.append(text)

        def on_done(code: int) -> None:
            self.output.append(f"[exit {code}]\n\n")
            self.stop_btn.set_sensitive(False)

        self._proc = Process(command, on_line, on_done, root=root)
        self._proc.start()

    def run_in_terminal(self, command, *, root: bool = False) -> None:
        from . import terminal_widget

        shown = command if isinstance(command, str) else " ".join(command)
        if not terminal_widget.available():
            self.output.append(
                "[no system terminal found; install kgx or gnome-terminal]\n\n")
            return
        self.output.append(f"$ (in terminal) {shown}\n\n")
        terminal_widget.launch(command, root=root)

    def stop(self) -> None:
        if self._proc and self._proc.running:
            self.output.append("[stopping…]\n")
            self._proc.stop()


def services_banner(app_window, units: list[str],
                    subtitle_extra: str = "") -> Adw.PreferencesGroup:
    """Return a small preferences group meant to sit at the top of a
    module. Lists the systemd services the module needs and shows a
    per-service live status pill plus a "Open Kali Services" button
    that deep-links to the central service manager.

    Every ``systemctl start`` action lives in the Kali Services
    module -- individual modules never touch systemd directly. This
    banner is how a module tells the operator "hey, I need these
    services running; here's where to flip them on".

    ``units`` is the list of systemd unit names (e.g.
    ``['bluetooth', 'avahi-daemon']``); ``subtitle_extra`` is an
    optional string appended to the group description if the module
    wants to add context (e.g. "MUST be off, not on").
    """
    group = Adw.PreferencesGroup(
        title="Required services",
        description=("Managed centrally in Kali Services -- this "
                     "module does not start them for you. " +
                     subtitle_extra).strip())

    # Live status: fire ``systemctl is-active <unit>`` for each and
    # tag the row.
    rows: dict[str, Adw.ActionRow] = {}
    for unit in units:
        row = Adw.ActionRow(title=unit,
                            subtitle="checking…")
        rows[unit] = row
        group.add(row)

        def make_cb(u: str):
            def done(r: Result) -> None:
                out = (r.stdout or "").strip()
                if out == "active":
                    rows[u].set_subtitle("● active")
                elif out == "failed":
                    rows[u].set_subtitle("✗ failed")
                elif out == "inactive":
                    rows[u].set_subtitle("○ inactive")
                else:
                    rows[u].set_subtitle("? " + (out or "unknown"))
            return done
        run_async(["systemctl", "is-active", unit],
                  make_cb(unit), timeout=5)

    # One button at the group level to hop to Kali Services.
    open_row = Adw.ActionRow(
        title="Manage services",
        subtitle="Opens the Kali Services screen")
    open_btn = Gtk.Button(label="Open",
                          valign=Gtk.Align.CENTER)
    open_btn.add_css_class("suggested-action")
    open_btn.connect(
        "clicked",
        lambda _b: app_window.activate_module(
            "kali_services", ",".join(units)))
    open_row.add_suffix(open_btn)
    group.add(open_row)

    return group
