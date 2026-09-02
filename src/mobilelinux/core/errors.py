"""Exception hierarchy for MobileLinux."""

from __future__ import annotations


class MobileLinuxError(Exception):
    """Base class for all framework errors."""


class RepoNotFoundError(MobileLinuxError):
    """The mobilelinux repository root could not be located."""


class DeviceNotFoundError(MobileLinuxError):
    """A requested device id/codename is not registered."""


class SchemaValidationError(MobileLinuxError):
    """A device definition failed schema validation."""

    def __init__(self, device: str, errors: list[str]):
        self.device = device
        self.errors = errors
        joined = "\n  - ".join(errors)
        super().__init__(f"device '{device}' failed validation:\n  - {joined}")


class ToolMissingError(MobileLinuxError):
    """A required external tool is not available and no dry-run fallback applies."""

    def __init__(self, tool: str, hint: str | None = None):
        self.tool = tool
        self.hint = hint
        msg = f"required tool '{tool}' is not installed"
        if hint:
            msg += f"\n  install hint: {hint}"
        super().__init__(msg)


class StrategyError(MobileLinuxError):
    """An install/flash strategy could not be executed."""


class BuildError(MobileLinuxError):
    """A build step failed."""


class SafetyAbort(MobileLinuxError):
    """A destructive operation was aborted for safety (e.g. device mismatch)."""
