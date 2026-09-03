# Kali Linux for the Motorola Moto G82 5G (`rhodep`)

A ready-to-flash **Kali Linux Rolling + Phosh** build on a **mainline Linux
7.2-rc5** kernel for the **Motorola Moto G82 5G** (codename `rhodep`, Qualcomm
SM6375), produced by [MobileLinux](https://github.com/d4rks1d33/MobileLinux).

> ⚠️ **READ THE DISCLAIMER AT THE BOTTOM BEFORE FLASHING. YOU DO THIS AT YOUR
> OWN RISK.**

## What you get

| File | Where to get it | What it is |
|------|-----------------|------------|
| `rescue.img` | **Release asset** | Rescue boot image (pmOS initramfs + debug shell). Boots a root shell over USB networking **without** mounting the system, used to write `userdata`. Reusable for recovery. |
| `kali-boot.img` | **Release asset** | The boot image (mainline kernel + pmOS initramfs). Flashed to `boot_a`. |
| `kali-userdata.img` | **Repo (Git LFS)** — see below | The full Kali system as a GPT disk (written to the `userdata` partition). |
| `CHANGELOG.md` / `CHECKSUMS.sha256` | Release asset / repo | Notes + checksums. |

> **`kali-userdata.img` is downloaded separately.** It is several GB and is
> **not** attached to the GitHub Release. Get it from the repository at
> **`release/rhodep/kali-userdata.img`**:
>
> - Direct download:
>   `https://github.com/d4rks1d33/MobileLinux/raw/main/release/rhodep/kali-userdata.img`
> - Or clone with Git LFS: `git lfs install && git clone https://github.com/d4rks1d33/MobileLinux`
>   then find it in `release/rhodep/`.

Default login after boot: **`kali` / `1234`** (change it immediately).

## Why it installs this way

The Motorola bootloader **refuses `fastboot flash userdata`** (permission
denied) and has no `fastbootd`. So the Kali system disk is written to
`userdata` with **`dd`** from a small rescue environment that we boot first.
`boot_a` is then flashed normally with fastboot. This is the same method
postmarketOS uses on this device.

## Requirements

- An **unlocked bootloader** (unlocking wipes the device — back up first).
- `fastboot` on your computer (`apt install android-sdk-platform-tools`, or the
  Android platform-tools).
- `telnet` and `nc` (netcat) on your computer.
- A USB-C cable and a charged phone.
- The three images in one folder: `rescue.img` and `kali-boot.img` from the
  **Release assets**, and `kali-userdata.img` from the **repo** (see above).
- Verify your downloads: `sha256sum -c CHECKSUMS.sha256`.

## Install — step by step

### 1. Boot the rescue image

Put the phone in fastboot/bootloader mode (power off, then hold **Volume Down +
Power**), connect USB, and run:

```bash
fastboot flash boot_a rescue.img
fastboot --set-active=a
fastboot reboot
```

The phone reboots into the rescue environment. It brings up a USB network at
**172.16.42.1** with a root shell on telnet (it does **not** mount your system).

### 2. Write the Kali system to `userdata`

Open **two terminals**.

**Terminal A — connect to the phone's rescue shell** and start a listener that
writes whatever it receives straight into the `userdata` partition:

```bash
telnet 172.16.42.1 23
# once you're in the rescue root shell, run:
nc -l -p 5555 | dd of=/dev/disk/by-partlabel/userdata bs=4M conv=fsync
```

**Terminal B — on your computer**, stream the image into that listener:

```bash
nc 172.16.42.1 5555 < kali-userdata.img
```

Wait for the transfer to finish (it's several GB — this takes a while). When
`dd` reports it's done in Terminal A, the system disk is written.

> Tip: if your `nc` doesn't support `-l -p`, use `nc -l 5555` on the phone and
> `nc 172.16.42.1 5555 < kali-userdata.img` on the host, or use SSH if the
> rescue image offers it.

### 3. Flash the Kali boot image

Reboot to the bootloader (`Volume Down + Power`, or from the rescue shell:
`reboot bootloader`) and flash the real boot image:

```bash
fastboot flash boot_a kali-boot.img
fastboot --set-active=a
fastboot reboot
```

### 4. First boot

- The phone **vibrates before switching to the real system** — that means the
  initramfs found and mounted the rootfs. Good sign.
- The **first boot is slow**: the initramfs grows the filesystem with
  `resize2fs` and systemd + Phosh start for the first time. **A black screen or
  stuck logo for a few minutes on the first boot is normal.** Be patient.
- You can confirm it's alive over USB: `ssh kali@172.16.42.1` (password `1234`).

Then Phosh comes up. Log in as **`kali` / `1234`** and change the password.

## Vendor firmware (Wi-Fi / Bluetooth / GPU / audio)

Some firmware is **proprietary and not redistributable**, so it is **not**
included. Until you extract it from your own device, the affected peripherals
(internal Wi-Fi, Bluetooth, GPU zap shader, audio amplifiers) won't initialize.
The build's `FIRMWARE.md` lists exactly which blobs to extract and where to put
them.

## Troubleshooting

**Black screen after the Motorola logo, and `172.16.42.1` refuses SSH/telnet.**
Almost always one of:

1. **You flashed `kali-boot.img` but not `kali-userdata.img`** (or an old
   userdata is still there). The boot image looks for the rootfs by UUID; if
   that exact system disk isn't on `userdata`, it can't mount root and never
   finishes booting. Write `kali-userdata.img` (step 2) with this exact release.
2. It's still the **first boot** doing `resize2fs` — wait a few minutes (the
   phone vibrated = root was found).
3. `droid-juicer` got re-enabled by an update and is hanging the desktop. From
   SSH: `sudo systemctl mask droid-juicer.service systemd-repart.service && sudo reboot`.

**Recovering a bricked/boot-looping phone.** Re-flash `rescue.img` (step 1),
then re-write `userdata` (step 2) and re-flash `kali-boot.img` (step 3). Because
this never touches the bootloader, the rescue path is normally available.

---

## Disclaimer

**You flash this entirely AT YOUR OWN RISK.** The authors and contributors
accept **NO responsibility and NO liability** for damaged, bricked or unusable
devices, lost or wiped data, voided warranties, or any other harm arising from
using these images. This software is provided **"AS IS", without warranty of
any kind**.

By design MobileLinux does **not** modify the bootloader and only writes the
`boot_a` and `userdata` partitions, so recovery via the rescue image is
*normally* possible — but **no outcome is guaranteed**. Hardware faults, power
loss at the wrong moment, wrong images, vendor anti-rollback, or plain bad luck
can still leave a device you cannot recover. **Back up everything first, and do
not flash a device you cannot afford to lose.**

The security/pentest tooling included in Kali/NetHunter must only be used on
systems you own or are explicitly authorized to test.

See also the repository's [DISCLAIMER.md](https://github.com/d4rks1d33/MobileLinux/blob/main/DISCLAIMER.md).
