# Installing MobileLinux on a Supported Device

> ⚠️ **At your own risk.** Installing replaces the OS and erases user data, and
> flashing carries a small risk of leaving a device unbootable. The authors
> accept **no liability** — see [DISCLAIMER.md](../DISCLAIMER.md). Recovery is
> *normally* possible ([recovery.md](recovery.md)) because the bootloader is
> never touched, but not guaranteed. **Back up everything first.**

This guide covers the end-to-end path for a **user** installing MobileLinux on a
device that already has a definition in the tree: check it is supported, build
the images, flash, and boot.

If you are porting a *new* device, this is not that guide — start from
[architecture.md](architecture.md).

## Prerequisites

- The `mobilelinux` CLI (run from a checkout of this repository).
- A Linux host with USB access to the phone.
- The USB flashing tool your device uses. The framework will tell you which one
  is missing when you run a command (see *Tool detection* below). For most
  Android-based devices this is `fastboot` and/or `adb`.
- An **unlocked bootloader** where the device requires it (e.g. `rhodep` has
  `install.unlock_required: true`).
- The proprietary firmware your device needs, where it is not redistributable
  (extracted from the stock device — never committed to git).

MobileLinux never runs heavy or destructive steps by accident. By default every
command only **plans** (prints what it would do). You opt in to real execution
with `--execute`, and to touching real block/loop devices with
`--allow-dangerous`. See *Tool detection & execution model* below.

## 1. Confirm the device is supported

List everything registered:

```
mobilelinux list-devices
```

Show the full definition for one device:

```
mobilelinux device-info rhodep
```

Get the objective, evidence-based hardware-support report and overall support
percentage:

```
mobilelinux check rhodep
```

`check` reads the *declared* status of each hardware feature from the device
definition and prints a weighted support percentage. It does **not** touch the
phone — it reports what the definition claims, with evidence. To probe the real
hardware on the device itself, use `mobilelinux test` (see
[testing.md](testing.md)).

`check` also warns about `broken` features (present but non-working/unsafe) and
lists `untested` ones. Read these carefully before you rely on the device — for
example, on `rhodep` the camera is `broken` and LTE/mobile-data is broken by
design (see [troubleshooting.md](troubleshooting.md)).

## 2. Build the images

```
mobilelinux build rhodep --distro kali
```

This runs the build pipeline (kernel → rootfs → images → `artifacts.json`).
Optional flags select a desktop and a distro profile:

```
mobilelinux build rhodep --distro kali --desktop phosh --profile security
```

By default this is a **plan** (nothing runs). Add `--execute` (and, for steps
that partition/loop-mount real devices, `--allow-dangerous`) to produce real
artifacts. See [build.md](build.md) for the full pipeline, the tools you must
install, and why the heavy tools are gated.

## 3. Flash the device

```
mobilelinux flash rhodep
```

`flash` is deliberately conservative: it verifies the build artifacts' hashes,
detects the connected device, confirms it matches the definition, shows exactly
which partitions it will modify, and requires explicit confirmation before
writing anything. The install **strategy is chosen automatically** from the
device definition (`install.strategy`).

Preview without any risk:

```
mobilelinux flash rhodep --dry-run
```

Use the rescue/recovery flow (where the device supports one):

```
mobilelinux flash rhodep --recovery
```

The full safety contract, the WARNING confirmation block, and a step-by-step
walkthrough of the `rhodep` rescue-dd flow are in [flash.md](flash.md).

## 4. First boot

On first boot the device runs whatever `first_boot` behavior its definition
declares. For `rhodep`:

- the root filesystem is resized to fill the target partition
  (`first_boot.resize_rootfs: true`);
- services that would hang or fail on a mainline Linux boot are masked (e.g.
  `droid-juicer.service`, `systemd-repart.service`).

After that the desktop starts. If the device does not boot, or boot-loops, see
[recovery.md](recovery.md) and [troubleshooting.md](troubleshooting.md).

## Tool detection & execution model

External build/flash tools are heavy and often absent, so the framework detects
them and refuses to guess:

- **Default (plan):** commands are printed, nothing runs.
- **`--dry-run`:** prints every command, runs nothing.
- **`--execute`:** runs commands whose tool is present.
- **`--allow-dangerous`:** additionally permits operations that touch real
  block/loop devices (`losetup`, `mkfs`, partitioning, `pmbootstrap`). Implies
  `--execute`.

If a required tool is missing, the step is skipped and the missing tool is
reported with an install hint. **Install the tools it names and re-run** — this
is why merely having `losetup`/`pmbootstrap` installed can never trigger an
accidental destructive run.

Other useful global flags: `--yes` (assume yes to confirmations — dangerous),
`-q/--quiet`, and `--repo` (point at a specific repository checkout). Global
flags may appear before or after the subcommand.

## See also

- [build.md](build.md) — building images in detail
- [flash.md](flash.md) — the flash safety contract and per-strategy details
- [testing.md](testing.md) — the on-device hardware test suite
- [troubleshooting.md](troubleshooting.md) — common problems
- [recovery.md](recovery.md) — when the device won't boot
