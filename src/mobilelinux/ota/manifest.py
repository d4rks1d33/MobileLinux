"""OTA manifest model + canonical serialization.

The manifest is signed over a *canonical* JSON encoding of everything except
the ``signature`` field, so signing and verification agree byte-for-byte
regardless of key ordering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def canonical_body(manifest: dict[str, Any]) -> bytes:
    """Return the canonical bytes that are signed (manifest minus signature)."""
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class Manifest:
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.data.get("release", {}).get("version", "")

    @property
    def channel(self) -> str:
        return self.data.get("release", {}).get("channel", "")

    @property
    def device_id(self) -> str:
        return self.data.get("device", {}).get("id", "")

    @property
    def architecture(self) -> str:
        return self.data.get("device", {}).get("architecture", "")

    @property
    def minimum_version(self) -> str:
        return self.data.get("device", {}).get("minimum_version", "")

    @property
    def security_patch_level(self) -> str:
        return self.data.get("security", {}).get("security_patch_level", "")

    @property
    def kernel_version(self) -> str:
        return self.data.get("security", {}).get("kernel_version", "")

    @property
    def artifacts(self) -> dict[str, Any]:
        return self.data.get("artifacts", {})

    @property
    def signature(self) -> dict[str, Any]:
        return self.data.get("signature", {})

    def canonical(self) -> bytes:
        return canonical_body(self.data)

    def to_json(self) -> str:
        return json.dumps(self.data, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        return cls(json.loads(text))
