"""On-device OTA state.

Stored at /etc/mobilelinux/state.json (or a local override for testing). Records
the installed version, channel, security patch level, device id and the update
public key location.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_STATE_PATH = Path(os.environ.get("MOBILELINUX_STATE", "/etc/mobilelinux/state.json"))


@dataclass
class DeviceState:
    device_id: str = ""
    version: str = "0.0.0"
    channel: str = "stable"
    security_patch_level: str = ""
    kernel_version: str = ""
    metadata_url: str = ""           # base URL for manifests
    public_key: str = "/etc/mobilelinux/keys/stable.ed25519.pub"
    ota_strategy: str = "single-rootfs"
    last_result: str = ""            # 'success' | 'rolled-back' | ''

    @classmethod
    def load(cls, path: Path | None = None) -> "DeviceState":
        p = path or DEFAULT_STATE_PATH
        if p.exists():
            return cls(**{**asdict(cls()), **json.loads(p.read_text())})
        return cls()

    def save(self, path: Path | None = None) -> None:
        p = path or DEFAULT_STATE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
