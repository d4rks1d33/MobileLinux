"""Compatibility checker: `mobilelinux check <device>`.

Produces an objective, evidence-based hardware-support report and an overall
support percentage. Percentages are computed from the device schema
(weighted status scores), never invented. not-present features are excluded.
"""

from __future__ import annotations

from ..core import ui
from ..core.model import Device, Feature, iter_hardware_rows

# Synthesized non-hardware rows shown at the top of the report, derived from
# structural fields of the definition (kernel/DT/boot presence).
def _structural_rows(device: Device) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    rows.append(("Kernel", "supported" if device.kernel.get("version") else "untested"))
    rows.append(("Device Tree",
                 "supported" if device.device_tree.get("dtb") else "untested"))
    rows.append(("Boot",
                 "supported" if device.boot.get("method") else "untested"))
    return rows


_LABEL = {
    "display": "Display", "touchscreen": "Touch", "gpu": "GPU",
    "storage": "Storage", "usb": "USB", "wifi": "WiFi",
    "bluetooth": "Bluetooth", "audio": "Audio", "battery": "Battery",
    "charging": "Charging", "modem": "Modem", "gnss": "GNSS", "nfc": "NFC",
    "fingerprint": "Fingerprint", "camera": "Camera", "sensors": "Sensors",
    "vibrator": "Vibrator",
}


def run_check(device: Device) -> int:
    ui.header(f"Device: {device.codename}")
    print(ui.grey(f"  {device.model}  [{device.soc_vendor}/{device.soc_family}, "
                  f"{device.kernel.get('type','?')} {device.kernel.get('version','?')}]"))
    print()

    # Structural rows
    for label, status in _structural_rows(device):
        print(f"  {ui.status_glyph(status)}  {label}")

    # Hardware rows in canonical order
    for feat in iter_hardware_rows(device):
        if feat.name in ("kernel", "device_tree", "boot"):
            continue
        label = _LABEL.get(feat.name, feat.name.title())
        line = f"  {ui.status_glyph(feat.status)}  {label}"
        if feat.status in ("partial", "broken") and feat.caveats:
            line += ui.grey(f"   \u2014 {feat.caveats[0]}")
        print(line)

    pct = device.support_percent()
    ui.header("Overall support")
    frac = pct / 100.0
    color = ui.green if pct >= 80 else (ui.yellow if pct >= 50 else ui.red)
    print(f"  {color(ui.bar(frac))} {color(str(pct) + '%')}")

    # Legend + honesty note
    print()
    _legend()
    _warnings(device)
    return 0


def _legend() -> None:
    print(ui.grey(
        f"  legend: {ui.status_glyph('supported')} supported  "
        f"{ui.status_glyph('partial')} partial  "
        f"{ui.status_glyph('broken')} broken  "
        f"{ui.status_glyph('untested')} untested  "
        f"{ui.status_glyph('not-present')} n/a"
    ))


def _warnings(device: Device) -> None:
    import sys
    sys.stdout.flush()
    broken = [f for f in device.features() if f.status == "broken"]
    untested = [f for f in device.features() if f.status == "untested"]
    if broken:
        ui.warn("broken hardware (present but unsafe/non-working):")
        for f in broken:
            msg = f.caveats[0] if f.caveats else (f.notes or "")
            print(f"    {ui.red('\u2717')} {f.name}: {msg}")
    if untested:
        names = ", ".join(f.name for f in untested)
        ui.note(f"  untested (no evidence, not counted as supported): {names}")
