# Installing kali on Motorola Moto G 2015 (motorola-osprey)

- SoC: Snapdragon 410
- Architecture: aarch64
- Install strategy: `fastboot`
- A/B slots: no

This device is installed entirely over fastboot.

> **Bootloader must be unlocked first.** This erases the device.

## Boot image note

This device stores the rootfs as a **GPT disk inside the `plain`** (logical sector 512). Mounting it requires the **postmarketOS initramfs** (which does `losetup -Pf --sector-size 4096` on the target partition and mounts root by UUID). The distro's own initramfs (initramfs-tools) does NOT do this and will drop to a busybox emergency shell. The build therefore embeds the pmOS initramfs from the `--input` base boot image — so pass a known-good pmOS `boot.img` to `mobilelinux build ... --input <pmos-boot.img>`.

## Steps

`mobilelinux flash osprey` performs these automatically:

1. unlock the bootloader if needed (fastboot oem unlock / Motorola unlock code)
2. Flash the Android boot image to the boot partition  ⚠️ destructive
3. Write the pmOS rootfs image to userdata (fastboot flash)  ⚠️ destructive
4. reboot

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
