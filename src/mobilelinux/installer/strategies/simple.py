"""Simple strategies that map almost directly onto a single flashing tool.

These reuse the base step interpreter. They exist as distinct classes so the
registry can select them by name and so their required-tool sets are explicit
for missing-tool reporting.
"""

from __future__ import annotations

from ...core.errors import StrategyError
from .base import PlannedOp, Strategy


class FastbootStrategy(Strategy):
    """Bootloader fastboot can write the rootfs partition directly (e.g. OnePlus 6)."""
    name = "fastboot"
    tools = ("fastboot",)

    def _op_dd_partition(self, step: dict) -> list[PlannedOp]:
        # On these devices the rootfs is written via fastboot flash, not dd.
        part = step["partition"]
        img = self._image_path(step.get("image", ""))
        return [PlannedOp(
            description=step.get("description", f"flash {part}"),
            command=["fastboot", "flash", part, img],
            destructive=True, tool="fastboot", partition=part,
        )]


class FastbootdStrategy(FastbootStrategy):
    """Requires rebooting into userspace fastboot (fastbootd) to write logical
    (super/dynamic) partitions (e.g. Pixel 3a)."""
    name = "fastbootd"


class HeimdallStrategy(Strategy):
    """Samsung download mode via Heimdall (e.g. Galaxy S III)."""
    name = "heimdall"
    tools = ("heimdall",)

    def _op_dd_partition(self, step: dict) -> list[PlannedOp]:
        part = step["partition"]
        img = self._image_path(step.get("image", ""))
        return [PlannedOp(
            description=step.get("description", f"heimdall flash {part}"),
            command=["heimdall", "flash", "--" + part, img],
            destructive=True, tool="heimdall", partition=part,
        )]

    def _op_set_active_slot(self, step: dict) -> list[PlannedOp]:
        # No A/B slots on these devices.
        return [PlannedOp(description="(no A/B slots)", command=None)]

    def _op_reboot(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="reboot", command=["heimdall", "close-pc-screen"], tool="heimdall")]


class SdcardStrategy(Strategy):
    """Whole-disk image written to SD/eMMC (e.g. PinePhone). Host-side dd to a
    block device the user must specify; extremely conservative."""
    name = "sdcard"
    tools = ()

    def _op_dd_partition(self, step: dict) -> list[PlannedOp]:
        img = self._image_path(step.get("image", ""))
        return [PlannedOp(
            description=step.get("description",
                                 f"write {step.get('image')} to the SD/eMMC target device "
                                 f"(set via --target; refusing to guess)"),
            command=None, destructive=True, partition=step.get("partition"),
        )]

    def _op_flash_partition(self, step: dict) -> list[PlannedOp]:
        # PinePhone has no fastboot; boot comes from the disk image itself.
        return [PlannedOp(description="(boot is part of the disk image)", command=None)]

    def _op_set_active_slot(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="(no A/B slots)", command=None)]

    def _op_reboot(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="power on the device from the new media", command=None)]


class UuuStrategy(Strategy):
    """NXP i.MX serial-download via uuu (e.g. Librem 5)."""
    name = "uuu"
    tools = ("uuu",)

    def _op_dd_partition(self, step: dict) -> list[PlannedOp]:
        img = self._image_path(step.get("image", ""))
        return [PlannedOp(
            description=step.get("description", f"uuu write {step.get('image')} to eMMC"),
            command=["uuu", img], destructive=True, tool="uuu", partition=step.get("partition"),
        )]

    def _op_flash_partition(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="(boot handled by uuu script / disk image)", command=None)]

    def _op_set_active_slot(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="(no A/B slots)", command=None)]


class AdbShellDdStrategy(Strategy):
    """Write a partition via `adb shell dd` from a booted Linux/recovery."""
    name = "adb-shell-dd"
    tools = ("adb",)

    def _op_dd_partition(self, step: dict) -> list[PlannedOp]:
        part = step["partition"]
        img = self._image_path(step.get("image", ""))
        target = f"/dev/disk/by-partlabel/{part}"
        return [PlannedOp(
            description=step.get("description", f"adb dd -> {target}"),
            command=["sh", "-c", f"adb shell 'su -c \"dd of={target} bs=4M conv=fsync\"' < {img}"],
            destructive=True, tool="adb", partition=part, remote=True,
        )]
