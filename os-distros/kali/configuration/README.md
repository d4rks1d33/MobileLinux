# Kali configuration

Declarative, distro-level configuration applied during the chroot integration
phase (after debos builds the base rootfs).

- `integration.yaml` — services to enable/mask, login (GDM) setup, the order in
  which the device's userspace installer layers run, and that apt holds are
  applied last.

Device-specific configuration (the `rhodep-*` packages, firmware extraction,
first-boot masks) lives in the **device definition**
(`devices/<vendor>/<codename>/device.yaml`), not here. The distro configuration
is the same for every device; the device layer adds device-specific bits on top.

See [../../../docs/os-distros.md](../../../docs/os-distros.md).
