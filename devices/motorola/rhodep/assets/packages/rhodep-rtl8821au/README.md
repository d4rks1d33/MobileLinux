# rhodep-rtl8821au

Make the **TP-Link Archer T2U Nano (AC600)** — an **RTL8811AU / RTL8821AU**
dual-band USB Wi-Fi adapter — work with **monitor mode + packet injection** on
the Kali NetHunter Pro rhodep port (Linux 7.2, aarch64).

This is the dual-band (2.4 + 5 GHz) counterpart to `rhodep-rtl8188eus-fix`,
which covers the single-band RTL8188EUS. Different chip, different driver.

## Which driver, and why not Kali's

The chip is USB `2357:011e`. Three drivers can claim it, and only one works here:

- **`rtl8xxxu`** (in-tree, generic) — does not bind this device on this kernel.
- **`realtek-rtl88xxau-dkms`** (Kali's out-of-tree aircrack-ng driver) — **does
  not build on Linux 7.2**. Its `dkms.conf` even declares
  `BUILD_EXCLUSIVE_KERNEL_MAX="6.15"`, and forcing past that hits dead kernel
  API: `from_timer` / `del_timer_sync` were removed, plus a broken
  `EXTRA_CFLAGS -I$(src)/include` that no longer reaches the compiler under
  modern kbuild. aircrack-ng itself now marks this driver **deprecated** and
  points at rtw88.
- **`rtw88`** (lwfinger, mac80211) — **compiles clean on 7.2 with no patches**
  and, being a proper mac80211 driver, gives standard `airmon-ng` / monitor /
  injection with none of the 8188eu-style flip-back-to-managed quirks. The
  module that matches this chip is `rtw_8821au` (see `rtw8821au.c`, which lists
  `USB_DEVICE_AND_INTERFACE_INFO(0x2357, 0x011e, ...)`).

So this package uses **rtw88**, installed via DKMS so it rebuilds when the
kernel changes.

## Prerequisites: kernel headers

DKMS needs the headers for the **running** kernel (`7.2.0-rc5`). The pmbootstrap
apk does not ship them, so install this repo's matching
`linux-headers-7.2.0-rc5_*.deb` first (see `../../kernel/` build notes). It
provides `/lib/modules/7.2.0-rc5/build`, a full `Module.symvers` with
mac80211/cfg80211 symbols, and the aarch64-native `scripts/`.

## Install

```sh
./install.sh
```

It clones `lwfinger/rtw88` (or uses a `rtw88/` checkout vendored next to the
script for offline installs), drops it in `/usr/src/rtw88-0.6`, removes Kali's
non-building `realtek-rtl88xxau-dkms` if present, and builds + installs the
module with DKMS. The firmware (`/lib/firmware/rtw88/rtw8821a_fw.bin`) is laid
down by the driver's own install.

No blacklist is needed: `rtl8xxxu` does not claim `2357:011e`, and the module
autoloads on the USB modalias when the adapter enumerates.

## LED off

The Archer T2U Nano has a bright LED that is pointless on a phone and adds heat
at the single USB-C port (already warm from OTG VBUS boost).
`99-rhodep-rtw88-led-off.rules` switches it off (`trigger=none`,
`brightness=0`) whenever the LED class device appears; it matches by the
`rtw88-*` LED name so the USB-port-dependent instance name does not matter.

## Held / protected

rtw88 is a DKMS module, not an apt package, so there is nothing to `apt-mark
hold`. Instead `install.sh` registers the built `rtw_8821au.ko` and the udev
rule with `rhodep-protect-files` (immutable + snapshot + auto-restore), and the
DKMS source lives in `/usr/src/rtw88-0.6` with the kernel headers held so it can
always rebuild.

Making the `.ko` immutable is deliberate friction: it stops an accidental delete
but means DKMS cannot overwrite it on a **kernel change**. On a new kernel,
`rhodep-protect-files release <ko>`, `dkms install rtw88/0.6 -k <NEW> --force`,
then re-run `install.sh`. See "Updating the kernel" in the top-level README —
you are reapplying every out-of-tree patch anyway.

## Load and test

The adapter needs USB-host + OTG VBUS to enumerate (single USB-C port).
`rhodep-usb-otg` brings the port up; if `wlan1` does not appear, `otg on`.

```sh
otg on                        # power the port (charging stops)
modprobe rtw_8821au
iw dev                        # -> wlan1 appears (phy#1), driver rtw_8821au

# monitor mode, the port way (never touches wlan0):
rhodep-pwn-monstart           # -> wlan1mon in monitor mode
# or the generic way:
airmon-ng start wlan1
```

## Confirmed working

Kernel `7.2.0-rc5` aarch64, Moto G82 5G (rhodep). `airmon-ng` reports:

	phy1  wlan1  rtw_8821au  TP-Link AC600 wireless Realtek RTL8811AU [Archer T2U Nano]

Monitor mode confirmed, and channel set accepted on **both bands** — 2.4 GHz
(ch 1-14) and 5 GHz (ch 36-128). Firmware 42.4.0 loads. Survives reboot via
DKMS.

> pwnagotchi (`extra-tools/pwnagotchi/`) captures on `wlan1mon` regardless of
> which of the two adapters is plugged in: `rhodep-pwn-monstart` works by
> interface name and phy, not by driver, so it drives this rtw88 adapter the
> same as the 8188eus one.
