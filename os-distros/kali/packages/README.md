# Kali package lists

Plain `apt` package lists consumed by the Kali distro backend during the
**chroot integration phase** (after debos produces the base rootfs). See
[../../../docs/os-distros.md](../../../docs/os-distros.md) and
[../../../docs/build-and-flash.md](../../../docs/build-and-flash.md).

| File | When | What |
|------|------|------|
| `base.list` | always | initramfs-tools, SoC glue, Kali metapackages, kali-menu |
| `phone-role.list` | `--desktop phosh` | dialer/SMS/MMS/Contacts (mobian-phosh-phone) + file manager |
| `build-deps.list` | if building device userspace helpers | dev headers to compile modem helpers in-chroot |

Device-specific packages (the `rhodep-*` `.deb`s, firmware) are **not** listed
here — they come from the device definition's `device_packages` and are applied
by the device layer, not the distro. This keeps the distro reusable across
devices.

Format: one package per line, `#` for comments.
