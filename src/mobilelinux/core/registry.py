"""Device discovery, loading and validation.

Device definitions live at ``devices/<vendor>/<codename>/device.yaml``. The
registry scans that tree, parses YAML, validates against the schema, and
returns :class:`Device` objects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .errors import DeviceNotFoundError, SchemaValidationError
from .model import Device
from .repo import Repo
from .validate import validate

try:
    import yaml
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install it with: pip install pyyaml"
    ) from exc

DEVICE_FILENAME = "device.yaml"


class Registry:
    def __init__(self, repo: Repo):
        self.repo = repo
        self._cache: dict[str, Device] = {}

    def _device_files(self) -> Iterator[Path]:
        root = self.repo.devices_dir
        if not root.is_dir():
            return
        yield from sorted(root.glob(f"*/*/{DEVICE_FILENAME}"))

    def load_all(self, *, validate_schema: bool = True) -> list[Device]:
        devices: list[Device] = []
        for path in self._device_files():
            devices.append(self._load_file(path, validate_schema=validate_schema))
        # keep registry cache
        for d in devices:
            self._cache[d.id] = d
        return devices

    def _load_file(self, path: Path, *, validate_schema: bool) -> Device:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if validate_schema:
            errors = validate(data, self.repo.schema_file)
            if errors:
                raise SchemaValidationError(data.get("id", path.parent.name), errors)
        return Device(data, path)

    def get(self, id_or_codename: str, *, validate_schema: bool = True) -> Device:
        """Resolve a device by id, codename, or alias."""
        if id_or_codename in self._cache:
            return self._cache[id_or_codename]
        for path in self._device_files():
            # cheap pre-parse to match id/codename without full validation
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            names = {data.get("id"), data.get("codename"), *data.get("aliases", [])}
            names.discard(None)
            if id_or_codename in names:
                dev = self._load_file(path, validate_schema=validate_schema)
                self._cache[dev.id] = dev
                return dev
        raise DeviceNotFoundError(
            f"no device matching '{id_or_codename}'. "
            f"Run 'mobilelinux list-devices' to see available devices."
        )

    def exists(self, id_or_codename: str) -> bool:
        try:
            self.get(id_or_codename, validate_schema=False)
            return True
        except DeviceNotFoundError:
            return False
