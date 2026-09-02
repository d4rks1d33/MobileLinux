"""Generate device-specific installation instructions from the definition.

The instructions are derived from ``install.strategy`` + ``install.steps`` so
they always match what `flash` will actually do — no hand-written per-device
prose that can drift.
"""

from __future__ import annotations

from ..core.model import Device

_STRATEGY_INTRO = {
    "rescue-dd": (
        "This device's bootloader refuses to flash the rootfs partition "
        "directly (and has no fastbootd). Installation therefore boots a "
        "**rescue image** that exposes a root shell over USB networking, and "
        "streams the rootfs disk into the target partition with `dd`."
    ),
    "fastboot": "This device is installed entirely over fastboot.",
    "fastbootd": (
        "This device stores the rootfs in a logical (super/dynamic) partition, "
        "so part of the install runs in **fastbootd** (userspace fastboot)."
    ),
    "heimdall": (
        "This device uses Samsung download mode; flashing is done with the "
        "open-source **heimdall** tool."
    ),
    "sdcard": (
        "This device boots from removable media; the whole-disk image is "
        "written to an SD card (or eMMC via a boot-from-SD helper)."
    ),
    "uuu": (
        "This device is flashed over the SoC serial-download protocol using "
        "NXP's **uuu**."
    ),
    "adb-shell-dd": "The rootfs is written via `adb shell dd` from a booted environment.",
}


def generate_instructions(device: Device, distro: str) -> str:
    strat = device.install_strategy
    lines: list[str] = []
    lines.append(f"# Installing {distro} on {device.model} ({device.codename})")
    lines.append("")
    lines.append(f"- SoC: {device.soc.get('marketing_name', device.soc_family)}")
    lines.append(f"- Architecture: {device.architecture}")
    lines.append(f"- Install strategy: `{strat}`")
    ab = device.install.get("ab_slots")
    lines.append(f"- A/B slots: {'yes' if ab else 'no'}")
    lines.append("")
    lines.append(_STRATEGY_INTRO.get(strat, "Custom installation strategy."))
    lines.append("")

    if device.install.get("unlock_required", True):
        lines.append("> **Bootloader must be unlocked first.** This erases the device.")
        lines.append("")

    rescue = device.install.get("rescue", {})
    if rescue.get("required"):
        lines.append("## Rescue environment")
        lines.append("")
        lines.append(f"- method: `{rescue.get('method')}`")
        lines.append(f"- reach it at: `{rescue.get('transport','')}`")
        if rescue.get("notes"):
            lines.append("")
            lines.append(rescue["notes"].strip())
        lines.append("")

    lines.append("## Steps")
    lines.append("")
    lines.append("`mobilelinux flash " + device.id + "` performs these automatically:")
    lines.append("")
    for i, step in enumerate(device.install.get("steps", []), 1):
        desc = step.get("description") or step.get("action")
        mark = "  ⚠️ destructive" if step.get("destructive") else ""
        lines.append(f"{i}. {desc}{mark}")
    lines.append("")
    lines.append("## Safety")
    lines.append("")
    lines.append("`flash` detects the connected device, confirms it matches this "
                 "definition, shows exactly which partitions it will modify, "
                 "verifies image hashes, and asks for confirmation. Use "
                 "`--dry-run` to preview without writing anything.")
    lines.append("")
    return "\n".join(lines)
