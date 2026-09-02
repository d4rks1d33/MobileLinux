"""Distribution backend registry."""

from __future__ import annotations

from pathlib import Path

from ..core.errors import BuildError
from .base import DistroBackend
from .kali import KaliBackend
from .postmarketos import PostmarketosBackend

_BACKENDS: dict[str, type[DistroBackend]] = {
    KaliBackend.name: KaliBackend,
    PostmarketosBackend.name: PostmarketosBackend,
}


# Convenience aliases.
_ALIASES = {"pmos": "postmarketos"}


def get_backend(name: str, repo_root: Path) -> DistroBackend:
    name = _ALIASES.get(name, name)
    cls = _BACKENDS.get(name)
    if cls is None:
        raise BuildError(
            f"no distro backend '{name}'. Known: {', '.join(sorted(_BACKENDS))}"
        )
    return cls(repo_root)


def known_distros() -> list[str]:
    return sorted(_BACKENDS)
