"""Terminal UI helpers: colored output, status glyphs, progress bars.

Kept dependency-free (no rich/colorama). Respects NO_COLOR and non-TTY.
"""

from __future__ import annotations

import os
import sys

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"


def _c(text: str, color: str) -> str:
    if not _USE_COLOR:
        return text
    return f"{color}{text}{_C.RESET}"


def bold(t: str) -> str:
    return _c(t, _C.BOLD)


def dim(t: str) -> str:
    return _c(t, _C.DIM)


def red(t: str) -> str:
    return _c(t, _C.RED)


def green(t: str) -> str:
    return _c(t, _C.GREEN)


def yellow(t: str) -> str:
    return _c(t, _C.YELLOW)


def cyan(t: str) -> str:
    return _c(t, _C.CYAN)


def grey(t: str) -> str:
    return _c(t, _C.GREY)


# Status glyphs used across check/test/status output.
GLYPH = {
    "supported": green("\u2713"),      # ✓
    "partial": yellow("\u26a0"),       # ⚠
    "broken": red("\u2717"),           # ✗
    "untested": grey("?"),
    "unsupported": red("\u2717"),
    "not-present": grey("\u2014"),     # —
    "ok": green("\u2713"),
    "warn": yellow("\u26a0"),
    "fail": red("\u2717"),
    "skip": grey("\u2014"),
}


def status_glyph(status: str) -> str:
    return GLYPH.get(status, "?")


def info(msg: str) -> None:
    print(msg)


def note(msg: str) -> None:
    print(dim(msg))


def warn(msg: str) -> None:
    print(f"{yellow('warning:')} {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{red('error:')} {msg}", file=sys.stderr)


def success(msg: str) -> None:
    print(f"{green('ok:')} {msg}")


def header(title: str) -> None:
    print()
    print(bold(title))


def bar(fraction: float, width: int = 20) -> str:
    """Render a progress bar like ``█████████████████░░░``."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "\u2588" * filled + "\u2591" * (width - filled)
