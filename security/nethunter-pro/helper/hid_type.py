#!/usr/bin/env python3
"""Type text or run a DuckyScript by writing HID reports to a gadget device.

Self-contained so the app does not depend on an external duckhunter/hid tool.
Usage:
    hid_type.py /dev/hidg0 text "some text to type"
    hid_type.py /dev/hidg0 ducky <<'EOF'
    STRING hello
    ENTER
    EOF
"""
from __future__ import annotations

import sys
import time

# US HID keycodes: char -> (modifier, keycode)
_SHIFT = 0x02
_BASE = {
    "a": 0x04, "b": 0x05, "c": 0x06, "d": 0x07, "e": 0x08, "f": 0x09,
    "g": 0x0a, "h": 0x0b, "i": 0x0c, "j": 0x0d, "k": 0x0e, "l": 0x0f,
    "m": 0x10, "n": 0x11, "o": 0x12, "p": 0x13, "q": 0x14, "r": 0x15,
    "s": 0x16, "t": 0x17, "u": 0x18, "v": 0x19, "w": 0x1a, "x": 0x1b,
    "y": 0x1c, "z": 0x1d,
    "1": 0x1e, "2": 0x1f, "3": 0x20, "4": 0x21, "5": 0x22, "6": 0x23,
    "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "\n": 0x28, "\r": 0x28, "\x1b": 0x29, "\t": 0x2b, " ": 0x2c,
    "-": 0x2d, "=": 0x2e, "[": 0x2f, "]": 0x30, "\\": 0x31, ";": 0x33,
    "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
}
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}
_NAMED = {
    "ENTER": (0, 0x28), "RETURN": (0, 0x28), "ESC": (0, 0x29),
    "ESCAPE": (0, 0x29), "TAB": (0, 0x2b), "SPACE": (0, 0x2c),
    "BACKSPACE": (0, 0x2a), "DELETE": (0, 0x4c), "UP": (0, 0x52),
    "DOWN": (0, 0x51), "LEFT": (0, 0x50), "RIGHT": (0, 0x4f),
    "HOME": (0, 0x4a), "END": (0, 0x4d),
}
_MODS = {"CTRL": 0x01, "CONTROL": 0x01, "SHIFT": 0x02, "ALT": 0x04,
         "GUI": 0x08, "WINDOWS": 0x08, "COMMAND": 0x08}
for i in range(1, 13):
    _NAMED[f"F{i}"] = (0, 0x3a + i - 1)


def _report(mod: int, key: int) -> bytes:
    return bytes([mod, 0, key, 0, 0, 0, 0, 0])


def _press(dev, mod: int, key: int) -> None:
    dev.write(_report(mod, key))
    dev.flush()
    dev.write(_report(0, 0))
    dev.flush()
    time.sleep(0.005)


def _char(dev, ch: str) -> None:
    if ch in _SHIFTED:
        _press(dev, _SHIFT, _BASE[_SHIFTED[ch]])
    elif ch.isupper():
        _press(dev, _SHIFT, _BASE[ch.lower()])
    elif ch in _BASE:
        _press(dev, 0, _BASE[ch])


def type_text(path: str, text: str) -> None:
    with open(path, "wb", buffering=0) as dev:
        for ch in text:
            _char(dev, ch)


def run_ducky(path: str, script: str) -> None:
    with open(path, "wb", buffering=0) as dev:
        default_delay = 0
        for raw in script.splitlines():
            line = raw.strip()
            if not line or line.startswith("REM"):
                continue
            parts = line.split(" ", 1)
            cmd = parts[0].upper()
            arg = parts[1] if len(parts) > 1 else ""
            if cmd == "STRING":
                for ch in arg:
                    _char(dev, ch)
            elif cmd in ("DELAY", "DEFAULTDELAY", "DEFAULT_DELAY"):
                ms = int(arg) if arg.isdigit() else default_delay
                if cmd != "DELAY":
                    default_delay = ms
                else:
                    time.sleep(ms / 1000.0)
            elif cmd in _NAMED:
                _press(dev, *_NAMED[cmd])
            elif cmd in _MODS:
                mod = _MODS[cmd]
                key = 0
                if arg:
                    k = arg.strip().upper()
                    if k in _NAMED:
                        key = _NAMED[k][1]
                    elif len(k) == 1 and k.lower() in _BASE:
                        key = _BASE[k.lower()]
                _press(dev, mod, key)
            if default_delay:
                time.sleep(default_delay / 1000.0)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path, mode = sys.argv[1], sys.argv[2]
    if mode == "text":
        type_text(path, sys.argv[3] if len(sys.argv) > 3 else "")
    elif mode == "ducky":
        run_ducky(path, sys.stdin.read())
    else:
        print(f"unknown mode: {mode}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
