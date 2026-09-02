"""The main window: a collapsible sidebar of modules and a content pane.

NavigationSplitView shows the sidebar and content side by side on a wide screen
and collapses to one page at a time on a phone, handling the sidebar's own
scrolling internally, which a plain ScrolledWindow in an OverlaySplitView did
not do once the list grew taller than the screen.
"""
from __future__ import annotations

from gi.repository import Adw, Gio, Gtk

from .module import REGISTRY, NHModule


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="NetHunter Pro")
        self.set_default_size(900, 700)

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(240)
        self.split.set_max_sidebar_width(320)
        self.toasts.set_child(self.split)

        # Collapse to a single page on a phone-width screen.
        bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 600px")
        )
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self._build_sidebar()
        self._build_content()

        if self.modules:
            self._select(self.modules[0])

    def _build_sidebar(self) -> None:
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_vexpand(True)
        self.listbox.connect("row-activated", self._on_row_activated)

        self.modules: list[NHModule] = []
        for cls in REGISTRY:
            module = cls(self)
            self.modules.append(module)

            row = Adw.ActionRow(title=module.title)
            row.set_tooltip_text(module.description)
            row.add_prefix(Gtk.Image.new_from_icon_name(module.icon))
            row.set_activatable(True)
            if not module.available:
                row.add_suffix(
                    Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                )
            row._module = module
            self.listbox.append(row)

        scroller = Gtk.ScrolledWindow(child=self.listbox)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="NetHunter Pro"))
        toolbar = Adw.ToolbarView(content=scroller)
        toolbar.add_top_bar(header)

        self.sidebar_page = Adw.NavigationPage(
            child=toolbar, title="NetHunter Pro")
        self.split.set_sidebar(self.sidebar_page)

    def _build_content(self) -> None:
        self.stack = Gtk.Stack()
        self.content_header = Adw.HeaderBar()
        self.title_label = Gtk.Label()
        self.content_header.set_title_widget(self.title_label)

        toolbar = Adw.ToolbarView(content=self.stack)
        toolbar.add_top_bar(self.content_header)

        self.content_page = Adw.NavigationPage(child=toolbar, title="")
        self.split.set_content(self.content_page)

        for module in self.modules:
            self.stack.add_named(module.get_content(), module.title)

    def _on_row_activated(self, _listbox, row) -> None:
        self._select(row._module)

    def _select(self, module: NHModule) -> None:
        self.stack.set_visible_child_name(module.title)
        self.title_label.set_label(module.title)
        self.content_page.set_title(module.title)
        # On a narrow screen, move to the content page after a pick.
        if self.split.get_collapsed():
            self.split.set_show_content(True)
        # Also update the sidebar selection so the highlighted row matches
        # the visible screen when we jump programmatically (e.g. via
        # NetDiscovery's "Send to nmap" button).
        for row in self.listbox:
            if getattr(row, "_module", None) is module:
                self.listbox.select_row(row)
                break

    # -- programmatic module switch ---------------------------------
    def activate_module(self, module_id: str, target: str | None = None):
        """Switch to a named module, optionally prefilling a target string.

        `module_id` is the Python module basename that the target class
        was registered from (e.g. "nmap", "routersploit"). It matches
        `type(module).__module__.rsplit(".",1)[-1]` for every module the
        app knows about. If a matching module exposes a `set_target(str)`
        method (a lightweight contract we added for cross-module deep-
        links), we call it after switching; otherwise the switch still
        happens and the target is discarded silently. Returns True if
        the switch succeeded, False if no module by that id was found --
        the caller can fall back to copying the value to the clipboard.
        """
        for module in self.modules:
            mod_id = type(module).__module__.rsplit(".", 1)[-1]
            if mod_id != module_id:
                continue
            self._select(module)
            if target is not None:
                try:
                    setter = getattr(module, "set_target", None)
                    if callable(setter):
                        setter(target)
                except Exception:
                    pass
            return True
        return False

    def get_active_module(self):
        """The NHModule currently visible in the content stack.

        Used by NetDiscovery after activate_module() to discover the
        widget names of the destination module for prefill, on the
        off chance a module we linked to does not implement
        set_target() yet.
        """
        name = self.stack.get_visible_child_name()
        for module in self.modules:
            if module.title == name:
                return module
        return None


class NetHunterProApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="org.kali.NetHunterPro")

    def do_activate(self) -> None:
        self._load_css()
        win = self.props.active_window or MainWindow(self)
        win.present()
        self._authorize_once(win)

    def _authorize_once(self, win) -> None:
        """Prompt for root once at startup via the helper."""
        import threading

        from .executor import authorize, helper_available
        from gi.repository import GLib

        if not helper_available():
            return  # no helper; individual commands will use pkexec instead

        def worker() -> None:
            ok = authorize()
            GLib.idle_add(self._authorized_result, win, ok)

        threading.Thread(target=worker, daemon=True).start()

    def _authorized_result(self, win, ok: bool) -> None:
        from .widgets import toast
        if ok:
            toast(win, "Authenticated; root actions are enabled")
        else:
            toast(win, "Not authenticated; root actions will prompt each time")

    def _load_css(self) -> None:
        import importlib.resources as res
        from gi.repository import Gdk, Gtk

        try:
            css = (res.files("nethunter_pro") / "style.css").read_bytes()
        except Exception:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
