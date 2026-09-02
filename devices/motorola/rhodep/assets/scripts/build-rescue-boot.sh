#!/bin/sh
# Build a RESCUE boot image for motorola-rhodep.
#
# The rescue image = the pmOS kernel + pmOS initramfs from a WORKING pmOS boot.img
# (e.g. boot-v45-DOCKER.img), with `pmos.debug-shell` added to the cmdline. On
# boot it brings up the USB gadget network + a root telnet on 172.16.42.1:23 and
# does NOT mount the root filesystem. This lets you `dd` to /dev/disk/by-partlabel/
# userdata to (re)install Kali or pmOS, or to recover from any bootloop.
#
# Why reuse a pmOS boot.img: its kernel+DTB+initramfs are known to boot on this
# device, and the pmOS initramfs already implements `pmos.debug-shell`.
#
# Usage:  sh build-rescue-boot.sh <working-pmos-boot.img> <rescue-boot.img>
# Ex:     sh build-rescue-boot.sh boot-v45-DOCKER.img rescue-boot.img
set -e

OLD="${1:?usage: build-rescue-boot.sh <pmos-boot.img> <rescue-boot.img>}"
OUT="${2:?usage: build-rescue-boot.sh <pmos-boot.img> <rescue-boot.img>}"
[ -f "$OLD" ] || { echo "ERROR: $OLD not found" >&2; exit 1; }

SELFDIR="$(cd "$(dirname "$0")" && pwd)"

# 1) Split the source Android boot image (v2): kernel(+appended dtb), ramdisk, dtb, cmdline
python3 - "$OLD" <<'PY'
import struct, sys
d = open(sys.argv[1], "rb").read()
assert d[:8] == b"ANDROID!", "not an Android boot image"
PS = 4096
def pad(n):
    r = n % PS
    return n + (PS - r) if r else n
ksz  = struct.unpack_from("<I", d, 8)[0]
rsz  = struct.unpack_from("<I", d, 16)[0]
hv   = struct.unpack_from("<I", d, 40)[0]
dtbsz = struct.unpack_from("<I", d, 1648)[0] if hv == 2 else 0
cmd  = d[64:64+512].split(b"\0")[0].decode()
extra = d[608:608+1024].split(b"\0")[0].decode()
open("/tmp/rescue_kernel", "wb").write(d[PS:PS+ksz])            # kernel (+appended dtb)
open("/tmp/rescue_ramdisk", "wb").write(d[PS+pad(ksz):PS+pad(ksz)+rsz])
open("/tmp/rescue_cmdline", "w").write(cmd + extra)
# the dtb sits at the end of the kernel blob (last dtbsz bytes) for header v2
kern = d[PS:PS+ksz]
open("/tmp/rescue_dtb", "wb").write(kern[-dtbsz:] if dtbsz else b"")
print("split OK: kernel", ksz, "ramdisk", rsz, "dtb", dtbsz, "hdr_v", hv)
print("orig cmdline:", (cmd+extra))
PY

# 2) Build the rescue cmdline = original cmdline + pmos.debug-shell (dedup)
ORIG_CMDLINE="$(cat /tmp/rescue_cmdline)"
case "$ORIG_CMDLINE" in
    *pmos.debug-shell*) RESCUE_CMDLINE="$ORIG_CMDLINE" ;;
    *) RESCUE_CMDLINE="pmos.debug-shell $ORIG_CMDLINE" ;;
esac
echo "rescue cmdline: $RESCUE_CMDLINE"

# 3) Repack. mkbootv2b.py takes: vmlinuz dtb ramdisk cmdline out  (it appends the dtb).
#    Our /tmp/rescue_kernel ALREADY has the dtb appended, so pass an EMPTY dtb to
#    avoid double-appending: use a tiny helper that packs kernel-as-is.
python3 - "$RESCUE_CMDLINE" "$OUT" <<'PY'
import struct, sys
PS = 4096
def pad(b):
    r = len(b) % PS
    return b + b"\0"*(PS-r) if r else b
cmdline, out = sys.argv[1], sys.argv[2]
kernel  = open("/tmp/rescue_kernel", "rb").read()    # already kernel+dtb
ramdisk = open("/tmp/rescue_ramdisk", "rb").read()
dtb     = open("/tmp/rescue_dtb", "rb").read()
h = bytearray(4096)
h[0:8] = b"ANDROID!"
struct.pack_into("<I", h, 8,  len(kernel))
struct.pack_into("<I", h, 12, 0x00008000)   # kernel_addr
struct.pack_into("<I", h, 16, len(ramdisk))
struct.pack_into("<I", h, 20, 0x01000000)   # ramdisk_addr
struct.pack_into("<I", h, 24, 0)            # second_size
struct.pack_into("<I", h, 28, 0)            # second_addr
struct.pack_into("<I", h, 32, 0x00000100)   # tags_addr
struct.pack_into("<I", h, 36, PS)
struct.pack_into("<I", h, 40, 2)            # header_version 2
struct.pack_into("<I", h, 44, 0x16000194)   # os_version (stock)
c = cmdline.encode()
h[64:64+min(len(c),511)] = c[:511]
if len(c) > 512:
    h[608:608+min(len(c)-512,1023)] = c[512:512+1023]
struct.pack_into("<I", h, 1632, 0)          # recovery_dtbo_size
struct.pack_into("<Q", h, 1636, 0)
struct.pack_into("<I", h, 1644, 1660)       # header_size
struct.pack_into("<I", h, 1648, len(dtb))   # dtb_size
struct.pack_into("<Q", h, 1652, 0x01f00000) # dtb_addr
img = bytes(h) + pad(kernel) + pad(ramdisk) + pad(dtb)
open(out, "wb").write(img)
print("wrote", out, len(img), "bytes")
PY

rm -f /tmp/rescue_kernel /tmp/rescue_ramdisk /tmp/rescue_dtb /tmp/rescue_cmdline
echo ""
echo "Rescue image ready: $OUT"
echo "Flash it:  fastboot flash boot_a $OUT && fastboot --set-active=a && fastboot reboot"
echo "Then:      telnet 172.16.42.1 23   (root shell, root NOT mounted)"
