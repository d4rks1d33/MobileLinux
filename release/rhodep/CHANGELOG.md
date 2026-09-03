# Changelog — Kali for Motorola Moto G82 5G (rhodep)

## v1.0.0 — first public release

First MobileLinux release for the **Motorola Moto G82 5G (`rhodep`,
Qualcomm SM6375)**: a full **Kali Linux Rolling + Phosh** desktop on a
**mainline 7.2-rc5** kernel, built end-to-end by the MobileLinux pipeline and
verified booting on real hardware.

### What's inside

- **Mainline Linux 7.2-rc5** kernel for the SM6375, Kali flavor
  (NetHunter symbols: USB Wi-Fi injection, SDR, BadUSB HID, CAN, NFS, extended
  netfilter) with the Debian-critical `CONFIG_MODULE_ALLOW_BTF_MISMATCH`.
- **Kali Linux Rolling 2026.3** userland with **Phosh** (mobile shell), the
  phone role (dialer/SMS/Contacts) and the Kali toolset.
- **Device support** installed and pinned: `rhodep-modem-support`,
  `rhodep-usb-otg`, `rhodep-battery-jeita`, `rhodep-phosh-wifi-guard`
  (with their apt dependencies resolved).
- First-boot fixes applied: `droid-juicer` and `systemd-repart` masked so the
  desktop actually comes up.

### Working hardware

Display, touch, GPU (GL/Vulkan/OpenCL), internal Wi-Fi + Bluetooth, audio
(speaker/mic/jack), battery + charging, USB/OTG, sensors (auto-rotate),
microSD, vibrator. Modem: GSM/2G voice + SMS + GPRS. NFC: reader.

### Known limitations

- **Mobile data (LTE)** is disabled: with `ipa.ko` loaded + LTE attach the SoC
  watchdog-resets, so `ipa.ko` is kept out of boot. (Voice/SMS/GPRS work.)
- **GNSS** standalone resets the SoC (cell-id positioning works).
- **Camera** and **fingerprint** are not functional on mainline.
- **First boot is slow**: the initramfs grows the root filesystem with
  `resize2fs`; a black screen / stuck logo for a few minutes on the very first
  boot is normal (the phone vibrates before switching to the real system).

### Notes

- Boot image uses the **postmarketOS initramfs** (which mounts the sector-4096
  GPT-in-userdata rootfs by UUID); the cmdline has **no `rootwait`** (it would
  hang this initramfs).
- Non-redistributable vendor firmware (WCN3990 Wi-Fi/BT, Adreno zap, AW88261
  audio, ...) is **not** shipped and must be extracted from your device for the
  affected peripherals to initialize.

Default login: **`kali` / `1234`** — change it immediately.
