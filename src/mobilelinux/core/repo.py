"""Repository discovery and path resolution.

The repo root is the directory containing ``mobilelinux.toml``. The CLI walks
up from the current directory (or an explicit --repo) to find it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import RepoNotFoundError

MARKER = "mobilelinux.toml"


@dataclass(frozen=True)
class Repo:
    root: Path

    @property
    def devices_dir(self) -> Path:
        return self.root / "devices"

    @property
    def distros_dir(self) -> Path:
        return self.root / "os-distros"

    @property
    def desktops_dir(self) -> Path:
        return self.root / "desktops"

    @property
    def security_dir(self) -> Path:
        return self.root / "security"

    @property
    def schema_dir(self) -> Path:
        return self.root / "schema"

    @property
    def schema_file(self) -> Path:
        return self.schema_dir / "device.schema.json"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def reference_repo(self) -> Path:
        # Read-only source of migration assets (nethunter-rhodep-repo).
        return (self.root / ".." / "nethunter-rhodep-repo").resolve()


def find_repo(start: str | os.PathLike | None = None) -> Repo:
    """Locate the repository root by walking upward from ``start``."""
    here = Path(start or os.getcwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / MARKER).is_file():
            return Repo(candidate)
    raise RepoNotFoundError(
        f"could not find {MARKER} in {here} or any parent directory. "
        "Run inside the mobilelinux repository or pass --repo."
    )
