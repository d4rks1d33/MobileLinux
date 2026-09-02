# Troubleshooting

Practical fixes for common problems. If the device won't boot at all, jump to
[recovery.md](recovery.md).

## "no device detected"

`mobilelinux detect` (and `flash`) probe over `fastboot` and `adb`.

- **Neither tool installed.** `detect` says so and reports the missing tools —
  install `fastboot`/`adb` and re-run.
- **Nothing connected / wrong mode.** Put the phone in **fastboot/bootloader
  mode**, or enable **USB debugging** for adb. Check the USB cable/port and,
  on Linux, your udev permissions.
- **Wrong mode for the strategy.** Each strategy expects a specific mode (e.g.
  fastbootd for logical partitions, download mode for Heimdall). See the
  strategy table in [flash.md](flash.md).

## "tool missing" messages

Build/flash steps that need an absent tool are **skipped**, and the missing tool
is reported with an install hint. This is normal and safe. **Install the tools
it names and re-run.** Nothing destructive runs just because a tool is present —
heavy/dangerous operations still require `--execute` (and `--allow-dangerous`).
See the execution model in [build.md](build.md).

## "artifact verification failed"

Before flashing, `flash` verifies every artifact's sha256 against
`artifacts.json`. If a file is missing or its hash doesn't match, it **refuses
to flash** and lists the problems. **Rebuild** the image:

```
mobilelinux build <device> --distro <distro>
```

Then flash again. A verification failure usually means an interrupted or partial
build, or a modified/corrupted artifact.

## Device mismatch abort

If the connected device does not match the one you asked to flash, `flash`
aborts with a `SafetyAbort` — it will never run another device's commands
(safety rule 8). Fixes:

- connect the correct device, or
- run `mobilelinux detect` to see what's actually attached and which definition
  it matches.

Note a *bare* fastboot with no identifying product string does not hard-fail
(you selected the device explicitly); only a **contradicting** identity aborts.

## "signature invalid" on update

OTA update metadata is signed. If an update reports an invalid signature, the
client refuses it — do not force it. Re-check the channel and that you have the
correct signing key/metadata for that channel, then retry the update. (See the
OTA docs referenced from [architecture.md](architecture.md).)

## Boot loops / won't boot

If the device boot-loops or hangs after flashing, use the rescue/recovery flow —
`mobilelinux flash <device> --recovery` where the device defines a rescue
environment — and follow [recovery.md](recovery.md). On `rhodep` the same
rescue-dd environment used to install is reused for recovery.

## rhodep-specific issues

### `userdata` can't be fastboot-flashed — by design

On `rhodep` the Motorola ABL bootloader **denies** `fastboot flash userdata`,
and there is no fastbootd. This is expected. The device therefore uses the
`rescue-dd` strategy: it boots a rescue image, opens a root telnet debug shell,
and `dd`s the rootfs into `userdata`. Do not try to force a fastboot write — use
`mobilelinux flash rhodep` and let it drive the rescue-dd flow (see
[flash.md](flash.md)).

### `droid-juicer` hanging boot

`droid-juicer.service` extracts firmware from Android partitions and **hangs
boot** on a device without Android. It is **masked** during the chroot
integration phase (`first_boot.mask_services` includes `droid-juicer.service`),
along with `systemd-repart.service` (which fails on the gpt-in-partition
layout). If you see a boot hang tied to droid-juicer, confirm the mask is in
place — a build that skipped the integration/masking step will hang here.

### `ipa` / LTE reset

**LTE/mobile data is broken on `rhodep`, on purpose.** With `ipa.ko` loaded and
an LTE attach, the SoC watchdog-resets within 3–10 minutes, so `ipa.ko` is
**blacklisted at boot** by the `rhodep-ipa-hold` package. The trade-off is no
mobile data and no ModemManager LTE. GSM/2G voice + SMS + GPRS work and are
stable. Do not unblock `ipa.ko` expecting working LTE — it will reset the SoC.

Related `rhodep` "working as intended" limitations you may hit:

- **Camera** is `broken` on mainline (stock blobs are Android HALs); the
  `camera` test warns accordingly.
- **Standalone GNSS** watchdog-resets the SoC in under a second; only cell-id
  positioning is safe (`gnss` is `partial`).
- **No in-call audio** (no `q6voice` in mainline).
- **WiFi monitor/injection** isn't feasible on the internal WCN3990 — use a USB
  WiFi adapter.

Run `mobilelinux check rhodep` for the full, evidence-based status list, and
`mobilelinux test rhodep` on the device to probe the real hardware (see
[testing.md](testing.md)).

## See also

- [install.md](install.md) — the intended install path
- [flash.md](flash.md) — safety contract and strategies
- [build.md](build.md) — tool detection and execution model
- [testing.md](testing.md) — `check` vs `test`
- [recovery.md](recovery.md) — recovering an unbootable device
