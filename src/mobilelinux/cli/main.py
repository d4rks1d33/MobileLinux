"""``mobilelinux`` command-line entry point.

Subcommands are thin wrappers that delegate to the core/installer/distro/ota
layers. The CLI never contains device-specific logic; that all lives in the
device definitions and the strategy backends.
"""

from __future__ import annotations

import argparse
import sys

from ..core import ui
from ..core.errors import MobileLinuxError
from .context import Context
from . import commands


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mobilelinux",
        description="Automated device porting platform for mobile Linux.",
    )
    p.add_argument("--repo", help="path to the mobilelinux repository (default: autodetect)")
    p.add_argument("--dry-run", action="store_true", help="print actions without executing destructive/build commands")
    p.add_argument("-q", "--quiet", action="store_true", help="reduce output")
    p.add_argument("-y", "--yes", action="store_true", help="assume yes to confirmations (dangerous)")

    sub = p.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("list-devices", help="list all registered devices")

    sp = sub.add_parser("device-info", help="show details for a device")
    sp.add_argument("device")

    sp = sub.add_parser("check", help="compatibility/hardware-support report for a device")
    sp.add_argument("device")

    sp = sub.add_parser("validate", help="validate device definitions against the schema")
    sp.add_argument("device", nargs="?", help="a specific device (default: all)")

    sp = sub.add_parser("detect", help="detect a connected device (usb/adb/fastboot)")

    sp = sub.add_parser("build", help="build images for a device")
    sp.add_argument("device")
    sp.add_argument("--distro", default=None, help="distribution backend (default from config)")
    sp.add_argument("--desktop", default=None, help="desktop environment")
    sp.add_argument("--profile", default=None, help="distro profile, e.g. 'security'")

    sp = sub.add_parser("flash", help="install/flash a device using its declared strategy")
    sp.add_argument("device")
    sp.add_argument("--recovery", action="store_true", help="use the recovery/rescue flow")

    sp = sub.add_parser("test", help="run the hardware test suite (mostly on-device)")
    sp.add_argument("device")
    sp.add_argument("--only", help="comma-separated subset of tests")

    sp = sub.add_parser("release", help="produce a signed release + OTA metadata")
    sp.add_argument("device")
    sp.add_argument("--version", required=True)
    sp.add_argument("--channel", default="stable")

    sp = sub.add_parser("update", help="OTA update client (run on device)")
    sp.add_argument("--check", action="store_true")
    sp.add_argument("--download", action="store_true")
    sp.add_argument("--install", action="store_true")
    sp.add_argument("--rollback", action="store_true")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--channel", default=None)

    sub.add_parser("security-status", help="show version + security patch level + affected CVEs")

    sp = sub.add_parser("import", help="import a device definition from postmarketOS pmaports")
    sp.add_argument("source", help="pmaports path or device codename")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        ctx = Context.create(
            repo_path=args.repo,
            dry_run=getattr(args, "dry_run", False),
            verbose=not getattr(args, "quiet", False),
            assume_yes=getattr(args, "yes", False),
        )
        return commands.dispatch(ctx, args)
    except MobileLinuxError as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        ui.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
