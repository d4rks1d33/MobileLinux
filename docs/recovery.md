# Recovery

> ⚠️ Recovery is *normally* possible because MobileLinux never touches the
> bootloader, but it is **not guaranteed**. The authors accept **no liability**
> for bricked devices or lost data — see [DISCLAIMER.md](../DISCLAIMER.md).

This document explains how to get a device **working again** after something goes
wrong with an update: a failed OTA, a non-booting kernel, a corrupt root
filesystem, or an update interrupted by power loss. It also explains the
**health check** that decides whether an update is committed or rolled back.

Related docs:
[ota.md](ota.md) ·
[release-process.md](release-process.md) ·
[research-ota-architecture.md](research-ota-architecture.md)

Sources: [ota/client.py](../src/mobilelinux/ota/client.py) (install/rollback),
[devices/motorola/rhodep/device.yaml](../devices/motorola/rhodep/device.yaml)
(`install.rescue`).

---

## 1. The two failure worlds: A/B vs single-rootfs

Recovery depends entirely on whether the device has **two slots** or **one**.

### A/B devices (e.g. rhodep) — automatic rollback

On an A/B device, an update is written to the **inactive** slot while the active
slot keeps running untouched ([ota.md](ota.md#3-the-core-concepts)). This is what
makes automatic recovery possible:

- If the new slot fails to boot or fails its health check, the bootloader's
  fallback logic returns to the **previous, still-intact slot**.
- No re-flash is needed for most failures — the old system is right there.

rhodep is A/B: `ota.strategy: ab`, `ota.rollback: true`
([device.yaml](../devices/motorola/rhodep/device.yaml) lines 316–319), with slot
selection via the Android `misc` metadata (`qbootctl`, `rauc-custom` backend).

### Single-rootfs devices — no automatic rollback

A single-rootfs device has only one root filesystem. There is no inactive slot to
fall back to, so an update is written **in place** and is **not atomic**. If it
fails, there is nothing to roll back to automatically — you must **re-flash** the
device using its rescue path ([§6](#6-re-flashing-via-the-rescue-path)).

The client makes this explicit: on a single-rootfs device it warns that the
update cannot roll back and **refuses to proceed without `--yes`**
([ota/client.py](../src/mobilelinux/ota/client.py) lines 180–185), and
`--rollback` is disabled (lines 210–212). See
[ota.md](ota.md#11-single-rootfs-devices-degrade-gracefully).

---

## 2. The health check

Installing an update is not the same as **confirming** it. After the device
reboots into the newly-installed system, it runs a **health check**. Only if the
system reaches a healthy state is the slot **marked good**; otherwise the device
**rolls back**. Conceptually the health check walks up the stack:

```
boot            → did the kernel + initramfs load and hand off?
systemd         → did the init system reach a running target?
network         → is basic connectivity up?
storage         → is the root (and data) storage mounted and writable?
critical services → are the essential services running?
device health check → device-specific checks (radio/display/etc.)
        │
        ├── all pass → mark slot GOOD (commit the update)
        └── any fail → ROLL BACK to the previous slot
```

This is the mechanism referenced throughout the OTA flow
([ota.md](ota.md#7-the-full-flow-build--install--confirm)) and in the client's
install path, which stages the update and notes that "a health check on next boot
marks it good, else it rolls back"
([ota/client.py](../src/mobilelinux/ota/client.py) lines 178–179). Until the
health check passes, `state.last_result` stays empty
([ota/state.py](../src/mobilelinux/ota/state.py)); after a rollback it records
that the slot was reverted.

---

## 3. Failed OTA (update did not complete or was rejected)

If `mobilelinux update` fails **before** the reboot — a bad signature, a hash
mismatch, a download error, a wrong-device/arch/min-version rejection — then
**nothing was activated**. The client refuses early
([ota.md](ota.md#10-how-the-client-refuses-a-bad-or-wrong-image)); the running
system is untouched and no recovery is needed. Fix the cause (re-fetch, correct
the manifest, use the right channel) and retry.

If the update installed but the **new slot misbehaves after reboot**, the health
check handles it: A/B devices roll back automatically ([§2](#2-the-health-check)).
You can also force a manual rollback:

```
mobilelinux update --rollback
```

`_rollback()` ([ota/client.py](../src/mobilelinux/ota/client.py) lines 209–218)
marks the current slot bad (`rauc status mark-bad`) and switches the active slot
back via the Android slot control; reboot to apply. (A/B only.)

---

## 4. Non-booting kernel

A bad kernel is the classic reason a slot never comes up.

- **A/B:** the new kernel lives in the inactive slot's boot image. If it does not
  boot, the health check never passes and the bootloader falls back to the
  previous slot's known-good kernel. Automatic.
- **Single-rootfs:** there is no alternate boot image to fall back to — you must
  re-flash a known-good boot image via the rescue path ([§6](#6-re-flashing-via-the-rescue-path)).

---

## 5. Corrupt rootfs / interrupted (power-loss) update

- **A/B:** because the update targets the **inactive** slot and only flips the
  active slot as the final atomic step, a power loss mid-write leaves the
  **active** slot untouched — the device still boots normally, and the partial
  write to the inactive slot is simply discarded and retried
  ([ota.md](ota.md#atomic-updates)). This is the whole point of atomic A/B.
- **Single-rootfs:** an interrupted in-place write **can** corrupt the running
  rootfs, because the update mutates the live filesystem. This is why the client
  refuses a single-rootfs install without an explicit `--yes` and warns to ensure
  the battery is charged ([ota/client.py](../src/mobilelinux/ota/client.py) lines
  180–185). Recovery is a re-flash via rescue.

---

## 6. Re-flashing via the rescue path

When a device cannot recover on its own (any single-rootfs failure, or an A/B
device that somehow lost both slots), you recover it the **same way you first
installed it** — through the device's **rescue** strategy. Recovery deliberately
**reuses the install/rescue flow** rather than inventing a second mechanism.

For rhodep, the rescue path is declared in `install.rescue`
([device.yaml](../devices/motorola/rhodep/device.yaml) lines 267–279):

```yaml
install:
  strategy: rescue-dd
  ...
  rescue:
    required: true
    method: pmos-debug-shell
    boot_image: build
    transport: telnet://172.16.42.1:23
    notes: >
      Rescue image = pmOS kernel+DTB+initramfs with 'pmos.debug-shell' appended.
      Brings up USB-gadget networking and a root telnet WITHOUT mounting root,
      so userdata can be dd-written. Also used for recovery.
```

Why this exists: on rhodep, `fastboot flash userdata` is **denied by the Motorola
ABL bootloader** (see the `userdata` partition note,
[device.yaml](../devices/motorola/rhodep/device.yaml) line 244). The rootfs is a
GPT disk *inside* the `userdata` partition, so it must be written with **`dd`**,
not fastboot. The rescue image solves this by:

1. Flashing a **pmOS debug-shell boot image** to the boot slot (via fastboot).
2. Booting it — it brings up **USB-gadget networking** and a **root telnet at
   `172.16.42.1:23`** *without mounting root*, so the root device is free to be
   overwritten.
3. Streaming the known-good Kali GPT disk into `userdata` with **`dd`** over that
   telnet session.
4. Flashing the distro boot image back and rebooting.

Those are exactly the `install.steps` (flash rescue → set slot → reboot →
wait-transport → `dd` userdata → flash boot → reboot,
[device.yaml](../devices/motorola/rhodep/device.yaml) lines 280–314). **Recovery
runs the same steps** — the rescue boot that installs the device is the same
rescue boot that repairs it. In the CLI this is the recovery flavor of flashing:

```
mobilelinux flash rhodep --recovery
```

(`--recovery` uses the rescue flow; see
[cli/main.py](../src/mobilelinux/cli/main.py) lines 68–70.)

---

## 7. Summary

| Failure | A/B device (rhodep) | Single-rootfs device |
|---|---|---|
| OTA rejected pre-reboot | Nothing changed; retry | Nothing changed; retry |
| New slot won't boot | Auto rollback to old slot | Re-flash via rescue |
| Bad kernel | Auto rollback | Re-flash via rescue |
| Corrupt rootfs | Active slot untouched; retry | Re-flash via rescue |
| Power loss mid-update | Inactive slot discarded; safe | Possible corruption; re-flash |
| Manual revert | `mobilelinux update --rollback` | Not available; re-flash |

The rule of thumb: **A/B devices self-heal via rollback; single-rootfs devices
recover by re-flashing through the same rescue path used to install them.** That
is why MobileLinux ties recovery to the device definition's `install.rescue`
block instead of maintaining a separate recovery subsystem.
