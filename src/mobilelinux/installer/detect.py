"""Device detection over USB via fastboot / adb.

`detect` probes the connected device, reports what it found, and matches it
against the registry so the correct install strategy can be chosen
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core import tools, ui
from ..core.registry import Registry


@dataclass
class DetectedDevice:
    transport: str = ""            # 'fastboot' | 'adb' | ''
    serial: str = ""
    manufacturer: str = ""
    model: str = ""
    codename: str = ""
    product: str = ""
    is_ab: bool | None = None
    current_slot: str = ""
    fastbootd: bool | None = None
    raw: dict[str, str] = field(default_factory=dict)


def _fastboot_getvar(runner: tools.Runner, var: str) -> str | None:
    # `fastboot getvar X` prints "X: value" to stderr.
    if not tools.have("fastboot"):
        return None
    import subprocess
    try:
        cp = subprocess.run(
            ["fastboot", "getvar", var],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    out = (cp.stderr or "") + (cp.stdout or "")
    for line in out.splitlines():
        if line.startswith(f"{var}:"):
            return line.split(":", 1)[1].strip()
    return None


def detect_fastboot(runner: tools.Runner) -> DetectedDevice | None:
    if not tools.have("fastboot"):
        return None
    import subprocess
    try:
        cp = subprocess.run(["fastboot", "devices"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    lines = [l for l in cp.stdout.splitlines() if l.strip()]
    if not lines:
        return None
    serial = lines[0].split()[0]
    d = DetectedDevice(transport="fastboot", serial=serial)
    d.product = _fastboot_getvar(runner, "product") or ""
    d.raw["product"] = d.product
    slot_count = _fastboot_getvar(runner, "slot-count")
    d.is_ab = (slot_count not in (None, "", "0", "1"))
    d.current_slot = _fastboot_getvar(runner, "current-slot") or ""
    # fastbootd exposes 'is-userspace: yes'
    d.fastbootd = (_fastboot_getvar(runner, "is-userspace") == "yes")
    return d


def detect_adb(runner: tools.Runner) -> DetectedDevice | None:
    if not tools.have("adb"):
        return None
    import subprocess
    try:
        cp = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    serials = [l.split()[0] for l in cp.stdout.splitlines()[1:] if l.strip() and "device" in l]
    if not serials:
        return None
    d = DetectedDevice(transport="adb", serial=serials[0])

    def getprop(p: str) -> str:
        try:
            r = subprocess.run(["adb", "shell", "getprop", p], capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        except Exception:
            return ""

    d.manufacturer = getprop("ro.product.manufacturer")
    d.model = getprop("ro.product.model")
    d.codename = getprop("ro.product.device") or getprop("ro.build.product")
    d.raw = {"device": d.codename, "model": d.model, "manufacturer": d.manufacturer}
    return d


def detect_command(ctx) -> int:
    runner = ctx.runner()
    registry: Registry = ctx.registry

    if not tools.have("fastboot") and not tools.have("adb"):
        ui.error("neither fastboot nor adb is installed; cannot detect a device")
        runner.missing.check("fastboot")
        runner.missing.check("adb")
        runner.missing.report()
        return 1

    detected = detect_fastboot(runner) or detect_adb(runner)
    if not detected:
        ui.warn("no device detected over fastboot or adb")
        ui.note("  Put the phone in fastboot/bootloader mode, or enable USB debugging (adb).")
        return 1

    ui.header("Detected device")
    if detected.transport == "adb":
        print(f"  Manufacturer: {detected.manufacturer or '?'}")
        print(f"  Model:        {detected.model or '?'}")
        print(f"  Codename:     {detected.codename or '?'}")
    else:
        print(f"  Transport:    fastboot")
        print(f"  Serial:       {detected.serial}")
        print(f"  Product:      {detected.product or '?'}")
        print(f"  A/B:          {_tri(detected.is_ab)}")
        print(f"  Fastbootd:    {_tri(detected.fastbootd)}")
        if detected.current_slot:
            print(f"  Active slot:  {detected.current_slot}")

    # Match against registry
    match = _match_registry(registry, detected)
    ui.header("Device definition")
    if match:
        print(f"  {ui.green('\u2713')} {match.id}  ({match.model})")
        ui.header("Installation strategy")
        print(f"  {ui.green('\u2713')} {match.install_strategy}")
    else:
        print(f"  {ui.yellow('\u2717')} no matching device definition")
        ui.note("  You can create one with 'mobilelinux import <pmaports-path>' or by hand.")
    return 0


def _match_registry(registry: Registry, d: DetectedDevice):
    candidates = [d.codename, d.product, d.model]
    for c in candidates:
        if not c:
            continue
        if registry.exists(c):
            return registry.get(c, validate_schema=False)
    return None


def _tri(v: bool | None) -> str:
    if v is None:
        return ui.grey("unknown")
    return ui.green("yes") if v else "no"
