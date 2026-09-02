"""Typed accessors over a parsed device definition.

The device definition is stored as YAML and validated against
``schema/device.schema.json``. Rather than duplicate the whole schema as
dataclasses (which would drift), :class:`Device` wraps the raw dict and exposes
convenient, well-named accessors for the parts the framework uses often.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Canonical ordering of hardware features for reports (core first).
HW_ORDER = [
    "boot", "kernel", "device_tree",  # synthesized rows, see checker
    "display", "touchscreen", "gpu", "storage", "usb",
    "wifi", "bluetooth", "audio", "battery", "charging",
    "modem", "gnss", "nfc", "fingerprint", "camera", "sensors", "vibrator",
]

# Default weights for the overall-support percentage. Core bring-up features
# weigh more than nice-to-haves. Overridable per-feature via ``weight``.
DEFAULT_WEIGHTS = {
    "display": 3.0, "touchscreen": 3.0, "storage": 3.0, "usb": 2.0,
    "gpu": 2.0, "wifi": 2.0, "bluetooth": 1.0, "audio": 1.5,
    "battery": 2.0, "charging": 2.0,
    "modem": 1.5, "gnss": 0.5, "nfc": 0.5, "fingerprint": 0.5,
    "camera": 0.5, "sensors": 1.0, "vibrator": 0.25,
}

# How much "credit" each status earns toward the support score.
STATUS_SCORE = {
    "supported": 1.0,
    "partial": 0.5,
    "broken": 0.0,
    "untested": 0.0,
    "unsupported": 0.0,
    "not-present": None,  # excluded from the denominator
}


@dataclass
class Feature:
    name: str
    status: str
    weight: float
    driver: str | None
    evidence: str | None
    notes: str | None
    caveats: list[str]
    test: str | None

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "Feature":
        return cls(
            name=name,
            status=d.get("status", "untested"),
            weight=float(d.get("weight", DEFAULT_WEIGHTS.get(name, 1.0))),
            driver=d.get("driver"),
            evidence=d.get("evidence"),
            notes=d.get("notes"),
            caveats=list(d.get("caveats", [])),
            test=d.get("test"),
        )


class Device:
    """Wrapper around a validated device-definition dict."""

    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path  # path to the device.yaml
        self.dir = path.parent

    # ---- identity ---------------------------------------------------------
    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def vendor(self) -> str:
        return self.data["vendor"]

    @property
    def model(self) -> str:
        return self.data["model"]

    @property
    def codename(self) -> str:
        return self.data["codename"]

    @property
    def aliases(self) -> list[str]:
        return list(self.data.get("aliases", []))

    @property
    def architecture(self) -> str:
        return self.data["architecture"]

    @property
    def maturity(self) -> str:
        return self.data.get("maturity", "testing")

    @property
    def chassis(self) -> str:
        return self.data.get("chassis", "handset")

    # ---- soc / kernel -----------------------------------------------------
    @property
    def soc(self) -> dict[str, Any]:
        return self.data.get("soc", {})

    @property
    def soc_family(self) -> str:
        return self.soc.get("family", "")

    @property
    def soc_vendor(self) -> str:
        return self.soc.get("vendor", "")

    @property
    def kernel(self) -> dict[str, Any]:
        return self.data.get("kernel", {})

    @property
    def kernel_provider(self) -> dict[str, Any]:
        return self.kernel.get("provider", {})

    @property
    def kernel_flavors(self) -> dict[str, Any]:
        return self.kernel.get("flavors", {})

    def flavor_for_distro(self, distro: str) -> str | None:
        """Return the kernel flavor name whose 'distros' includes ``distro``."""
        for name, flavor in self.kernel_flavors.items():
            if distro in flavor.get("distros", []):
                return name
        # Fall back to a flavor literally named after the distro.
        if distro in self.kernel_flavors:
            return distro
        return None

    @property
    def device_tree(self) -> dict[str, Any]:
        return self.data.get("device_tree", {})

    @property
    def firmware(self) -> dict[str, Any]:
        return self.data.get("firmware", {})

    # ---- hardware ---------------------------------------------------------
    @property
    def hardware(self) -> dict[str, dict[str, Any]]:
        return self.data.get("hardware", {})

    def features(self) -> list[Feature]:
        hw = self.hardware
        return [Feature.from_dict(name, hw[name]) for name in hw]

    def feature(self, name: str) -> Feature | None:
        d = self.hardware.get(name)
        return Feature.from_dict(name, d) if d else None

    # ---- boot / storage / install / ota ----------------------------------
    @property
    def boot(self) -> dict[str, Any]:
        return self.data.get("boot", {})

    @property
    def storage(self) -> dict[str, Any]:
        return self.data.get("storage", {})

    @property
    def install(self) -> dict[str, Any]:
        return self.data.get("install", {})

    @property
    def install_strategy(self) -> str:
        return self.install.get("strategy", "custom")

    @property
    def ota(self) -> dict[str, Any]:
        return self.data.get("ota", {})

    @property
    def ota_strategy(self) -> str:
        return self.ota.get("strategy", "single-rootfs")

    @property
    def device_packages(self) -> list[dict[str, Any]]:
        return list(self.data.get("device_packages", []))

    @property
    def first_boot(self) -> dict[str, Any]:
        return self.data.get("first_boot", {})

    @property
    def tests(self) -> list[str]:
        return list(self.data.get("tests", []))

    @property
    def sources(self) -> dict[str, Any]:
        return self.data.get("sources", {})

    # ---- derived ----------------------------------------------------------
    def support_score(self) -> tuple[float, float]:
        """Return (weighted_score, weighted_max) over evidence-based features.

        not-present features are excluded. Percentage = score / max.
        """
        score = 0.0
        maximum = 0.0
        for feat in self.features():
            credit = STATUS_SCORE.get(feat.status)
            if credit is None:  # not-present
                continue
            maximum += feat.weight
            score += feat.weight * credit
        return score, maximum

    def support_percent(self) -> int:
        score, maximum = self.support_score()
        if maximum <= 0:
            return 0
        return int(round(100.0 * score / maximum))

    def pretty_name(self) -> str:
        return f"{self.model} ({self.codename})"

    def __repr__(self) -> str:
        return f"<Device {self.id} {self.model!r}>"


def iter_hardware_rows(device: Device) -> Iterable[Feature]:
    """Yield features in canonical order, then any extras."""
    hw = device.hardware
    seen = set()
    for name in HW_ORDER:
        if name in hw:
            seen.add(name)
            yield Feature.from_dict(name, hw[name])
    for name in hw:
        if name not in seen:
            yield Feature.from_dict(name, hw[name])
