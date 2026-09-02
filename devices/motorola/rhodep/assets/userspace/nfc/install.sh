#!/bin/sh
# Install the NFC userspace: the rhodep-nfc reader tool and the modprobe.d
# line that makes the chip actually configure its antenna. Run as root.
#
# The kernel side (patches 0101-0105) is what makes the S3NRN4V read cards at
# all. This installs the pieces that live in userspace:
#
#   /etc/modprobe.d/s3fwrn5-rhodep.conf   sets rfreg_dual=1, without which the
#                                         chip comes up on the legacy transfer,
#                                         answers on the bus, and reads nothing
#   /usr/local/bin/rhodep-nfc             terminal reader for every tag tech
#
# The RF register blobs (sec_s3fwrn5_rfreg.bin, sec_s3fwrn5_swreg.bin) are
# shipped and protected separately with the vendor NFC blobs.

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
PROTECT=/usr/local/sbin/rhodep-protect-files

BIN=/usr/local/bin/rhodep-nfc
CONF=/etc/modprobe.d/s3fwrn5-rhodep.conf

install_file() {
	src="$1"; dst="$2"; mode="$3"
	[ -x "$PROTECT" ] && "$PROTECT" release "$dst" 2>/dev/null || true
	install -D -m "$mode" "$src" "$dst"
	echo "installed $dst"
}

install_file "$HERE/rhodep-nfc" "$BIN" 0755
install_file "$HERE/modprobe.d/s3fwrn5-rhodep.conf" "$CONF" 0644

# Apply the module option now if the module is already loaded, so NFC works
# this boot without a reload. Harmless if it is not loaded.
if [ -d /sys/module/s3fwrn5 ]; then
	echo 1 > /sys/module/s3fwrn5/parameters/rfreg_dual 2>/dev/null \
		&& echo "rfreg_dual set live" || true
fi

if [ -x "$PROTECT" ]; then
	"$PROTECT" register nfc 0755 "$BIN" 2>/dev/null || true
	"$PROTECT" register nfc 0644 "$CONF" 2>/dev/null || true
	echo "protected"
fi

python3 -m py_compile "$BIN" && echo "rhodep-nfc syntax OK"
echo "nfc userspace installed -- stop neard and run: sudo rhodep-nfc info"
