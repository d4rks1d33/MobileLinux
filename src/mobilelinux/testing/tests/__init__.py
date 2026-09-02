"""Registry of hardware test modules."""

from __future__ import annotations

from ..base import HardwareTest
from . import generic

_ALL: dict[str, HardwareTest] = {}


def _register(cls: type[HardwareTest]) -> None:
    inst = cls()
    _ALL[inst.name] = inst


for _cls in (
    generic.BootTest, generic.DisplayTest, generic.TouchTest, generic.GpuTest,
    generic.StorageTest, generic.UsbTest, generic.WifiTest, generic.BluetoothTest,
    generic.AudioTest, generic.BatteryTest, generic.ChargingTest, generic.ModemTest,
    generic.GnssTest, generic.NfcTest, generic.SensorsTest, generic.VibratorTest,
):
    _register(_cls)


def get_test(name: str) -> HardwareTest | None:
    return _ALL.get(name)


def all_tests() -> dict[str, HardwareTest]:
    return dict(_ALL)
