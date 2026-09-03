"""Distribution backend interface.

A distro backend knows how to produce a root filesystem for a given device +
desktop + optional profile. It is deliberately decoupled from hardware: it
receives the Device (for arch/firmware/device-package hints) but must not embed
device-specific logic beyond what the definition declares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core import tools
from ..core.model import Device


@dataclass
class BuildRequest:
    device: Device
    desktop: str
    profile: str | None
    out_dir: Path             # out/<device>/
    repo_root: Path
    runner: tools.Runner


@dataclass
class RootfsResult:
    """Result of a rootfs build stage."""
    tarball: Path | None = None    # rootfs tar (before imaging)
    rootfs_dir: Path | None = None  # extracted + integrated rootfs (ready to image)
    notes: list[str] = field(default_factory=list)


class DistroBackend:
    #: distro id, e.g. "kali"
    name = "base"
    #: tools the backend needs
    tools: tuple[str, ...] = ()

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    @property
    def dir(self) -> Path:
        return self.repo_root / "os-distros" / self.name

    def build_rootfs(self, req: BuildRequest) -> RootfsResult:  # pragma: no cover - interface
        raise NotImplementedError

    def default_desktop(self) -> str:
        return "phosh"
