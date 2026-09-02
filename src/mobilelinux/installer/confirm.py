"""Confirmation prompts for destructive operations."""

from __future__ import annotations

import sys

from ..core import ui
from ..core.errors import SafetyAbort


def confirm_destructive(
    device_pretty: str,
    strategy: str,
    partitions: list[str],
    *,
    assume_yes: bool,
    dry_run: bool,
) -> bool:
    """Show a WARNING block and require explicit confirmation.

    Returns True to proceed, False to abort. In dry-run, never proceeds with
    real writes but returns True so the plan can be printed.
    """
    print()
    print(ui.red(ui.bold("WARNING")))
    print()
    print(f"Device detected:")
    print(f"  {ui.bold(device_pretty)}")
    print()
    print("This operation will modify:")
    for p in partitions:
        print(f"  {ui.yellow(p)}")
    print()
    print(f"Installation strategy:")
    print(f"  {strategy}")
    print()
    print(ui.grey(
        "Disclaimer: you run this AT YOUR OWN RISK. The authors and contributors\n"
        "accept NO liability for damaged, bricked or lost devices/data. This tool\n"
        "does not modify the bootloader itself, so recovery is normally possible\n"
        "via the device's rescue/fastboot flow \u2014 but no outcome is guaranteed.\n"
        "See DISCLAIMER.md."
    ))
    print()

    if dry_run:
        ui.note("[dry-run] no partitions will be written")
        return True
    if assume_yes:
        ui.warn("proceeding without prompt (--yes)")
        return True
    if not sys.stdin.isatty():
        raise SafetyAbort("refusing to run a destructive operation without a TTY; pass --yes to override")

    try:
        ans = input("Continue? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")
