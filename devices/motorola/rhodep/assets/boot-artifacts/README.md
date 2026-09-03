# Boot image artifacts

`mkbootv2b.py` needs a ramdisk and a cmdline to build a boot image. Both come
from the known-good `kali-boot-v47.img` and never change, so they are kept
outside /tmp:

- `v47_cmdline.txt` — the kernel command line (committed here).
- `v47_ramdisk` — the pmOS initramfs, ~13 MB, **not committed** (the repo
  ignores binaries). It lives at
  `/opt/postmarket/_common/boot-artifacts/v47_ramdisk`.

To extract the ramdisk again from a boot image, unpack `kali-boot-v47.img` with
any Android boot image tool and take the ramdisk section.

See `docs/interconnect-sm6375-wip/HANDOFF-SESSION4.md` §6 for the full
build/flash procedure.
