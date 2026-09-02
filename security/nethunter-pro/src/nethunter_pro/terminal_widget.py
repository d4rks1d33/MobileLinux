"""Launch a fully-built command in an external interactive terminal.

An embedded VTE is not an option: the only VTE packaged is the GTK3 build
(Vte 2.91) and this is a GTK4 app, so they cannot share a process. Instead,
interactive tools (SET, wifipumpkin3, msfconsole, reaver, cansniffer, ...) are
built up from options in the GUI, exactly like the Android app did, and then
launched in the system terminal with those parameters already set, so the user
only has to answer the tool's own prompts.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess

# Phosh's Console (kgx) first, then common alternatives.
_TERMINALS = [
    ("kgx", ["kgx", "-e"]),
    ("ptyxis", ["ptyxis", "--"]),
    ("gnome-terminal", ["gnome-terminal", "--"]),
    ("foot", ["foot"]),
    ("konsole", ["konsole", "-e"]),
    ("xterm", ["xterm", "-e"]),
]


def available() -> bool:
    return any(shutil.which(name) for name, _ in _TERMINALS)


def launch(command: str | list[str], *, root: bool = False, hold: bool = True) -> bool:
    """Open a command in a system terminal.

    command may be a string or argv. With root=True it is wrapped in pkexec.
    With hold=True the shell stays open after the command exits (so output and
    any final prompt remain visible) until the user presses Enter.
    """
    if isinstance(command, list):
        command = " ".join(shlex.quote(c) for c in command)
    if root:
        command = f"pkexec {command}"
    if hold:
        # Keep the terminal open after the tool finishes.
        command = f"{command}; echo; echo '[finished - press Enter to close]'; read _"

    inner = ["sh", "-c", command]
    for name, prefix in _TERMINALS:
        if shutil.which(name):
            try:
                subprocess.Popen([*prefix, *inner])
                return True
            except Exception:
                continue
    return False
