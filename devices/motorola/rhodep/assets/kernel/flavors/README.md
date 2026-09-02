# Kernel flavors

The kernel (source + 108 patches + DTB) is shared by every distro. The ONLY
per-distro difference is the `.config`, expressed here as a **fragment** merged
onto the shared base config (`../provider/config-base.aarch64`) with
`merge_config.sh` + `make olddefconfig`.

| Flavor | Fragment | Distros | Discriminator |
|--------|----------|---------|---------------|
| `pmos` | `pmos.fragment` (empty = base) | postmarketos | `CONFIG_RT2800USB` absent |
| `kali` | `kali.fragment` (NetHunter delta) | kali | `CONFIG_RT2800USB=m` present |

`kali.fragment` was generated as the real delta between the Kali config and the
pmOS base config in the reference port (NetHunter USB WiFi injection, SDR,
BadUSB HID, CAN, NFS server, extended netfilter, binder, + the shared
`MODULE_ALLOW_BTF_MISMATCH`).

Build a flavor with:

    mobilelinux kernel rhodep --flavor kali

which swaps the active aport config, runs `pmbootstrap checksum` + `build`, and
verifies the discriminator so you always know which flavor compiled
(`grep -c RT2800USB <config>` — 1+=Kali, 0=pmOS).
