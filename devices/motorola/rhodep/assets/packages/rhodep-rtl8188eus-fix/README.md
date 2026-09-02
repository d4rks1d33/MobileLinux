# rhodep-rtl8188eus-fix

Make the **TP-Link RTL8188EUS** USB Wi-Fi adapter work with **monitor mode +
packet injection** on the Kali NetHunter Pro rhodep port (Linux 7.2, aarch64).

The mainline `rtl8xxxu` driver that ships with the kernel gives monitor mode
but **unreliable injection** on this chip. The proper driver is `rtl8188eus`
(loaded as `8188eu`), which Kali ships as `realtek-rtl8188eus-dkms` — but that
driver does not build against Linux 7.2 / GCC 15 out of the box. This directory
provides the fix.

## What breaks and why

`realtek-rtl8188eus-dkms` 5.3.9~git20260622 fails to compile on 7.2 with three
unrelated, mechanical errors (all handled by `apply-rtl8188eus-fix.sh`):

1. **AppleTalk NAT bridge** — `core/rtw_br_ext.c` references `struct elapaarp`,
   `struct ddpehdr`, `AARP_PA_ALEN`, which were removed from the kernel. This is
   the `CONFIG_BR_EXT` connection-sharing helper, not needed for monitor/inject.
   Fix: `CONFIG_BR_EXT = n` in the driver Makefile.
2. **`strncpy` no longer implicitly declared** (GCC 15 fortify treats the
   implicit builtin as an error). Fix: replace every `strncpy` with `strscpy`
   (same `(dest, src, size)` signature, always declared).
3. **`cfg80211_ops.remain_on_channel` signature changed** — it gained a trailing
   `const u8 *rx_addr` parameter. Fix: add it to
   `cfg80211_rtw_remain_on_channel()`.

## Prerequisites: kernel headers

DKMS needs the kernel headers for the **running** kernel (`7.2.0-rc5`). The
pmbootstrap apk does not ship them, so this repo provides a matching
`linux-headers-7.2.0-rc5_*.deb` (see `../../kernel/` build notes). Install it
first, then the compile deps:

```sh
dpkg -i linux-headers-7.2.0-rc5_7.2~rc5-rhodep2_arm64.deb
apt --fix-broken install -y          # pulls bc bison flex libelf-dev libssl-dev
```

The headers package provides `/lib/modules/7.2.0-rc5/build`, a full
`Module.symvers` (with mac80211/cfg80211 symbols) and the aarch64-native
`scripts/` (modpost/fixdep) — validated by building an out-of-tree module with
`vermagic=7.2.0-rc5 SMP preempt mod_unload aarch64`.

## Install the driver

```sh
# 1. the DKMS package from Kali
apt install -y realtek-rtl8188eus-dkms

# 2. patch its source for kernel 7.2 (idempotent; auto-detects the DKMS dir)
./apply-rtl8188eus-fix.sh

# 3. build + install the module
VER=5.3.9~git20260622.cbeae98
dkms build   -m realtek-rtl8188eus -v "$VER" -k 7.2.0-rc5 --force
dkms install -m realtek-rtl8188eus -v "$VER" -k 7.2.0-rc5 --force
depmod -a 7.2.0-rc5
```

## Make it permanent

```sh
# prefer 8188eu over the generic driver, and autoload it
echo "blacklist rtl8xxxu" > /etc/modprobe.d/blacklist-rtl8xxxu.conf
echo "8188eu"             > /etc/modules-load.d/8188eu.conf
update-initramfs -u -k 7.2.0-rc5

# freeze the packages so apt can't overwrite the working kernel/driver
apt-mark hold realtek-rtl8188eus-dkms linux-headers-7.2.0-rc5 \
              linux-image-7.2.0-rc5 firmware-motorola-rhodep \
              rhodep-modem-support rhodep-usb-otg rhodep-battery-jeita
```

## Load and test

The TP-Link needs USB-host + OTG VBUS to enumerate (single USB-C port). The
`rhodep-usb-otg` package brings the port up in host mode at boot; if the adapter
does not appear, force VBUS with `otg on` (or `i2cset -f -y 0 0x3b 0x01 0x3a`).

```sh
modprobe -r rtl8xxxu 2>/dev/null
modprobe 8188eu
iw dev                        # -> wlan1 appears (phy#1)

airmon-ng start wlan1
aireplay-ng --test wlan1mon   # -> "Injection is working!"
```

## Confirmed working

Kernel `7.2.0-rc5` aarch64, Moto G82 5G (rhodep). `8188eu` autoloads at boot,
`wlan1` present, monitor mode + injection verified. Survives reboot.
