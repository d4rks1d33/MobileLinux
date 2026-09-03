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
| `kali-userdata.img.xz` | **Repo (Git LFS)** — see below | The full Kali system as a GPT disk (xz-compressed, ~1.6 GB). Decompresses to ~8 GB. |
| `CHANGELOG.md` / `CHECKSUMS.sha256` | Release asset / repo | Notes + checksums. |

> **`kali-userdata.img.xz` is downloaded separately.** It is ~1.6 GB and is
> **not** attached to the GitHub Release. Get it from the repository at
> **`release/rhodep/kali-userdata.img.xz`** (stored with Git LFS):
>
> - Direct download:
>   `https://github.com/d4rks1d33/MobileLinux/raw/main/release/rhodep/kali-userdata.img.xz`
> - Or clone with Git LFS: `git lfs install && git clone https://github.com/d4rks1d33/MobileLinux`
>   then find it in `release/rhodep/`.
>
> You do **not** need to decompress it manually — the flashing step below pipes
> it through `xz` on the fly.

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
- The files in one folder: `rescue.img` and `kali-boot.img` from the
  **Release assets**, and `kali-userdata.img.xz` from the **repo** (see above).
- `xz` on your computer (`apt install xz-utils`) to decompress the system image.
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

**Terminal B — on your computer**, decompress and stream the image into that
listener (the image is xz-compressed; `xz -dc` decompresses on the fly so you
never need ~8 GB of free disk on your PC):

```bash
xz -dc kali-userdata.img.xz | nc 172.16.42.1 5555
```

Wait for the transfer to finish (the decompressed stream is ~8 GB — this takes a
while). When `dd` reports it's done in Terminal A, the system disk is written.

> If you'd rather decompress first: `xz -dk kali-userdata.img.xz` (needs ~8 GB
> free), then `nc 172.16.42.1 5555 < kali-userdata.img`.

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

Some firmware for this phone is **proprietary and not redistributable**, so it
is **not** included in these images. The good news: **most of it is extracted
automatically from your own phone**, because the stock partitions
(`modem_a`, `super`/`vendor`, `fsg_a`, `persist`) are still there — the Kali
system only lives in `userdata`. Only a couple of things need a manual step.

Do all of this **on the phone** (over SSH `kali@172.16.42.1`, password `1234`,
then `sudo su`), after the first boot has finished.

### What happens automatically (nothing to do)

On first boot the shipped device packages copy these from your phone's own
partitions — you don't extract anything:

- **Modem / ADSP / CDSP firmware** — served read-only from `modem_a`
  (`rhodep-modem-support` mounts it at `/readonly/firmware` and symlinks the
  `.mbn` files the kernel expects).
- **Modem remote-filesystem, carrier config, RF calibration** — copied from
  `modem_a`, `fsg_a` and `persist` by `rhodep-rfs-populate`.
- **Sensor registry** (auto-rotation) — copied from `vendor` + `persist` by
  `rhodep-ssc-populate`.

### 1. Wi-Fi / Bluetooth / GPU firmware (manual — the `firmware-motorola-rhodep` package)

These blobs (WCN3990 Wi-Fi/BT, Adreno GPU) live in the stock **`vendor`**
partition (a logical partition inside `super`). Extract them once, drop them
into the firmware package tree under the exact paths, build the `.deb`, and
install it:

Required files (install under `/usr/lib/firmware/`):

| File | For |
|------|-----|
| `ath10k/WCN3990/hw1.0/board-2.bin`, `firmware-5.bin` | internal Wi-Fi |
| `qca/crbtfw21.tlv`, `qca/crnv21.bin` | internal Bluetooth |
| `qcom/a619_gmu.bin`, `qcom/a630_sqe.fw`, `qcom/sm6375/motorola/rhodep/a615_zap.mdt` (+`.b00 .b01 .b02`) | GPU (Adreno 619) |
| `qcom/sm6375/motorola/rhodep/{adspr,adsps,adspua,cdspr,modemr,modemuw}.jsn`, `wlanmdsp.mbn` | protection domains (Wi-Fi PD lives inside the modem) |

How to get them and install:

```bash
# On the phone, as root. Map the stock 'vendor' partition (inside super) and
# mount it read-only. (These sector numbers are for rhodep's stock layout.)
dmsetup create vendor_ro --table "0 1235440 linear /dev/disk/by-partlabel/super 6789120"
mkdir -p /mnt/vendor_ro && mount -o ro /dev/mapper/vendor_ro /mnt/vendor_ro

# The blobs are under /mnt/vendor_ro/firmware and /mnt/vendor_ro/bt_firmware.
# Copy them into a firmware package tree using the paths in the table above, e.g.:
mkdir -p PKG/usr/lib/firmware/ath10k/WCN3990/hw1.0
cp /mnt/vendor_ro/firmware/ath10k/WCN3990/hw1.0/{board-2.bin,firmware-5.bin} \
   PKG/usr/lib/firmware/ath10k/WCN3990/hw1.0/
#   ...repeat for the qca/, qcom/... files in the table...

umount /mnt/vendor_ro && dmsetup remove vendor_ro

# Simplest install: copy straight into place
cp -r PKG/usr/lib/firmware/* /usr/lib/firmware/
depmod -a; update-initramfs -u 2>/dev/null || true

# IMPORTANT: stop apt from replacing these device-specific blobs with the
# generic firmware-atheros package (which would break Wi-Fi/BT):
apt-mark hold firmware-atheros

reboot
```

> The framework can also package this as a proper `firmware-motorola-rhodep.deb`
> — the build writes a `FIRMWARE.md` next to the images listing the exact paths.
> The manual copy above is the quickest route on-device.

### 2. Audio (manual — the AW88261 amplifier tuning blob)

The speaker/earpiece amplifiers won't initialize without their tuning file
(`aw88261_acf.bin`), which lives in the stock `vendor` partition. On the phone,
as root:

```bash
# Map + mount the stock vendor partition (same as above)
dmsetup create vendor_ro --table "0 1235440 linear /dev/disk/by-partlabel/super 6789120"
mkdir -p /mnt/vendor_ro && mount -o ro /dev/mapper/vendor_ro /mnt/vendor_ro

# Copy the amplifier tuning blob into place (chip id 0x2113 on rhodep)
install -D -m 0644 /mnt/vendor_ro/firmware/aw882xx_pid_2113_acf.bin \
        /lib/firmware/aw88261_acf.bin

umount /mnt/vendor_ro && dmsetup remove vendor_ro
reboot
```

After reboot the amplifiers probe and you get sound (PipeWire routing is already
set up by the shipped image).

### Notes

- If you **reinstall the rootfs**, redo the audio step (it lives in `userdata`).
- The stock `vendor` sector offsets (`6789120` / `1235440`) are for rhodep's
  known stock layout; if yours differs, recompute them from the `super`
  partition's `liblp` metadata.
- **NFC** needs blobs that are no longer on the phone after flashing (they come
  from a LineageOS ROM) and is not fully functional anyway — safe to ignore.
- **Never `apt full-upgrade` without the holds** (`sudo apt-mark hold
  firmware-atheros`), or Wi-Fi/BT firmware gets overwritten.

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
