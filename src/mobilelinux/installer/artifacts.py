"""Build artifacts produced by `build` and consumed by `flash`/`release`.

An artifact set is written to ``out/<device>/`` alongside a manifest
(``artifacts.json``) recording sizes and sha256 hashes. `flash` validates
these hashes before writing anything to a device.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Artifact:
    key: str          # logical name: 'boot', 'rootfs', 'rescue'
    filename: str     # file on disk (relative to the artifact dir)
    sha256: str = ""
    size: int = 0
    role: str = ""    # boot | rootfs | rescue | firmware

    def resolve(self, base: Path) -> Path:
        return base / self.filename


@dataclass
class ArtifactSet:
    device: str
    distro: str = ""
    desktop: str = ""
    version: str = ""
    artifacts: dict[str, Artifact] = field(default_factory=dict)

    @property
    def dir_name(self) -> str:
        return self.device

    def add(self, key: str, path: Path, role: str = "") -> Artifact:
        art = Artifact(
            key=key,
            filename=path.name,
            sha256=sha256_file(path) if path.exists() else "",
            size=path.stat().st_size if path.exists() else 0,
            role=role or key,
        )
        self.artifacts[key] = art
        return art

    def get(self, key: str) -> Artifact | None:
        return self.artifacts.get(key)

    def to_json(self) -> str:
        data = {
            "device": self.device,
            "distro": self.distro,
            "desktop": self.desktop,
            "version": self.version,
            "artifacts": {k: asdict(v) for k, v in self.artifacts.items()},
        }
        return json.dumps(data, indent=2)

    def save(self, base: Path) -> Path:
        base.mkdir(parents=True, exist_ok=True)
        out = base / "artifacts.json"
        out.write_text(self.to_json(), encoding="utf-8")
        return out

    @classmethod
    def load(cls, base: Path) -> "ArtifactSet":
        data = json.loads((base / "artifacts.json").read_text(encoding="utf-8"))
        s = cls(
            device=data["device"], distro=data.get("distro", ""),
            desktop=data.get("desktop", ""), version=data.get("version", ""),
        )
        for k, v in data.get("artifacts", {}).items():
            s.artifacts[k] = Artifact(**v)
        return s

    def verify(self, base: Path) -> list[str]:
        """Return a list of problems (empty = all hashes match)."""
        problems: list[str] = []
        for key, art in self.artifacts.items():
            path = art.resolve(base)
            if not path.exists():
                problems.append(f"{key}: missing file {art.filename}")
                continue
            if art.sha256:
                actual = sha256_file(path)
                if actual != art.sha256:
                    problems.append(
                        f"{key}: sha256 mismatch (expected {art.sha256[:12]}…, got {actual[:12]}…)"
                    )
        return problems
