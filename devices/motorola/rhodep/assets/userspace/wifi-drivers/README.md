# rhodep-wifi-drivers

Install the **external USB Wi-Fi adapter drivers** so a freshly flashed image
comes up ready for **monitor mode + packet injection** on `wlan1`, instead of
needing the drivers built by hand. The internal WCN3990 (`wlan0`) cannot do raw
mode; these adapters are how the port does WiFi auditing.

Two adapters, two drivers (this is an orchestrator around the two driver
packages, not a third driver):

| Adapter | Chip | Driver | Package |
| --- | --- | --- | --- |
| TP-Link TL-WN722N v2/v3 | RTL8188EUS | `rtl8188eus` (`8188eu`), patched for 7.2 | `packages/rhodep-rtl8188eus-fix` |
| TP-Link Archer T2U Nano | RTL8811AU | `rtw88` (`rtw_8821au`) | `packages/rhodep-rtl8821au` |

## Why this is not baked into the image like the other userspace

Both are **DKMS modules: compiled against the kernel that will run them.** A
loadable `.ko` has to match its kernel exactly (same build, not just the same
`uname -r` -- see "Building out-of-tree drivers" in the top-level README). The
debos build chroot runs the *build host's* kernel, not the phone's, so it cannot
produce a working `.ko` for the phone. Baking a prebuilt module in would give
you a file that refuses to load with `Exec format error` / version-magic
mismatch.

So the build is **deferred to the device**:

- In the **chroot**, `install.sh` stages the driver sources under
  `/usr/local/share/rhodep/wifi-drivers/` and arms
  `rhodep-wifi-drivers-firstboot.service`.
- On the **device's first boot**, that service runs
  `rhodep-wifi-drivers-build`, which compiles both drivers against the booted
  kernel, writes a per-kernel stamp (`/var/lib/rhodep/wifi-drivers.<KVER>.done`)
  and disables itself. It needs the network once, to `apt-get` the DKMS package
  and build deps; if the network or the kernel headers are not there yet it
  exits cleanly and retries on the next boot.
- On a **live system**, `install.sh` builds immediately.

## Prerequisite: kernel headers

DKMS needs `/lib/modules/<KVER>/build`. The pmbootstrap apk does not ship it, so
this port builds a matching `linux-headers-<KVER>.deb` with
`scripts/build-kernel-headers.sh`. Put that `.deb` next to `install.sh` (or in
`/srv`, or under the stage dir) and it is installed automatically; otherwise
install it yourself before running. Without headers the first-boot service waits
and retries rather than failing.

## Use

```sh
# in the debos chroot (stage + arm firstboot):
sh install.sh

# on the device, to build now:
sudo sh install.sh
# or, after a kernel change, just:
sudo rhodep-wifi-drivers-build
```

After a build, plug the adapter and power the port (`otg on` -- single USB-C
port, so host mode needs VBUS boost from the charger). It enumerates as `wlan1`;
`wlan0` is never touched. Monitor mode: `rhodep-pwn-monstart` (port way) or
`airmon-ng start wlan1`.

## After a kernel change

A new kernel means new modules. `rhodep-wifi-drivers-build` keys its stamp to
`uname -r`, so booting a new kernel makes the first-boot service build again on
its own -- provided the matching `linux-headers-<KVER>` are installed. The
built `.ko` files are made immutable by `rhodep-protect-files` (the driver
packages do this), which is deliberate friction: DKMS cannot overwrite an
immutable `.ko`, so on a kernel change you first
`rhodep-protect-files release <ko>`. See "Updating the kernel" in the top-level
README -- you are reapplying every out-of-tree piece anyway.
