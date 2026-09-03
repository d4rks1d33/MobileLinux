#!/bin/sh
# rhodep: read the rear sensor's first registers across power cycles.
#
# Power cycling is done with unbind/bind rather than rmmod/insmod: rmmod
# intermittently returns EBUSY and leaves the module loaded but unbound, which
# reads as "the whole bus died" and is not that at all. Module parameters are
# writable through sysfs, so the sequence can be varied without reloading.
BUS=4
P=/sys/module/camdiag/parameters
DRV=/sys/bus/i2c/drivers/camdiag

blk() {	# $1 = i2c address
	out=""
	for r in 00 02 04 06 08 0a 0c 0e; do
		v=$(i2ctransfer -f -y $BUS w2@$1 0x00 0x$r r2 2>/dev/null | tr -d ' ')
		out="$out ${v:-XXXX}"
	done
	echo "$out"
}

repower() {	# "name=value" ...
	for kv in "$@"; do
		echo "${kv#*=}" > "$P/${kv%%=*}" 2>/dev/null
	done
	echo 4-0056 > "$DRV/unbind" 2>/dev/null
	sleep 1
	echo 4-0056 > "$DRV/bind" 2>/dev/null
	sleep 2
	[ -e "$DRV/4-0056" ] || echo "    (warning: not bound)"
}

echo "== control: EEPROM 0x50, must be stable and reg 0x0d = 0x5355 =="
repower
echo "  0x50:$(blk 0x50)"
echo "  0x50 reg 0x0d = $(i2ctransfer -f -y $BUS w2@0x50 0x00 0x0d r2 2>/dev/null | tr -d ' ')"
echo
echo "== sensor 0x56, same power-up repeated =="
echo "  A read1:$(blk 0x56)"
echo "  A read2:$(blk 0x56)"
repower
echo "  B read1:$(blk 0x56)"
echo
echo "== varying the sequence =="
repower pre_reset_ms=20 post_reset_ms=200
echo "  C 20/200ms   :$(blk 0x56)"
repower pre_reset_ms=1 post_reset_ms=18 assert_reset_first=N
echo "  D no pre-reset:$(blk 0x56)"
repower assert_reset_first=Y mclk_before_reset=N
echo "  E mclk after :$(blk 0x56)"
repower mclk_before_reset=Y
echo "  F back to vendor order:$(blk 0x56)"
