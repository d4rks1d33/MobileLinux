# Hardware Testing

MobileLinux has two distinct ways to answer "does this hardware work?":

- **`mobilelinux check <device>`** — reads the *declared* support status from the
  device definition and prints an objective, evidence-based support percentage.
  It does not touch hardware.
- **`mobilelinux test <device>`** — **probes the real hardware** on the device
  (sysfs, DRM, ALSA, iio, qrtr, …) and reports pass/fail/warn/skip per feature.

Use `check` to see what a definition *claims*; use `test` to verify it *on the
phone*.

## `check`: declared status and support %

```
mobilelinux check rhodep
```

`check` prints structural rows (kernel, device tree, boot) and one row per
hardware feature, using the status declared in the definition
(`supported` / `partial` / `broken` / `untested` / `not-present`). It then
computes an overall support percentage as a **weighted score over the declared
statuses** — the number is derived from the schema, never invented, and
`not-present` features are excluded. It finishes with a legend and honesty
notes: `broken` hardware is listed explicitly, and `untested` features are
called out as *not counted as supported* (no evidence).

This is the evidence-based view: it reflects exactly what the definition
records, with the `evidence:` fields behind each status.

## `test`: probing real hardware

```
mobilelinux test rhodep
mobilelinux test rhodep --only wifi,bluetooth   # subset
```

`test` runs the modular hardware test suite. Each module probes the running
system for **positive evidence** a feature works and returns one of:

- **pass** — positive evidence found;
- **warn** — ambiguous/partial evidence;
- **fail** — clear absence;
- **skip** — cannot run here (e.g. off-device).

On-device it exits non-zero if any test **fails**; off-device it always exits
cleanly (everything skips). A summary line totals pass/warn/fail/skip.

### Off-device: tests skip with "run on device"

Most probes only make sense on the target hardware, so when you are **not** on
the device they report `skip (run on device)` rather than guessing. `test`
prints a reminder to copy `mobilelinux` to the phone and run it there for real
results.

### On-device detection

The runner decides it is "on device" by matching the machine's Device Tree
model / compatible against the device's codename or SoC family. It reads:

- `/sys/firmware/devicetree/base/model`
- `/sys/firmware/devicetree/base/compatible`
- `/proc/device-tree/model`

If any of those contains the device's `codename` or `soc.family`, the tests run
for real; otherwise they skip.

## The test modules

The suite is **modular** — one module per feature. There are **18** generic test
modules:

`boot`, `display`, `touch`, `gpu`, `storage`, `usb`, `wifi`, `bluetooth`,
`audio`, `battery`, `charging`, `modem`, `gnss`, `nfc`, `sensors`, `camera`,
`fingerprint`, `vibrator`.

What each probes (summarized):

| Test | Probe | pass / warn / fail |
|------|-------|--------------------|
| `boot` | `/proc/sys/kernel/osrelease` | pass if running; warn if version differs from the definition |
| `display` | `/sys/class/drm/*` connectors | pass on a connected connector; warn if DRM present but none connected; fail if no DRM |
| `touch` | `/sys/class/input/*/name` (touch/goodix/focaltech) | pass on a touchscreen input; fail otherwise |
| `gpu` | `/dev/dri/renderD*` | pass on a render node (notes GL tools); fail if none |
| `storage` | `/sys/block/*` (mmcblk/sd/ufs/nvme) | pass on block devices; fail if none |
| `usb` | `/sys/class/udc/*` | pass on a UDC; warn if none |
| `wifi` | `/sys/class/net/wl*` | pass on a wireless iface; fail if none |
| `bluetooth` | `/sys/class/bluetooth/hci*` | pass on an HCI controller; fail if none |
| `audio` | `/proc/asound/card*` | pass on a sound card; fail if none |
| `battery` | `/sys/class/power_supply/*` type Battery | pass with capacity; fail if none |
| `charging` | power supply type Mains/USB | pass if present; warn if none |
| `modem` | `mmcli` / `/sys/class/wwan/*` / `/dev/wwan*` | pass on ModemManager or wwan; warn if none |
| `gnss` | `/dev/gnss*` (+ definition status) | warn if the definition marks GNSS `partial`; pass/warn otherwise |
| `nfc` | `nfc-list` / `/sys/class/nfc/*` | pass if NFC subsystem present; warn if none |
| `sensors` | `/sys/bus/iio/devices/iio:device*` | pass on IIO devices; warn if none |
| `camera` | `/dev/video*`, `/dev/media*` (+ definition status) | warn if the definition marks camera `broken`; pass/warn otherwise |
| `fingerprint` | definition status | skip if `not-present`; warn (not automated) otherwise |
| `vibrator` | input `vibra*` / `/sys/class/leds/*vibr*` | pass if a vibrator device is found; warn if none |

Some modules deliberately **honor the definition**: e.g. `gnss` and `camera`
downgrade to `warn` when the definition already records them as `partial` or
`broken`, so the test never over-claims (on `rhodep`, standalone GNSS
watchdog-resets the SoC, and the camera is broken on mainline).

## How devices declare which tests apply, and the evidence rule

A device lists the tests that apply in its definition under `tests:`, and each
hardware feature can name the test that verifies it via `hardware.<feature>.test`.
For example, `rhodep` declares 16 tests (`boot`, `display`, `touch`, `gpu`,
`storage`, `usb`, `wifi`, `bluetooth`, `audio`, `battery`, `charging`, `modem`,
`gnss`, `nfc`, `sensors`, `vibrator`) and points `hardware.wifi.test: wifi`,
`hardware.touchscreen.test: touch`, and so on. When `--only` is not given,
`test` runs the device's declared `tests:` list (falling back to all modules if
a device declares none).

This closes the loop with `check`: a **passing on-device test is the evidence**
that justifies marking a feature `supported`. The definition records the
`evidence:` for each status, the `hardware.<f>.test` names how to verify it, and
`test` produces the objective result on real hardware. `check` then reflects
those declared, evidence-backed statuses in the support percentage.

## See also

- [install.md](install.md) — includes running `check` before install
- [build.md](build.md) — building the image you then test on-device
- [troubleshooting.md](troubleshooting.md) — interpreting failures
