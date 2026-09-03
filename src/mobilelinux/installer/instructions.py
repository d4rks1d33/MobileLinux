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

    # Boot image / initramfs note for gpt-in-partition devices.
    initcfg = device.boot.get("initramfs", {})
    if (initcfg.get("type") == "postmarketos"
            or "loop-gpt-4096" in initcfg.get("features", [])):
        lines.append("## Boot image note")
        lines.append("")
        lines.append("This device stores the rootfs as a **GPT disk inside the "
                     f"`{device.storage.get('rootfs_layout')}`** (logical sector "
                     f"{device.storage.get('rootfs_sector_size', 512)}). Mounting it "
                     "requires the **postmarketOS initramfs** (which does "
                     "`losetup -Pf --sector-size 4096` on the target partition and "
                     "mounts root by UUID). The distro's own initramfs "
                     "(initramfs-tools) does NOT do this and will drop to a busybox "
                     "emergency shell. The build therefore embeds the pmOS initramfs "
                     "from the `--input` base boot image — so pass a known-good pmOS "
                     "`boot.img` to `mobilelinux build ... --input <pmos-boot.img>`.")
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
    # First boot + black-screen troubleshooting (for gpt-in-partition + pmOS initramfs).
    initcfg = device.boot.get("initramfs", {})
    if (initcfg.get("type") == "postmarketos"
            or "loop-gpt-4096" in initcfg.get("features", [])):
        lines.append("## First boot")
        lines.append("")
        lines.append("- The device **vibrates before switch_root** — that means the "
                     "initramfs found and mounted the rootfs by UUID. Good sign.")
        lines.append("- On the **first boot** the initramfs runs `resize2fs` to grow "
                     "the rootfs to fill the partition, then systemd + the desktop "
                     "start. **A black screen / stuck logo for several minutes on the "
                     "first boot is NORMAL.** Wait.")
        lines.append("- You can confirm it's alive over USB: `ssh kali@172.16.42.1` "
                     "(password `1234`).")
        lines.append("")
        lines.append("## Black screen after the logo — troubleshooting")
        lines.append("")
        lines.append("If it stays black long after the first boot (and SSH works), it "
                     "is a userspace/kernel issue, not the boot image:")
        lines.append("")
        lines.append("1. **`droid-juicer` not masked** hangs `graphical.target` "
                     "(it waits forever for Android firmware). The build masks it, "
                     "but an `apt full-upgrade` can re-enable it. Fix:")
        lines.append("   ```")
        lines.append("   sudo systemctl mask droid-juicer.service systemd-repart.service")
        lines.append("   sudo reboot")
        lines.append("   ```")
        lines.append("2. **GDM vs phosh.service** both claiming the display (if "
                     "`kali-linux-default` pulled in gdm3). Ensure a single display "
                     "owner (the login layer masks `phosh.service` and points "
                     "`display-manager` at gdm3).")
        lines.append("3. **Wrong cmdline**: the boot image must use the pmOS "
                     "initramfs and a cmdline WITHOUT `rootwait` (rootwait hangs "
                     "because the pmOS initramfs resolves root by `pmos_root_uuid`, "
                     "not `root=`). This build already does the right thing.")
        lines.append("4. **UUID mismatch**: the `pmos_root_uuid` in the boot image "
                     "must equal the ext4 UUID inside userdata. Verify from a rescue "
                     "shell: `losetup -Pf --sector-size 4096 "
                     "/dev/disk/by-partlabel/userdata; blkid /dev/loop0p2`.")
        lines.append("")

    lines.append("## Safety")
    lines.append("")
    lines.append("`flash` detects the connected device, confirms it matches this "
                 "definition, shows exactly which partitions it will modify, "
                 "verifies image hashes, and asks for confirmation. Use "
                 "`--dry-run` to preview without writing anything.")
    lines.append("")
    return "\n".join(lines)


def generate_firmware_instructions(device) -> str:
    """Generate instructions to obtain non-redistributable vendor firmware.

    These blobs cannot be shipped in git; they are extracted from the device's
    own stock/vendor partitions. The instructions are derived from the device
    definition's firmware.extract_from_device + runtime_mounts.
    """
    fw = device.firmware
    lines: list[str] = []
    lines.append(f"# Vendor firmware for {device.model} ({device.codename})")
    lines.append("")
    lines.append("Some firmware for this device is **proprietary and NOT "
                 "redistributable**, so it is not shipped in this repository. You "
                 "must extract it from your own device (from its stock vendor "
                 "partitions) and place it in the firmware package before "
                 "building, OR install it on the running phone.")
    lines.append("")
    pkg = fw.get("package")
    if pkg:
        lines.append(f"Target firmware package: `{pkg}` "
                     f"(files land under `/usr/lib/firmware/...`).")
        lines.append("")

    blobs = fw.get("extract_from_device", [])
    if blobs:
        lines.append("## Blobs to extract")
        lines.append("")
        lines.append("| Firmware file | For | Source |")
        lines.append("|---------------|-----|--------|")
        for b in blobs:
            lines.append(f"| `{b.get('name','')}` | {b.get('provides','')} | "
                         f"{b.get('source','stock vendor')} |")
        lines.append("")

    mounts = fw.get("runtime_mounts", [])
    if mounts:
        lines.append("## Runtime firmware mounts")
        lines.append("")
        lines.append("Large modem/DSP blobs are served at runtime directly from a "
                     "device partition (not copied into the rootfs):")
        lines.append("")
        for m in mounts:
            lines.append(f"- `{m.get('source')}` -> `{m.get('target')}` "
                         f"(`{m.get('fstype','ext4')}`, `{m.get('options','ro')}`)")
        lines.append("")

    lines.append("## How to extract them")
    lines.append("")
    lines.append("From the stock ROM or a running Android/pmOS on the device, the "
                 "blobs live under the vendor/system partitions (often inside the "
                 "Android dynamic `super` partition) and the modem partition. Two "
                 "common routes:")
    lines.append("")
    lines.append("1. **From the running phone** (rescue shell or a booted Linux): "
                 "mount the vendor/modem partitions read-only and copy the files "
                 "listed above into the firmware package's `lib/firmware/` tree, "
                 "preserving the paths in the table.")
    lines.append("2. **From the stock firmware image**: unpack the vendor/super "
                 "image (e.g. with `lpunpack` + `simg2img`) and copy the same "
                 "files out.")
    lines.append("")
    lines.append("Then rebuild the firmware package and re-run the build, or "
                 "`dpkg -i` the firmware package on the device. Until the blobs are "
                 "present, the corresponding hardware (WiFi/BT/GPU-zap/audio) will "
                 "not initialize even though all the software is in place.")
    lines.append("")
    lines.append("> These instructions are generated from the device definition's "
                 "`firmware.extract_from_device`; keep that list accurate.")
    lines.append("")
    return "\n".join(lines)
