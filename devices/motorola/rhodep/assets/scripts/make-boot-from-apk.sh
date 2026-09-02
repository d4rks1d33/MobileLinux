#!/bin/sh
# Build a new boot.img from the kernel APK just built by pmbootstrap,
# reusing the initramfs from a previous boot.img (it does not change when
# only the kernel is touched).
#
# Usage: sh make-boot-from-apk.sh <old-boot.img> <new-boot.img>
# Ex:    sh make-boot-from-apk.sh old-boot.img new-boot.img
#
set -e

# Resolve this script's own directory so mkbootv2b.py is found in any clone,
# not at a hard-coded absolute path.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

OLD="${1:?missing old boot.img (source of the initramfs)}"
NEW="${2:?missing new boot.img name}"

PMB="${PMB:-$HOME/.local/var/pmbootstrap}"
PKGDIR="$PMB/packages/edge/aarch64"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

APK=$(ls -t "$PKGDIR"/linux-motorola-rhodep-*.apk 2>/dev/null | head -1)
[ -n "$APK" ] || { echo "ERROR: no encuentro el apk del kernel en $PKGDIR" >&2; exit 1; }
echo "apk:        $APK"

mkdir -p "$WORK/apk" && tar xzf "$APK" -C "$WORK/apk" 2>/dev/null || true
KERNEL=$(find "$WORK/apk" -name vmlinuz | head -1)
DTB=$(find "$WORK/apk" -name 'sm6375-motorola-rhodep.dtb' | head -1)
[ -n "$KERNEL" ] && [ -n "$DTB" ] || { echo "ERROR: missing vmlinuz or dtb in the apk" >&2; exit 1; }
echo "vmlinuz:    $(stat -c%s "$KERNEL") bytes"
echo "dtb:        $(stat -c%s "$DTB") bytes"

# Extract the initramfs and cmdline from the old boot.img (Android v2 header)
python3 - "$OLD" "$WORK/ramdisk" "$WORK/cmdline" <<'EOF'
import struct, sys
old, rd_out, cmd_out = sys.argv[1], sys.argv[2], sys.argv[3]
d = open(old, "rb").read()
assert d[:8] == b"ANDROID!", "no es un boot image Android"
PS = 4096
def pad(n):
    r = n % PS
    return n + (PS - r) if r else n
ksz = struct.unpack_from("<I", d, 8)[0]
rsz = struct.unpack_from("<I", d, 16)[0]
off = PS + pad(ksz)
open(rd_out, "wb").write(d[off:off + rsz])
cmd = d[64:64 + 512].split(b"\0")[0].decode()
extra = d[608:608 + 1024].split(b"\0")[0].decode()
open(cmd_out, "w").write(cmd + extra)
print("initramfs:  %d bytes" % rsz)
print("cmdline:    %s" % (cmd + extra))
EOF

CMDLINE=$(cat "$WORK/cmdline")

# Allow editing the cmdline via env vars, for diagnostic images:
#   CMDLINE_DROP="quiet"  removes those words
#   CMDLINE_ADD="..."     appends at the end
if [ -n "$CMDLINE_DROP" ]; then
	for w in $CMDLINE_DROP; do
		CMDLINE=$(printf '%s' "$CMDLINE" | tr ' ' '\n' | grep -vx -- "$w" | tr '\n' ' ')
	done
	echo "cmdline:    (removed: $CMDLINE_DROP)"
fi
if [ -n "$CMDLINE_ADD" ]; then
	CMDLINE="$CMDLINE $CMDLINE_ADD"
	echo "cmdline:    (added: $CMDLINE_ADD)"
fi

# Ensure clk_scale=100 in the cmdline
case "$CMDLINE" in
	*panel_novatek_nt37701.clk_scale=*) ;;
	*) CMDLINE="$CMDLINE panel_novatek_nt37701.clk_scale=100"
	   echo "cmdline:    (added clk_scale=100)" ;;
esac

python3 "$SCRIPT_DIR/mkbootv2b.py" \
	"$KERNEL" "$DTB" "$WORK/ramdisk" "$CMDLINE" "$NEW"

echo
echo "done: $NEW"
md5sum "$NEW"
echo
echo "Flash with:"
echo "  fastboot flash boot_a $(basename "$NEW")"
echo "  fastboot --set-active=a && fastboot reboot"
