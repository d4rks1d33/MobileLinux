"""External-tool detection and command execution.

The framework orchestrates real build/flash tooling (fastboot, adb, debos,
pmbootstrap, mkbootimg, ...). Those tools are heavy and often absent on a
plain machine. Per project policy, MobileLinux:

  * detects whether each required tool is present,
  * runs the real tool when available,
  * otherwise prints exactly the command it WOULD run (dry-run),
  * and clearly tells the user which tools are missing and how to install them,
    so they can install them and re-run.

Nothing destructive ever runs without an explicit, present tool AND (for
destructive ops) an explicit confirmation handled by the caller.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

from . import ui
from .errors import ToolMissingError


@dataclass(frozen=True)
class Tool:
    """Description of an external tool the framework may invoke."""

    name: str            # executable name, e.g. "fastboot"
    purpose: str         # short human description
    install_hint: str    # how to install it (Debian/Alpine/pip/go, as appropriate)
    optional: bool = False


# Registry of tools the framework knows about. Install hints prefer the most
# common route on a Debian/Kali build host (the reference environment), with
# alternatives noted.
TOOLS: dict[str, Tool] = {
    "fastboot": Tool(
        "fastboot", "flash/boot Android bootloader partitions",
        "apt install android-sdk-platform-tools  (or: pmbootstrap has it bundled)"),
    "adb": Tool(
        "adb", "Android Debug Bridge (device detection, adb-shell-dd)",
        "apt install android-sdk-platform-tools"),
    "heimdall": Tool(
        "heimdall", "flash Samsung download-mode (Odin) devices",
        "apt install heimdall-flash"),
    "uuu": Tool(
        "uuu", "NXP i.MX serial-download flashing (Librem 5)",
        "download from https://github.com/nxp-imx/mfgtools/releases"),
    "rkdeveloptool": Tool(
        "rkdeveloptool", "Rockchip flashing",
        "apt install rkdeveloptool"),
    "mkbootimg": Tool(
        "mkbootimg", "build Android boot images",
        "apt install android-sdk-libsparse-utils mkbootimg  (or android-tools)"),
    "img2simg": Tool(
        "img2simg", "convert raw images to Android sparse format",
        "apt install android-sdk-libsparse-utils"),
    "debos": Tool(
        "debos", "build Debian/Kali rootfs from a recipe",
        "go install github.com/go-debos/debos/cmd/debos@latest  (needs Go)"),
    "pmbootstrap": Tool(
        "pmbootstrap", "build mainline kernels/rootfs the postmarketOS way",
        "pipx install pmbootstrap  (or: apt install pmbootstrap on some distros)"),
    "mkfs.ext4": Tool(
        "mkfs.ext4", "create ext4 filesystems for rootfs images",
        "apt install e2fsprogs"),
    "sgdisk": Tool(
        "sgdisk", "create GPT partition tables (gpt-in-partition layout)",
        "apt install gdisk"),
    "losetup": Tool(
        "losetup", "attach loop devices for image assembly",
        "apt install util-linux (usually preinstalled)"),
    "resize2fs": Tool(
        "resize2fs", "grow ext4 filesystems (first-boot resize)",
        "apt install e2fsprogs"),
    "rauc": Tool(
        "rauc", "build/verify/install atomic A/B update bundles",
        "apt install rauc", optional=True),
    "openssl": Tool(
        "openssl", "sign releases and verify signatures",
        "apt install openssl"),
    "syft": Tool(
        "syft", "generate SBOM (SPDX/CycloneDX) from a rootfs",
        "https://github.com/anchore/syft (curl installer)", optional=True),
    "debsecan": Tool(
        "debsecan", "map installed Debian/Kali packages to CVEs",
        "apt install debsecan", optional=True),
}


def find(name: str) -> str | None:
    """Return the resolved path of ``name`` or None if not on PATH."""
    return shutil.which(name)


def have(name: str) -> bool:
    return find(name) is not None


def require(name: str) -> str:
    """Return the path to a tool or raise ToolMissingError with an install hint."""
    path = find(name)
    if path:
        return path
    tool = TOOLS.get(name)
    hint = tool.install_hint if tool else None
    raise ToolMissingError(name, hint)


@dataclass
class MissingTools:
    """Accumulates required/optional tools that were not found during a plan."""

    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)

    def add(self, name: str) -> None:
        tool = TOOLS.get(name)
        if tool and tool.optional:
            if name not in self.optional:
                self.optional.append(name)
        else:
            if name not in self.required:
                self.required.append(name)

    def check(self, name: str) -> bool:
        """Record ``name`` if missing; return True if present."""
        if have(name):
            return True
        self.add(name)
        return False

    @property
    def any(self) -> bool:
        return bool(self.required or self.optional)

    def report(self) -> None:
        """Print a clear, actionable summary of missing tooling."""
        if not self.any:
            return
        ui.header("Missing tools")
        if self.required:
            ui.error("the following REQUIRED tools are not installed:")
            for name in self.required:
                t = TOOLS.get(name)
                hint = t.install_hint if t else "(no hint)"
                print(f"  {ui.red('\u2717')} {ui.bold(name)} \u2014 {t.purpose if t else ''}")
                print(f"      install: {hint}")
        if self.optional:
            ui.warn("optional tools not installed (features degrade gracefully):")
            for name in self.optional:
                t = TOOLS.get(name)
                hint = t.install_hint if t else "(no hint)"
                print(f"  {ui.yellow('\u26a0')} {ui.bold(name)} \u2014 {t.purpose if t else ''}")
                print(f"      install: {hint}")
        print()
        ui.note("Install the tools above and re-run the same command.")


class Runner:
    """Executes commands, honoring dry-run and recording missing tools.

    A single Runner instance is threaded through build/flash so that a whole
    operation can be planned (dry-run) and its missing tools summarized at the
    end.
    """

    def __init__(self, dry_run: bool = False, verbose: bool = True):
        self.dry_run = dry_run
        self.verbose = verbose
        self.missing = MissingTools()

    def _fmt(self, cmd: list[str]) -> str:
        return " ".join(_quote(c) for c in cmd)

    def run(
        self,
        cmd: list[str],
        *,
        tool: str | None = None,
        check: bool = True,
        capture: bool = False,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess | None:
        """Run ``cmd``.

        ``tool`` is the executable whose presence gates real execution; if it
        is missing, the command is treated as dry-run and recorded. If not
        given, ``cmd[0]`` is used.
        """
        gate = tool or (cmd[0] if cmd else "")
        present = have(gate)
        pretty = self._fmt(cmd)

        if self.dry_run or not present:
            prefix = ui.dim("[dry-run]") if self.dry_run else ui.yellow("[skip: tool missing]")
            if self.verbose:
                print(f"{prefix} {pretty}")
            if not present:
                self.missing.check(gate)
            return None

        if self.verbose:
            print(f"{ui.cyan('$')} {pretty}")
        return subprocess.run(
            cmd,
            check=check,
            input=input_bytes,
            capture_output=capture,
            cwd=cwd,
        )

    def capture(self, cmd: list[str], *, tool: str | None = None) -> str | None:
        """Run and return stdout as text, or None in dry-run / missing-tool."""
        cp = self.run(cmd, tool=tool, check=False, capture=True)
        if cp is None:
            return None
        return cp.stdout.decode("utf-8", "replace")


def _quote(s: str) -> str:
    if not s or any(ch in s for ch in " \t\"'\\$"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s
