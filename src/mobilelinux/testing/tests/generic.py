"""Generic hardware test modules.

Each probes the running Linux system for evidence a feature works. They are
intentionally conservative: PASS only on positive evidence, WARN on ambiguous,
FAIL on clear absence, SKIP when off-device.
"""

from __future__ import annotations

from ..base import HardwareTest, Outcome, TestEnv, TestResult


class BootTest(HardwareTest):
    name = "boot"
    title = "Kernel boot"
    on_device_only = True

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        rel = self.read("/proc/sys/kernel/osrelease") or ""
        want = device.kernel.get("version", "").replace("-", "")
        ok = bool(rel)
        msg = f"running kernel {rel}"
        if want and want.split(".")[0] not in rel:
            return TestResult(self.name, Outcome.WARN, f"{msg} (expected ~{device.kernel.get('version')})")
        return TestResult(self.name, Outcome.PASS if ok else Outcome.FAIL, msg)


class DisplayTest(HardwareTest):
    name = "display"
    title = "DRM / display"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        cards = self.glob("/sys/class/drm/card*/card*-DSI*") or self.glob("/sys/class/drm/card*")
        connected = [c for c in self.glob("/sys/class/drm/*/status")
                     if (self.read(c) or "") == "connected"]
        if connected:
            return TestResult(self.name, Outcome.PASS, f"{len(connected)} connected connector(s)")
        if cards:
            return TestResult(self.name, Outcome.WARN, "DRM present, no connected connector")
        return TestResult(self.name, Outcome.FAIL, "no DRM device")


class TouchTest(HardwareTest):
    name = "touch"
    title = "Touchscreen"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        for dev in self.glob("/sys/class/input/input*/name"):
            name = (self.read(dev) or "").lower()
            if "touch" in name or "goodix" in name or "focaltech" in name:
                return TestResult(self.name, Outcome.PASS, f"input: {name}")
        return TestResult(self.name, Outcome.FAIL, "no touchscreen input device")


class GpuTest(HardwareTest):
    name = "gpu"
    title = "GPU acceleration"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        render = self.glob("/dev/dri/renderD*")
        if not render:
            return TestResult(self.name, Outcome.FAIL, "no /dev/dri/renderD* node")
        details = []
        if self.have_cmd("glxinfo") or self.have_cmd("eglinfo"):
            details.append("gl tools present")
        return TestResult(self.name, Outcome.PASS, f"render node present", details)


class StorageTest(HardwareTest):
    name = "storage"
    title = "Storage / UFS"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        blocks = [b for b in self.glob("/sys/block/*")
                  if any(x in b for x in ("mmcblk", "sd", "ufs", "nvme"))]
        return TestResult(self.name, Outcome.PASS if blocks else Outcome.FAIL,
                          f"{len(blocks)} block device(s)")


class UsbTest(HardwareTest):
    name = "usb"
    title = "USB"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        udc = self.glob("/sys/class/udc/*")
        if udc:
            return TestResult(self.name, Outcome.PASS, f"UDC: {[u.split('/')[-1] for u in udc]}")
        return TestResult(self.name, Outcome.WARN, "no USB device controller found")


class WifiTest(HardwareTest):
    name = "wifi"
    title = "WiFi"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        wifis = self.glob("/sys/class/net/wl*")
        return TestResult(self.name, Outcome.PASS if wifis else Outcome.FAIL,
                          f"{len(wifis)} wireless interface(s)")


class BluetoothTest(HardwareTest):
    name = "bluetooth"
    title = "Bluetooth"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        hci = self.glob("/sys/class/bluetooth/hci*")
        return TestResult(self.name, Outcome.PASS if hci else Outcome.FAIL,
                          f"{len(hci)} HCI controller(s)")


class AudioTest(HardwareTest):
    name = "audio"
    title = "Audio"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        cards = [c for c in self.glob("/proc/asound/card*") if c[-1].isdigit()]
        return TestResult(self.name, Outcome.PASS if cards else Outcome.FAIL,
                          f"{len(cards)} sound card(s)")


class BatteryTest(HardwareTest):
    name = "battery"
    title = "Battery"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        for ps in self.glob("/sys/class/power_supply/*/type"):
            if (self.read(ps) or "") == "Battery":
                cap = self.read(ps.replace("/type", "/capacity"))
                return TestResult(self.name, Outcome.PASS, f"battery at {cap}%")
        return TestResult(self.name, Outcome.FAIL, "no battery power supply")


class ChargingTest(HardwareTest):
    name = "charging"
    title = "Charging"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        for ps in self.glob("/sys/class/power_supply/*/type"):
            t = self.read(ps) or ""
            if t in ("Mains", "USB"):
                return TestResult(self.name, Outcome.PASS, f"charger supply present ({t})")
        return TestResult(self.name, Outcome.WARN, "no mains/USB supply visible")


class ModemTest(HardwareTest):
    name = "modem"
    title = "Modem"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        if self.have_cmd("mmcli"):
            return TestResult(self.name, Outcome.PASS, "ModemManager present (use `mmcli -L`)")
        wwan = self.glob("/sys/class/wwan/*") or self.glob("/dev/wwan*")
        if wwan:
            return TestResult(self.name, Outcome.PASS, "wwan device present")
        return TestResult(self.name, Outcome.WARN, "no modem tooling/device found")


class GnssTest(HardwareTest):
    name = "gnss"
    title = "GNSS"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        gnss = self.glob("/dev/gnss*")
        # Honor the definition: standalone GNSS may be unsafe on some devices.
        feat = device.feature("gnss")
        if feat and feat.status == "partial":
            return TestResult(self.name, Outcome.WARN,
                              "GNSS partial per definition; standalone may be unsafe")
        return TestResult(self.name, Outcome.PASS if gnss else Outcome.WARN,
                          f"{len(gnss)} gnss node(s)")


class NfcTest(HardwareTest):
    name = "nfc"
    title = "NFC"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        if self.have_cmd("nfc-list") or self.glob("/sys/class/nfc/*"):
            return TestResult(self.name, Outcome.PASS, "NFC subsystem present")
        return TestResult(self.name, Outcome.WARN, "no NFC device")


class SensorsTest(HardwareTest):
    name = "sensors"
    title = "Sensors"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        iio = self.glob("/sys/bus/iio/devices/iio:device*")
        return TestResult(self.name, Outcome.PASS if iio else Outcome.WARN,
                          f"{len(iio)} IIO device(s)")


class CameraTest(HardwareTest):
    name = "camera"
    title = "Camera"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        feat = device.feature("camera")
        if feat and feat.status in ("broken", "unsupported"):
            return TestResult(self.name, Outcome.WARN,
                              "camera not functional per definition (mainline HAL limitation)")
        v4l = self.glob("/dev/video*")
        media = self.glob("/dev/media*")
        if v4l or media:
            return TestResult(self.name, Outcome.PASS,
                              f"{len(v4l)} video + {len(media)} media node(s)")
        return TestResult(self.name, Outcome.WARN, "no V4L2/media device")


class FingerprintTest(HardwareTest):
    name = "fingerprint"
    title = "Fingerprint"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        feat = device.feature("fingerprint")
        if feat and feat.status == "not-present":
            return TestResult(self.name, Outcome.SKIP, "no fingerprint hardware")
        # libfprint devices are hard to probe generically.
        return TestResult(self.name, Outcome.WARN, "fingerprint probing not automated")


class VibratorTest(HardwareTest):
    name = "vibrator"
    title = "Vibrator"

    def run(self, device, env: TestEnv) -> TestResult:
        if not env.on_device:
            return TestResult(self.name, Outcome.SKIP, "run on device")
        for name in self.glob("/sys/class/input/input*/name"):
            if "vibra" in (self.read(name) or "").lower():
                return TestResult(self.name, Outcome.PASS, "vibrator input present")
        leds = [l for l in self.glob("/sys/class/leds/*") if "vibr" in l.lower()]
        return TestResult(self.name, Outcome.PASS if leds else Outcome.WARN,
                          "vibrator led present" if leds else "no vibrator device")
