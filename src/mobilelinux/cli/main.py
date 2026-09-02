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


def _add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    # When suppress=True (the per-subcommand copy), unset flags are omitted from
    # the namespace instead of defaulting to False, so a global flag given
    # BEFORE the subcommand (parsed at the top level) is not clobbered back to
    # its default by the subparser.
    d = argparse.SUPPRESS if suppress else None
    parser.add_argument("--repo", default=d,
                        help="path to the mobilelinux repository (default: autodetect)")
    parser.add_argument("--dry-run", action="store_true", default=d,
                        help="print actions without executing anything")
    parser.add_argument("--execute", action="store_true", default=d,
                        help="actually run build/flash steps (default: plan only)")
    parser.add_argument("--allow-dangerous", action="store_true", default=d,
                        help="permit ops that touch real block/loop devices (implies --execute)")
    parser.add_argument("-q", "--quiet", action="store_true", default=d, help="reduce output")
    parser.add_argument("-y", "--yes", action="store_true", default=d,
                        help="assume yes to confirmations (dangerous)")


def build_parser() -> argparse.ArgumentParser:
    # A parent parser lets the global flags appear either before OR after the
    # subcommand (e.g. `mobilelinux flash rhodep --dry-run`). The subcommand copy
    # uses SUPPRESS defaults so it doesn't overwrite flags given before it.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_flags(common, suppress=True)

    p = argparse.ArgumentParser(
        prog="mobilelinux",
        description="Automated device porting platform for mobile Linux.",
    )
    _add_global_flags(p, suppress=False)

    sub = p.add_subparsers(dest="command", metavar="<command>")

    def add(name, help):
        return sub.add_parser(name, help=help, parents=[common])

    add("list-devices", "list all registered devices")

    sp = add("device-info", "show details for a device")
    sp.add_argument("device")

    sp = add("check", "compatibility/hardware-support report for a device")
    sp.add_argument("device")

    sp = add("validate", "validate device definitions against the schema")
    sp.add_argument("device", nargs="?", help="a specific device (default: all)")

    add("detect", "detect a connected device (usb/adb/fastboot)")

    sp = add("build", "build images for a device")
    sp.add_argument("device")
    sp.add_argument("--distro", default=None, help="distribution backend (default from config)")
    sp.add_argument("--desktop", default=None, help="desktop environment")
    sp.add_argument("--profile", default=None, help="distro profile, e.g. 'security'")

    sp = add("kernel", "build just the kernel for a device+flavor (swap active aport)")
    sp.add_argument("device")
    sp.add_argument("--distro", default=None, help="distro whose flavor to build")
    sp.add_argument("--flavor", default=None, help="explicit kernel flavor (e.g. pmos, kali)")

    sp = add("kernel-config", "interactively enable/disable kernel modules in a flavor")
    sp.add_argument("device")
    sp.add_argument("--flavor", default=None, help="kernel flavor to edit (e.g. kali)")
    sp.add_argument("--distro", default=None, help="distro whose flavor to edit")
    sp.add_argument("--preset", default=None, help="apply a catalog preset (e.g. nethunter-full)")
    sp.add_argument("--enable", action="append", help="enable a symbol (repeatable)")
    sp.add_argument("--disable", action="append", help="disable a symbol (repeatable)")
    sp.add_argument("--show", action="store_true", help="print current state and exit")
    sp.add_argument("--menuconfig", action="store_true", help="defer to kernel's make menuconfig")

    sp = add("flash", "install/flash a device using its declared strategy")
    sp.add_argument("device")
    sp.add_argument("--recovery", action="store_true", help="use the recovery/rescue flow")

    sp = add("test", "run the hardware test suite (mostly on-device)")
    sp.add_argument("device")
    sp.add_argument("--only", help="comma-separated subset of tests")

    sp = add("release", "produce a signed release + OTA metadata")
    sp.add_argument("device")
    sp.add_argument("--version", required=True)
    sp.add_argument("--channel", default="stable")

    sp = add("update", "OTA update client (run on device)")
    sp.add_argument("--check", action="store_true")
    sp.add_argument("--download", action="store_true")
    sp.add_argument("--install", action="store_true")
    sp.add_argument("--rollback", action="store_true")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--channel", default=None)

    add("security-status", "show version + security patch level + affected CVEs")

    sp = add("keygen", "generate a signing keypair for a channel")
    sp.add_argument("--channel", default="dev")
    sp.add_argument("--key-id", default=None)

    sp = add("import", "import a device definition from postmarketOS pmaports")
    sp.add_argument("source", help="pmaports path or device codename")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        allow_dangerous = getattr(args, "allow_dangerous", False)
        ctx = Context.create(
            repo_path=args.repo,
            dry_run=getattr(args, "dry_run", False),
            verbose=not getattr(args, "quiet", False),
            assume_yes=getattr(args, "yes", False),
            execute=getattr(args, "execute", False) or allow_dangerous,
            allow_dangerous=allow_dangerous,
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
