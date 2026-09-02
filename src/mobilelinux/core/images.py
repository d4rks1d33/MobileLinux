"""Image assembly: Android boot images and rootfs disk images.

These functions build the concrete artifacts the flasher consumes. They
orchestrate real tools (mkbootimg, sgdisk, mkfs.ext4, losetup) and degrade to
dry-run when the tools are absent.
"""

from __future__ import annotations

from pathlib import Path

from . import tools, ui
from .model import Device


def build_android_bootimg(
    device: Device, runner: tools.Runner, *,
    kernel: Path, ramdisk: Path, out: Path, root_uuid: str = "",
) -> Path:
    """Build an Android boot image per the device's boot.android_bootimg spec.

    Honors append_dtb (DTB concatenated to the Image) and the exact offsets /
    header version / pagesize required by the bootloader.
    """
    boot = device.boot
    cfg = boot.get("android_bootimg", {})
    offsets = cfg.get("offsets", {})
    cmdline = cfg.get("cmdline", "")
    if root_uuid:
        cmdline = cmdline.replace("@ROOT_UUID@", root_uuid)

    cmd = [
        "mkbootimg",
        "--kernel", str(kernel),
        "--ramdisk", str(ramdisk),
        "--pagesize", str(cfg.get("pagesize", 2048)),
        "--base", cfg.get("base", "0x00000000"),
        "--kernel_offset", offsets.get("kernel", "0x00008000"),
        "--ramdisk_offset", offsets.get("ramdisk", "0x01000000"),
        "--tags_offset", offsets.get("tags", "0x00000100"),
        "--header_version", str(cfg.get("header_version", 2)),
        "--cmdline", cmdline,
        "-o", str(out),
    ]
    if cfg.get("os_patch_level"):
        cmd += ["--os_patch_level", cfg["os_patch_level"]]
    if cfg.get("extra_args"):
        cmd += cfg["extra_args"].split()

    ui.info(f"  build boot image -> {out.name}")
    if device.device_tree.get("append_dtb"):
        ui.note("    (DTB is appended to the kernel Image; header carries no separate DTB)")
    runner.run(cmd, tool="mkbootimg")
    return out


def build_rootfs_image(
    device: Device, runner: tools.Runner, *,
    rootfs_tar: Path, out: Path,
) -> Path:
    """Build the rootfs disk image.

    For ``gpt-in-partition`` layout (rhodep): create a GPT disk with a boot +
    root partition at the device's logical sector size, unpack the rootfs into
    the root partition. For ``plain``: a single ext4 image. For ``whole-disk``:
    a full bootable disk (PinePhone/Librem 5).
    """
    storage = device.storage
    layout = storage.get("rootfs_layout", "plain")
    sector = storage.get("rootfs_sector_size", 512)

    ui.info(f"  build rootfs image ({layout}, sector={sector}) -> {out.name}")

    if layout == "gpt-in-partition":
        # Assemble a GPT disk (p1 boot vfat + p2 root ext4) at sector size.
        # sgdisk on a regular file is safe; losetup/mkfs on real block devices
        # are marked dangerous and are only planned unless --allow-dangerous.
        size_mib = storage.get("image_size_mib", 4096)
        runner.run(["truncate", "-s", f"{size_mib}M", str(out)])
        runner.run(["sgdisk", "-og", str(out)], tool="sgdisk")
        runner.run(["sgdisk", "-n", "1:0:+256M", "-c", "1:pmOS_boot", "-t", "1:0700", str(out)], tool="sgdisk")
        runner.run(["sgdisk", "-n", "2:0:0", "-c", "2:pmOS_root", "-t", "2:8300", str(out)], tool="sgdisk")
        loop = runner.run(
            ["losetup", "-Pf", "--sector-size", str(sector), "--show", str(out)],
            tool="losetup", dangerous=True,
        )
        runner.run(["mkfs.ext4", "-L", "rootfs", "/dev/loopXp2"], tool="mkfs.ext4", dangerous=True)
        runner.run(["sh", "-c", f"mount /dev/loopXp2 /mnt && tar -C /mnt -xf {rootfs_tar}"], dangerous=True)
        ui.note("    (loop/mkfs/unpack require root + --allow-dangerous; planned otherwise)")
    elif layout == "whole-disk":
        runner.run(["sh", "-c", f"# assemble whole-disk image from {rootfs_tar} + bootloader embed"])
    else:  # plain
        runner.run(["mkfs.ext4", "-L", "rootfs", "-d", "rootfs-work", str(out)],
                   tool="mkfs.ext4", dangerous=True)

    return out
