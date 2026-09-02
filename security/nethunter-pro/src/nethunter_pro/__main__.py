import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from . import modules  # noqa: F401,E402  (registers modules on import)
from .window import NetHunterProApp  # noqa: E402


def main() -> int:
    return NetHunterProApp().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
