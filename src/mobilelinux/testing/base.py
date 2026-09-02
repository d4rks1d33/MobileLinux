"""Hardware test framework.

Each test module verifies one feature by probing the running system (sysfs,
DRM, ALSA, qrtr, etc). Tests are modular and declarative about *where* they can
run: most only make sense **on the device**. When run off-device the runner
reports them as ``skipped (run on device)`` rather than guessing.

A test returns a :class:`TestResult` with an outcome and a short message. The
device definition lists which tests apply (``tests:``), and each hardware
feature may name the test that verifies it (``hardware.<f>.test``), so a passing
test can justify a ``supported`` status (the evidence rule).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass
class TestResult:
    name: str
    outcome: Outcome
    message: str = ""
    details: list[str] = field(default_factory=list)


class HardwareTest:
    """Base class for a hardware test module."""

    #: unique test name, matches device tests[] and hardware.<f>.test
    name = "base"
    #: human title
    title = "Base test"
    #: whether this test must run on the target device
    on_device_only = True

    def applicable(self, device) -> bool:
        """Whether this test is relevant to the device (default: listed in tests)."""
        return self.name in device.tests

    def run(self, device, env: "TestEnv") -> TestResult:  # pragma: no cover - interface
        raise NotImplementedError

    # -- probing helpers ----------------------------------------------------
    @staticmethod
    def read(path: str) -> str | None:
        try:
            with open(path, "r", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return None

    @staticmethod
    def exists(path: str) -> bool:
        return os.path.exists(path)

    @staticmethod
    def glob(pattern: str) -> list[str]:
        from glob import glob
        return glob(pattern)

    @staticmethod
    def have_cmd(name: str) -> bool:
        return shutil.which(name) is not None


@dataclass
class TestEnv:
    """Execution environment for tests."""
    on_device: bool          # are we running on the target hardware?
    device_codename: str = ""

    @classmethod
    def detect(cls, device) -> "TestEnv":
        """Heuristic: we're on-device if the machine model / DT compatible
        matches the device codename."""
        on_device = _running_on_device(device)
        return cls(on_device=on_device, device_codename=device.codename)


def _running_on_device(device) -> bool:
    # DT 'compatible' or model usually contains the codename/soc.
    for path in ("/sys/firmware/devicetree/base/model",
                 "/sys/firmware/devicetree/base/compatible",
                 "/proc/device-tree/model"):
        val = HardwareTest.read(path)
        if val:
            low = val.lower()
            if device.codename.lower() in low or device.soc_family.lower() in low:
                return True
    return False
