"""Base class for install/flash strategies.

A strategy translates a device's declarative ``install.steps`` into concrete
operations against a connected device, honoring dry-run and the tool-detection
policy. Strategies are intentionally small and share the step interpreter in
:class:`Strategy`; device differences live in the device definition, not in
strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core import tools, ui
from ...core.errors import StrategyError
from ...core.model import Device
from ..artifacts import ArtifactSet


@dataclass
class PlannedOp:
    """A single concrete operation in a flash plan."""
    description: str
    command: list[str] | None      # None for host-side/logical ops
    destructive: bool = False
    tool: str | None = None
    partition: str | None = None
    remote: bool = False           # runs inside the rescue/on-device env


class Strategy:
    """Interprets a device's install steps into a list of PlannedOps."""

    #: strategy id, matched against device install.strategy
    name = "base"
    #: tools this strategy may need (for missing-tool reporting)
    tools: tuple[str, ...] = ()

    def __init__(self, device: Device, artifacts: ArtifactSet, artifact_dir: Path):
        self.device = device
        self.artifacts = artifacts
        self.artifact_dir = artifact_dir

    # -- helpers ------------------------------------------------------------
    def _image_path(self, key: str) -> str:
        art = self.artifacts.get(key)
        if art:
            return str(art.resolve(self.artifact_dir))
        # Fall back to a literal filename referenced in the step.
        return key

    def modified_partitions(self) -> list[str]:
        """Partitions this install will write (for the confirmation prompt)."""
        parts: list[str] = []
        for step in self.device.install.get("steps", []):
            if step.get("action") in ("flash-partition", "dd-partition"):
                p = step.get("partition")
                if p and p not in parts:
                    parts.append(p)
        return parts

    # -- planning -----------------------------------------------------------
    def plan(self) -> list[PlannedOp]:
        ops: list[PlannedOp] = []
        for step in self.device.install.get("steps", []):
            ops.extend(self._plan_step(step))
        return ops

    def _plan_step(self, step: dict) -> list[PlannedOp]:
        action = step.get("action")
        handler = getattr(self, f"_op_{action.replace('-', '_')}", None)
        if handler is None:
            raise StrategyError(f"strategy '{self.name}' cannot handle step action '{action}'")
        return handler(step)

    # -- step handlers (overridable) ---------------------------------------
    def _op_message(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description=step.get("description", ""), command=None)]

    def _op_flash_partition(self, step: dict) -> list[PlannedOp]:
        part = step["partition"]
        img = self._image_path(step.get("image", ""))
        via = step.get("via", "fastboot")
        if via == "fastboot":
            cmd = ["fastboot", "flash", part, img]
            tool = "fastboot"
        elif via == "fastbootd":
            cmd = ["fastboot", "flash", part, img]
            tool = "fastboot"
        elif via == "heimdall":
            cmd = ["heimdall", "flash", "--" + part, img]
            tool = "heimdall"
        else:
            raise StrategyError(f"flash-partition via '{via}' unsupported")
        return [PlannedOp(
            description=step.get("description", f"flash {part}"),
            command=cmd, destructive=True, tool=tool, partition=part,
        )]

    def _op_dd_partition(self, step: dict) -> list[PlannedOp]:
        # Overridden by rescue-dd / adb-shell-dd which know the transport.
        raise StrategyError(f"strategy '{self.name}' does not implement dd-partition")

    def _op_set_active_slot(self, step: dict) -> list[PlannedOp]:
        slot = step.get("slot", "a")
        return [PlannedOp(
            description=f"set active slot {slot}",
            command=["fastboot", "--set-active=" + slot], tool="fastboot",
        )]

    def _op_reboot(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="reboot", command=["fastboot", "reboot"], tool="fastboot")]

    def _op_reboot_bootloader(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="reboot to bootloader",
                          command=["fastboot", "reboot", "bootloader"], tool="fastboot")]

    def _op_reboot_fastbootd(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="reboot to fastbootd",
                          command=["fastboot", "reboot", "fastboot"], tool="fastboot")]

    def _op_enter_rescue(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description="enter rescue environment", command=None)]

    def _op_wait_transport(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description=step.get("description", "wait for transport"), command=None)]

    def _op_run_remote(self, step: dict) -> list[PlannedOp]:
        return [PlannedOp(description=step.get("description", "run remote command"),
                          command=None, remote=True)]

    # -- execution ----------------------------------------------------------
    def execute(self, runner: tools.Runner) -> None:
        for op in self.plan():
            if op.command is None:
                if op.description:
                    ui.note(f"  \u2022 {op.description}")
                continue
            ui.info(f"  \u2022 {op.description}")
            runner.run(op.command, tool=op.tool, check=True)
