# Flashing a Device

`mobilelinux flash <device>` installs a built image onto a phone using the
device's declared install strategy. It is written to be **conservative**: it
refuses to write anything until it has verified the artifacts, confirmed the
connected device, shown you exactly what will change, and received explicit
confirmation.

```
mobilelinux flash rhodep
mobilelinux flash rhodep --dry-run     # preview the plan, write nothing
mobilelinux flash rhodep --recovery    # use the rescue/recovery flow
```

The install **strategy is chosen automatically** from the device definition
(`install.strategy`) — you never select it on the command line.

## The safety contract (10 rules)

Before writing any partition, `flash` enforces all ten of these:

1. **Detect the connected device** over fastboot or adb.
2. **Confirm codename/model** against the device definition.
3. **Read available partitions** (via the strategy plan).
4. **Show exactly what it will modify** — the partition list and every planned
   operation, with destructive ones marked.
5. **Verify the strategy matches the definition** — the strategy is loaded by
   name from `install.strategy`; an unknown strategy aborts.
6. **Require explicit confirmation** for destructive operations.
7. **Support `--dry-run`** — plan only, never writes.
8. **Never run another device's commands** — if the connected device does not
   match the requested one, it aborts (`SafetyAbort`) rather than flashing the
   wrong device.
9. **Validate artifact hashes before writing** — it loads `artifacts.json` and
   verifies every file's sha256; verification failure refuses the flash.
10. **Offer the rescue/recovery flow** where the device defines one
    (`install.rescue`), and via `--recovery`.

If no build artifacts exist, `flash` stops and tells you to run
`mobilelinux build <device> --distro <distro>` first.

If some steps are skipped because their tool is missing, `flash` reports the
missing tools and exits with a distinct status so you can install them and
re-run.

### The WARNING confirmation block

Before any write, `flash` prints a WARNING block and asks for confirmation. It
looks like this:

```
WARNING

Device detected:
  Motorola Moto G82 5G (rhodep)

This operation will modify:
  boot_a
  userdata

Installation strategy:
  rescue-dd

Continue? [y/N]
```

- Under `--dry-run` it prints `[dry-run] no partitions will be written` and
  proceeds only to show the plan (nothing is written).
- With `--yes` it proceeds without prompting (dangerous).
- With no TTY and no `--yes`, it **aborts** rather than run unattended.

## Detecting a device

```
mobilelinux detect
```

`detect` probes the connected device over fastboot (and adb), reports what it
found — for fastboot: serial, product, A/B, fastbootd, active slot; for adb:
manufacturer, model, codename — and matches it against the registry to show the
device definition and the **install strategy that would be used**. If neither
`fastboot` nor `adb` is installed, it says so and reports the missing tools. If
nothing is connected, it tells you to enter fastboot/bootloader mode or enable
USB debugging.

`flash` uses the same detection internally (rule 1). If the transport gives no
identifying information (a bare fastboot with no product string), `flash` does
not hard-fail — you selected the device explicitly — but a *contradicting*
identity aborts (rule 8).

## Strategies (chosen automatically)

MobileLinux implements several install strategies. The device definition picks
one; the difference between devices is *data*, not forked scripts.

| Strategy | Used when | Notes |
|----------|-----------|-------|
| `rescue-dd` | Bootloader refuses to flash the rootfs partition and there is no fastbootd (e.g. `rhodep`) | Rescue-boot + `dd` over telnet/ssh. |
| `fastboot` | Bootloader fastboot can write the rootfs partition directly (e.g. OnePlus 6) | Plain `fastboot flash`. |
| `fastbootd` | Writing logical/dynamic (super) partitions needs userspace fastboot (e.g. Pixel 3a) | Reboots into fastbootd first. |
| `heimdall` | Samsung download mode (e.g. Galaxy S III) | `heimdall flash --<part>`; no A/B slots. |
| `sdcard` | Whole-disk image to SD/eMMC (e.g. PinePhone) | Host-side write to a target you must specify; refuses to guess. |
| `uuu` | NXP i.MX serial download (e.g. Librem 5) | `uuu` writes eMMC. |
| `adb-shell-dd` | Write a partition via `adb shell dd` from a booted Linux/recovery | For devices reachable over adb. |

## Walkthrough: the `rhodep` rescue-dd flow

`rhodep` (Motorola Moto G82 5G) cannot have its rootfs written by fastboot —
`fastboot flash userdata` is **denied by the Motorola ABL bootloader**, and the
device has no fastbootd. So its definition uses the `rescue-dd` strategy, whose
plan (from `install.steps`) runs end to end as follows:

1. **Message.** Notes that `userdata` cannot be written by fastboot on this
   device.
2. **Flash the rescue image to the boot slot** (`fastboot flash boot_a`,
   destructive). The rescue image is a pmOS kernel+DTB+initramfs with
   `pmos.debug-shell` appended.
3. **Set active slot `a`** (`fastboot --set-active=a`).
4. **Reboot** (`fastboot reboot`) into the rescue image. It brings up USB-gadget
   networking and a **root telnet debug shell** at `telnet://172.16.42.1:23`,
   **without mounting the root filesystem**.
5. **Wait for the rescue transport** (the telnet at `172.16.42.1:23`).
6. **Stream the Kali GPT disk into `userdata` with `dd`.** Because telnet can't
   stream binary reliably, the rescue shell opens a netcat listener and the host
   pipes the image into it; inside the rescue env this is
   `dd of=/dev/disk/by-partlabel/userdata bs=4M conv=fsync` (destructive). The
   target device path comes from `storage.partitions` (`userdata` →
   `/dev/disk/by-partlabel/userdata`).
7. **Flash the distro boot image** (`fastboot flash boot_a`, destructive).
8. **Set active slot `a`** again (`fastboot --set-active=a`).
9. **Reboot** (`fastboot reboot`).
10. **Message.** First boot resizes the rootfs and starts the desktop.

The same rescue environment is reused for **recovery** — pass `--recovery`, or
see [recovery.md](recovery.md).

> Note: on `rhodep`, `userdata` being un-flashable by fastboot is **by design**,
> not a bug. See [troubleshooting.md](troubleshooting.md).

## See also

- [install.md](install.md) — the full user path
- [build.md](build.md) — producing the artifacts flash verifies
- [recovery.md](recovery.md) — the rescue flow when a device won't boot
- [troubleshooting.md](troubleshooting.md) — device-mismatch aborts, missing
  tools, verification failures
