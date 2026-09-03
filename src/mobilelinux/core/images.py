"""Image assembly: rootfs disk images and Android boot images.

These functions build the concrete artifacts the flasher consumes, using the
real, validated procedure from the reference port:

* ``gpt-in-partition`` (rhodep): the rootfs is an ext4 filesystem wrapped in a
  GPT disk at logical sector size 4096, with p1 pmOS_boot (vfat) + p2 pmOS_root
  (ext4). The initramfs does ``losetup -Pf --sector-size 4096`` and mounts p2 by
  UUID. We assemble it by building the ext4 with ``mkfs.ext4 -d`` and writing it
  into the disk at the p2 offset (dd seek), which avoids relying on loop-device
  partition scanning inside a container.
* the Android boot image is built with the device's boot script (mkbootv2b.py:
  flat Image + appended DTB, header v2), embedding the real kernel/DTB/initrd and
  a cmdline carrying the rootfs UUID.

Everything is gated by the Runner: real only with ``--execute`` /
``--allow-dangerous``; otherwise planned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import tools, ui
from .model import Device


def _live(runner: tools.Runner) -> bool:
    """True when we should actually run (not dry-run, execution enabled)."""
    return not runner.dry_run and (runner.execute or runner.allow_dangerous)


def _sh(cmd: str, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "sh", "-c", cmd], check=check,
                          capture_output=True, text=True)


# --------------------------------------------------------------------------
# rootfs disk image
# --------------------------------------------------------------------------
def build_rootfs_image(
    device: Device, runner: tools.Runner, *,
    rootfs_dir: Path, out: Path, work: Path | None = None,
) -> tuple[Path, str]:
    """Build the rootfs disk image from an extracted rootfs directory.

    Returns (image_path, root_uuid). root_uuid is "" in plan mode. ``work`` is a
    case-sensitive scratch dir for the intermediate ext4 (defaults to out.parent).
    """
    storage = device.storage
    layout = storage.get("rootfs_layout", "plain")
    sector = storage.get("rootfs_sector_size", 512)
    work = work or out.parent

    ui.info(f"  build rootfs image ({layout}, sector={sector}) -> {out.name}")

    if not _live(runner):
        ui.info(f"  [plan] mkfs.ext4 -d {rootfs_dir} -> ext4; wrap in GPT "
                f"(sector {sector}, p1 vfat 256M + p2 ext4) -> {out.name}")
        return out, ""

    if layout == "gpt-in-partition":
        return _build_gpt_in_partition(device, rootfs_dir, out, sector, work)
    elif layout == "whole-disk":
        ui.warn("  whole-disk layout not implemented for real build yet; planning")
        return out, ""
    else:  # plain
        return _build_plain_ext4(rootfs_dir, out)


def _dir_size_mib(path: Path) -> int:
    cp = subprocess.run(["sudo", "du", "-sm", str(path)], capture_output=True, text=True)
    try:
        return int(cp.stdout.split()[0])
    except Exception:
        return 4096


def _ext4_uuid(ext4: Path) -> str:
    cp = subprocess.run(["sudo", "dumpe2fs", "-h", str(ext4)],
                        capture_output=True, text=True)
    for line in cp.stdout.splitlines():
        if "Filesystem UUID" in line:
            return line.split(":", 1)[1].strip()
    return ""


def _build_gpt_in_partition(device: Device, rootfs_dir: Path, out: Path,
                            sector: int, work: Path) -> tuple[Path, str]:
    ext4 = work / "root.ext4"
    # size: used + ~40% headroom (first boot resize2fs grows it to the partition)
    used = _dir_size_mib(rootfs_dir)
    root_mib = max(used + used // 2, used + 1024)
    ui.note(f"    ext4: {used}MiB used -> {root_mib}MiB image")
    _sh(f"rm -f {ext4}; truncate -s {root_mib}M {ext4}")
    _sh(f"mkfs.ext4 -q -F -L rootfs -m 0 -d {rootfs_dir} -b 4096 {ext4}")
    root_uuid = _ext4_uuid(ext4)
    ui.note(f"    ext4 UUID: {root_uuid}")

    # GPT disk, logical sector 4096: p1 pmOS_boot 256MiB @ sector 256,
    # p2 pmOS_root @ sector 65792. Offsets computed by hand (robust in a
    # container where loop partition scanning is unreliable).
    p2_start = 65792
    p2_offset = p2_start * sector
    ext_bytes = int(subprocess.run(["stat", "-c", "%s", str(ext4)],
                                   capture_output=True, text=True).stdout.strip())
    total = p2_offset + ext_bytes + 1024 * 1024  # + backup GPT slack
    _sh(f"rm -f {out}; truncate -s {total} {out}")
    loop = subprocess.run(["sudo", "losetup", "-f", "--show", "-b", str(sector), str(out)],
                          capture_output=True, text=True).stdout.strip()
    _sh(f"sgdisk -og {loop}")
    _sh(f"sgdisk -a 1 -n 1:256:{p2_start-1} -c 1:pmOS_boot -t 1:0700 {loop}")
    _sh(f"sgdisk -a 1 -n 2:{p2_start}:0    -c 2:pmOS_root -t 2:8300 {loop}")
    subprocess.run(["sudo", "losetup", "-d", loop], check=False)

    # write the ext4 into p2 by offset, and a vfat into p1 by offset
    _sh(f"dd if={ext4} of={out} bs={sector} seek={p2_start} conv=fsync,notrunc status=none")
    p1_size_sectors = p2_start - 256
    p1 = subprocess.run(["sudo", "losetup", "-f", "--show",
                         "-o", str(256 * sector),
                         "--sizelimit", str(p1_size_sectors * sector), str(out)],
                        capture_output=True, text=True).stdout.strip()
    subprocess.run(["sudo", "mkfs.vfat", "-n", "pmOS_boot", p1],
                   capture_output=True)
    subprocess.run(["sudo", "losetup", "-d", p1], check=False)

    ui.success(f"    rootfs disk: {out.name} (GPT sector-{sector}, root UUID {root_uuid[:8]}…)")
    # scratch ext4 kept in work/, cleaned by finalize
    return out, root_uuid


def _build_plain_ext4(rootfs_dir: Path, out: Path) -> tuple[Path, str]:
    used = _dir_size_mib(rootfs_dir)
    size = max(used + used // 2, used + 512)
    _sh(f"rm -f {out}; truncate -s {size}M {out}")
    _sh(f"mkfs.ext4 -q -F -L rootfs -d {rootfs_dir} {out}")
    return out, _ext4_uuid(out)


# --------------------------------------------------------------------------
# Android boot image
# --------------------------------------------------------------------------
def build_android_bootimg(
    device: Device, runner: tools.Runner, *,
    kernel: Path, dtb: Path, ramdisk: Path, out: Path, root_uuid: str = "",
) -> Path:
    """Build an Android boot image using the device's boot script (mkbootv2b.py:
    flat Image + appended DTB, header v2), with a cmdline carrying the root UUID.
    """
    cfg = device.boot.get("android_bootimg", {})
    cmdline = cfg.get("cmdline", "")
    if root_uuid:
        cmdline = cmdline.replace("@ROOT_UUID@", root_uuid)
    script = device.dir / "assets" / "scripts" / "mkbootv2b.py"

    ui.info(f"  build boot image -> {out.name}")
    if device.device_tree.get("append_dtb"):
        ui.note("    (DTB appended to the kernel Image; header carries no separate DTB)")

    if not _live(runner):
        ui.info(f"  [plan] python3 {script.name} vmlinuz dtb initrd "
                f"'{cmdline[:40]}…' {out.name}")
        return out

    if script.is_file():
        subprocess.run(["python3", str(script), str(kernel), str(dtb),
                        str(ramdisk), cmdline, str(out)], check=True)
    else:
        # Fallback to mkbootimg (no appended DTB).
        offsets = cfg.get("offsets", {})
        subprocess.run([
            "mkbootimg", "--kernel", str(kernel), "--ramdisk", str(ramdisk),
            "--dtb", str(dtb),
            "--pagesize", str(cfg.get("pagesize", 2048)),
            "--base", cfg.get("base", "0x00000000"),
            "--kernel_offset", offsets.get("kernel", "0x00008000"),
            "--ramdisk_offset", offsets.get("ramdisk", "0x01000000"),
            "--tags_offset", offsets.get("tags", "0x00000100"),
            "--header_version", str(cfg.get("header_version", 2)),
            "--cmdline", cmdline, "-o", str(out),
        ], check=True)
    ui.success(f"    boot image: {out.name}")
    return out
