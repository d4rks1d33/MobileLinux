"""Subcommand implementations (dispatch table)."""

from __future__ import annotations

import argparse

from ..core import ui
from ..core.model import Device, iter_hardware_rows
from .context import Context


def dispatch(ctx: Context, args: argparse.Namespace) -> int:
    handler = _HANDLERS.get(args.command)
    if handler is None:
        ui.error(f"unknown command: {args.command}")
        return 2
    return handler(ctx, args)


# --------------------------------------------------------------------------
# list-devices
# --------------------------------------------------------------------------
def cmd_list_devices(ctx: Context, args: argparse.Namespace) -> int:
    devices = ctx.registry.load_all(validate_schema=False)
    if not devices:
        ui.warn("no devices found under devices/")
        return 0
    devices.sort(key=lambda d: (d.vendor, d.id))
    width = max(len(d.id) for d in devices)
    ui.header(f"Registered devices ({len(devices)})")
    for d in devices:
        pct = d.support_percent()
        strat = d.install_strategy
        print(
            f"  {ui.bold(d.id.ljust(width))}  "
            f"{d.model:<22} {d.soc_family:<12} "
            f"{ui.grey(strat):<18} "
            f"{_pct_color(pct)}"
        )
    return 0


def _pct_color(pct: int) -> str:
    s = f"{pct}%"
    if pct >= 80:
        return ui.green(s)
    if pct >= 50:
        return ui.yellow(s)
    return ui.red(s)


# --------------------------------------------------------------------------
# device-info
# --------------------------------------------------------------------------
def cmd_device_info(ctx: Context, args: argparse.Namespace) -> int:
    d = ctx.registry.get(args.device, validate_schema=False)
    ui.header(f"{d.model} ({d.codename})")
    print(f"  id            {d.id}")
    print(f"  vendor        {d.vendor}")
    print(f"  architecture  {d.architecture}")
    print(f"  SoC           {d.soc.get('marketing_name', d.soc_family)} "
          f"[{d.soc_vendor}/{d.soc_family}]")
    k = d.kernel
    print(f"  kernel        {k.get('type','?')} {k.get('version','?')} "
          f"({k.get('build',{}).get('method','make')})")
    print(f"  maturity      {d.maturity}")

    ui.header("Install")
    inst = d.install
    print(f"  strategy      {inst.get('strategy')}")
    print(f"  A/B slots     {_yn(inst.get('ab_slots', False))}"
          + (f"  slots={inst.get('slots')}" if inst.get('slots') else ""))
    print(f"  fastbootd     {_yn(inst.get('fastbootd', False))}")
    if inst.get("boot_partition"):
        print(f"  boot part     {inst['boot_partition']}")
    rt = inst.get("rootfs_target", {})
    if rt:
        print(f"  rootfs        {rt.get('partition')} via {rt.get('method')}")
    rescue = inst.get("rescue", {})
    if rescue.get("required"):
        print(f"  rescue        {rescue.get('method')} @ {rescue.get('transport','')}")

    ui.header("OTA")
    ota = d.ota
    print(f"  strategy      {ota.get('strategy','single-rootfs')}")
    print(f"  rollback      {_yn(ota.get('rollback', False))}")

    ui.header("Hardware")
    for feat in iter_hardware_rows(d):
        glyph = ui.status_glyph(feat.status)
        line = f"  {glyph} {feat.name:<14} {feat.status}"
        if feat.driver:
            line += ui.grey(f"  [{feat.driver}]")
        print(line)

    src = d.sources
    if src:
        ui.header("Sources")
        for key in ("imported_from", "pmaports", "postmarketos_wiki"):
            if src.get(key):
                print(f"  {key:<16} {src[key]}")
    return 0


def _yn(b: bool) -> str:
    return ui.green("yes") if b else ui.grey("no")


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------
def cmd_validate(ctx: Context, args: argparse.Namespace) -> int:
    from ..core.validate import validate
    import yaml

    if args.device:
        paths = [ctx.registry.get(args.device, validate_schema=False).path]
    else:
        paths = sorted(ctx.repo.devices_dir.glob("*/*/device.yaml"))

    ok = True
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        errors = validate(data, ctx.repo.schema_file)
        name = data.get("id", path.parent.name)
        if errors:
            ok = False
            print(f"  {ui.red('\u2717')} {name}")
            for e in errors:
                print(f"      {e}")
        else:
            print(f"  {ui.green('\u2713')} {name}")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# thin wrappers to feature modules (defined in later phases)
# --------------------------------------------------------------------------
def cmd_check(ctx: Context, args: argparse.Namespace) -> int:
    from ..testing.checker import run_check
    d = ctx.registry.get(args.device, validate_schema=False)
    return run_check(d)


def cmd_detect(ctx: Context, args: argparse.Namespace) -> int:
    from ..installer.detect import detect_command
    return detect_command(ctx)


def cmd_build(ctx: Context, args: argparse.Namespace) -> int:
    from ..core.build import build_command
    d = ctx.registry.get(args.device, validate_schema=False)
    return build_command(ctx, d, distro=args.distro, desktop=args.desktop, profile=args.profile)


def cmd_flash(ctx: Context, args: argparse.Namespace) -> int:
    from ..installer.flash import flash_command
    d = ctx.registry.get(args.device, validate_schema=False)
    return flash_command(ctx, d, recovery=args.recovery)


def cmd_test(ctx: Context, args: argparse.Namespace) -> int:
    from ..testing.runner import test_command
    d = ctx.registry.get(args.device, validate_schema=False)
    only = args.only.split(",") if args.only else None
    return test_command(ctx, d, only=only)


def cmd_release(ctx: Context, args: argparse.Namespace) -> int:
    from ..ota.release import release_command
    d = ctx.registry.get(args.device, validate_schema=False)
    return release_command(ctx, d, version=args.version, channel=args.channel)


def cmd_update(ctx: Context, args: argparse.Namespace) -> int:
    from ..ota.client import update_command
    return update_command(ctx, args)


def cmd_security_status(ctx: Context, args: argparse.Namespace) -> int:
    from ..security.status import security_status_command
    return security_status_command(ctx)


def cmd_import(ctx: Context, args: argparse.Namespace) -> int:
    from ..importers.pmaports import import_command
    return import_command(ctx, args.source)


def cmd_keygen(ctx: Context, args: argparse.Namespace) -> int:
    from ..ota import signing
    keys = ctx.repo.root / "keys"
    channel = args.channel
    priv = keys / f"{channel}.ed25519.key"
    pub = keys / f"{channel}.ed25519.pub"
    try:
        signing.generate_keypair(priv, pub, key_id=args.key_id or channel)
    except signing.SigningError as exc:
        ui.error(str(exc))
        return 1
    if channel in ("stable", "beta"):
        ui.warn("this is a RELEASE channel key. Keep the private key OFFLINE (HSM/air-gapped). "
                "Never commit it. Only the .pub belongs on devices.")
    return 0


_HANDLERS = {
    "list-devices": cmd_list_devices,
    "device-info": cmd_device_info,
    "validate": cmd_validate,
    "check": cmd_check,
    "detect": cmd_detect,
    "build": cmd_build,
    "flash": cmd_flash,
    "test": cmd_test,
    "release": cmd_release,
    "update": cmd_update,
    "security-status": cmd_security_status,
    "import": cmd_import,
    "keygen": cmd_keygen,
}
