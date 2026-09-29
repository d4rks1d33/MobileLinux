# Installing kali on Moto G82 5G (rhodep)

- SoC: Snapdragon 695 5G
- Architecture: aarch64
- Install strategy: `rescue-dd`
- A/B slots: yes

This device's bootloader refuses to flash the rootfs partition directly (and has no fastbootd). Installation therefore boots a **rescue image** that exposes a root shell over USB networking, and streams the rootfs disk into the target partition with `dd`.

> **Bootloader must be unlocked first.** This erases the device.

## Rescue environment

- method: `pmos-debug-shell`
- reach it at: `telnet://172.16.42.1:23`

Rescue image = pmOS kernel+DTB+initramfs with 'pmos.debug-shell' appended. Brings up USB-gadget networking and a root telnet WITHOUT mounting root, so userdata can be dd-written. Also used for recovery.

## Boot image note

This device stores the rootfs as a **GPT disk inside the `gpt-in-partition`** (logical sector 4096). Mounting it requires the **postmarketOS initramfs** (which does `losetup -Pf --sector-size 4096` on the target partition and mounts root by UUID). The distro's own initramfs (initramfs-tools) does NOT do this and will drop to a busybox emergency shell. The build therefore embeds the pmOS initramfs from the `--input` base boot image — so pass a known-good pmOS `boot.img` to `mobilelinux build ... --input <pmos-boot.img>`.

## Steps

`mobilelinux flash rhodep` performs these automatically:

1. rescue-dd: userdata cannot be written by fastboot on this device
2. Flash the rescue image to the boot slot  ⚠️ destructive
3. set-active-slot
4. reboot
5. Wait for the rescue telnet at 172.16.42.1:23
6. Stream the Kali GPT disk into userdata  ⚠️ destructive
7. Flash the distro boot image  ⚠️ destructive
8. set-active-slot
9. reboot
10. First boot resizes the rootfs and starts the desktop

## First boot

- The device **vibrates before switch_root** — that means the initramfs found and mounted the rootfs by UUID. Good sign.
- On the **first boot** the initramfs runs `resize2fs` to grow the rootfs to fill the partition, then systemd + the desktop start. **A black screen / stuck logo for several minutes on the first boot is NORMAL.** Wait.
- You can confirm it's alive over USB: `ssh kali@172.16.42.1` (password `1234`).

## Black screen after the logo — troubleshooting

If it stays black long after the first boot (and SSH works), it is a userspace/kernel issue, not the boot image:

1. **`droid-juicer` not masked** hangs `graphical.target` (it waits forever for Android firmware). The build masks it, but an `apt full-upgrade` can re-enable it. Fix:
   ```
   sudo systemctl mask droid-juicer.service systemd-repart.service
   sudo reboot
   ```
2. **GDM vs phosh.service** both claiming the display (if `kali-linux-default` pulled in gdm3). Ensure a single display owner (the login layer masks `phosh.service` and points `display-manager` at gdm3).
3. **Wrong cmdline**: the boot image must use the pmOS initramfs and a cmdline WITHOUT `rootwait` (rootwait hangs because the pmOS initramfs resolves root by `pmos_root_uuid`, not `root=`). This build already does the right thing.
4. **UUID mismatch**: the `pmos_root_uuid` in the boot image must equal the ext4 UUID inside userdata. Verify from a rescue shell: `losetup -Pf --sector-size 4096 /dev/disk/by-partlabel/userdata; blkid /dev/loop0p2`.

## Safety

`flash` detects the connected device, confirms it matches this definition, shows exactly which partitions it will modify, verifies image hashes, and asks for confirmation. Use `--dry-run` to preview without writing anything.
