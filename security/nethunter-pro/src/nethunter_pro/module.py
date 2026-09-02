"""Base class every screen inherits from, plus a registry.

Each module is one page in the app. A module declares a title, an icon and
whether the tools it needs are present; the shell greys out ones that are not
usable and shows why.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from .executor import which


class NHModule(Adw.Bin):
    title: str = "Module"
    icon: str = "application-x-executable-symbolic"
    description: str = ""
    # Tools that must be on PATH for this module to do anything.
    required_tools: list[str] = []

    def __init__(self, app_window) -> None:
        super().__init__()
        self.app_window = app_window
        self._built = False

    @property
    def missing_tools(self) -> list[str]:
        return [t for t in self.required_tools if not which(t)]

    @property
    def available(self) -> bool:
        return not self.missing_tools

    def get_content(self) -> Gtk.Widget:
        """Build the page lazily, the first time it is shown.

        Every page is wrapped in a scrolled window so screens with many
        options (wifite, BlueDucky, SET, ...) can be scrolled when they are
        taller than the screen. Pages built around a StatusPage are left as-is,
        since those centre their own content and should not scroll.
        """
        if not self._built:
            if self.missing_tools:
                self.set_child(self._missing_banner())
            else:
                content = self.build()
                if isinstance(content, Adw.StatusPage):
                    self.set_child(content)
                else:
                    scroller = Gtk.ScrolledWindow(child=content)
                    scroller.set_policy(
                        Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                    scroller.set_propagate_natural_height(False)
                    self.set_child(scroller)
            self._built = True
        return self

    def build(self) -> Gtk.Widget:  # overridden by each module
        raise NotImplementedError

    def _missing_banner(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title="Missing tools",
            description=(
                "This screen needs: "
                + ", ".join(self.missing_tools)
                + "\n\nInstall them with apt, then reopen the app."
            ),
        )
        return page


REGISTRY: list[type[NHModule]] = []


def register(cls: type[NHModule]) -> type[NHModule]:
    REGISTRY.append(cls)
    return cls
