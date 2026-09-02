"""`mobilelinux test <device>` — run the modular hardware test suite.

Runs the test modules the device declares (``tests:``). Most tests only make
sense on the target hardware; off-device they report ``skip (run on device)``.
Produces a hardware status report and a summary that can be used as evidence
for the compatibility checker.
"""

from __future__ import annotations

from ..core import ui
from ..core.model import Device
from .base import Outcome, TestEnv
from .tests import get_test, all_tests

_GLYPH = {
    Outcome.PASS: ui.green("\u2713"),
    Outcome.FAIL: ui.red("\u2717"),
    Outcome.WARN: ui.yellow("\u26a0"),
    Outcome.SKIP: ui.grey("\u2014"),
}


def test_command(ctx, device: Device, *, only: list[str] | None = None) -> int:
    env = TestEnv.detect(device)
    names = device.tests or list(all_tests().keys())
    if only:
        names = [n for n in names if n in only]

    ui.header(f"Hardware tests: {device.pretty_name()}")
    if not env.on_device:
        ui.note("  not running on the target device; hardware probes will be skipped.")
        ui.note("  copy mobilelinux to the phone and run there for real results.")
    print()

    results = []
    for name in names:
        test = get_test(name)
        if test is None:
            ui.warn(f"unknown test '{name}'")
            continue
        res = test.run(device, env)
        results.append(res)
        line = f"  {_GLYPH[res.outcome]}  {test.title:<18} {res.message}"
        print(line)
        for d in res.details:
            print(ui.grey(f"        {d}"))

    # Summary
    counts = {o: 0 for o in Outcome}
    for r in results:
        counts[r.outcome] += 1
    ui.header("Summary")
    print(f"  {ui.green(str(counts[Outcome.PASS]) + ' pass')}  "
          f"{ui.yellow(str(counts[Outcome.WARN]) + ' warn')}  "
          f"{ui.red(str(counts[Outcome.FAIL]) + ' fail')}  "
          f"{ui.grey(str(counts[Outcome.SKIP]) + ' skip')}")

    if not env.on_device:
        return 0
    # On-device: fail the command if any hardware test failed.
    return 1 if counts[Outcome.FAIL] else 0
