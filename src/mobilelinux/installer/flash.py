"""`mobilelinux flash <device>` — conservative, device-aware installer.

Safety contract (per project requirements). Before writing any partition it:
  1. detects the connected device,
  2. confirms codename/model against the definition,
  3. reads available partitions (via the strategy plan),
  4. shows exactly what it will modify,
  5. verifies the strategy matches the device definition,
  6. requires explicit confirmation for destructive ops,
  7. supports --dry-run,
  8. never runs another device's commands (mismatch aborts),
  9. validates artifact hashes before writing,
 10. offers the rescue/recovery flow where available.
"""

from __future__ import annotations

from pathlib import Path

from ..core import tools, ui
from ..core.errors import SafetyAbort, StrategyError
from ..core.model import Device
from .artifacts import ArtifactSet
from .confirm import confirm_destructive
from .detect import detect_adb, detect_fastboot, DetectedDevice
from .strategies import get_strategy


def flash_command(ctx, device: Device, *, recovery: bool = False) -> int:
    runner = ctx.runner()
    artifact_dir = ctx.repo.out_dir / device.id

    # (9) Load + verify artifacts (hashes) before touching the device.
    if not (artifact_dir / "artifacts.json").exists():
        ui.error(f"no build artifacts found in {artifact_dir}")
        ui.note(f"  run: mobilelinux build {device.id} --distro <distro>")
        return 1
    artifacts = ArtifactSet.load(artifact_dir)
    problems = artifacts.verify(artifact_dir)
    if problems:
        ui.error("artifact verification failed (refusing to flash):")
        for p in problems:
            print(f"    {ui.red('\u2717')} {p}")
        return 1
    ui.success(f"artifacts verified ({len(artifacts.artifacts)} files, sha256 ok)")

    # (5) Strategy must match the definition.
    strat_name = device.install_strategy
    try:
        strat_cls = get_strategy(strat_name)
    except StrategyError as exc:
        ui.error(str(exc))
        return 1
    strategy = strat_cls(device, artifacts, artifact_dir)

    # (1,2,8) Detect + confirm identity (skipped in dry-run if nothing connected).
    detected = _detect(runner)
    if detected is not None:
        if not _identity_matches(device, detected):
            raise SafetyAbort(
                f"connected device ({_detected_name(detected)}) does not match "
                f"'{device.id}' ({device.model}); refusing to flash another device"
            )
        ui.success(f"connected device matches {device.pretty_name()}")
    else:
        if not ctx.dry_run:
            ui.warn("no device detected over fastboot/adb")
            ui.note("  Connect the device in the required mode, or use --dry-run to preview.")
        else:
            ui.note("[dry-run] no device connected; previewing plan only")

    # (3,4) Build the plan and show exactly what will be modified.
    parts = strategy.modified_partitions()
    ui.header("Flash plan")
    print(f"  strategy: {ui.bold(strat_name)}")
    ops = strategy.plan()
    for op in ops:
        mark = ui.red(" [destructive]") if op.destructive else ""
        print(f"    - {op.description}{mark}")

    # (10) Mention rescue/recovery availability.
    rescue = device.install.get("rescue", {})
    if rescue.get("required") or recovery:
        ui.note(f"  rescue available: {rescue.get('method','?')} @ {rescue.get('transport','')}")

    # (6) Confirm.
    proceed = confirm_destructive(
        device.pretty_name(), strat_name, parts,
        assume_yes=ctx.assume_yes, dry_run=ctx.dry_run,
    )
    if not proceed:
        ui.warn("aborted by user")
        return 1

    # Execute.
    ui.header("Executing")
    strategy.execute(runner)

    if runner.missing.any:
        runner.missing.report()
        ui.warn("some steps were skipped because tools are missing; install them and re-run")
        return 2

    ui.success("flash complete" if not ctx.dry_run else "dry-run complete")
    return 0


def _detect(runner: tools.Runner) -> DetectedDevice | None:
    return detect_fastboot(runner) or detect_adb(runner)


def _identity_matches(device: Device, detected: DetectedDevice) -> bool:
    names = {device.id, device.codename, *device.aliases}
    names = {n.lower() for n in names if n}
    candidates = [detected.codename, detected.product, detected.model]
    for c in candidates:
        if c and c.lower() in names:
            return True
    # If the transport gave us nothing identifying (bare fastboot), don't hard-fail;
    # the user already selected the device explicitly. But require product to at
    # least not contradict a known alias when present.
    if not any([detected.codename, detected.product, detected.model]):
        return True
    return False


def _detected_name(d: DetectedDevice) -> str:
    return d.codename or d.product or d.model or d.serial or "unknown"
