#!/bin/sh
# Does the I2C transaction shape matter? Compare a repeated-START read (what
# the CCI and i2ctransfer do by default) against write-STOP-then-read.
#
# Self-contained on purpose: sets the parameters it needs, rebinds, and refuses
# to measure anything until the device is bound with MCLK actually running.
# Every wrong result in this investigation so far came from measuring a device
# that was not in the state the script assumed.
DRV=/sys/bus/i2c/drivers/camdiag
P=/sys/module/camdiag/parameters

DEV=
for d in /sys/bus/i2c/devices/*-0056; do [ -e "$d" ] && DEV=$(basename "$d"); done
[ -n "$DEV" ] || { echo "no *-0056 device"; exit 1; }
BUS=${DEV%%-*}

echo Y > "$P/enable_mclk"  2>/dev/null
echo Y > "$P/release_reset" 2>/dev/null
echo "$DEV" > "$DRV/unbind" 2>/dev/null; sleep 1
echo "$DEV" > "$DRV/bind"   2>/dev/null; sleep 2

mc=$(grep -E 'camss_mclk1_clk ' /sys/kernel/debug/clk/clk_summary | awk '{print $2}')
bound=no; [ -e "$DRV/$DEV" ] && bound=yes
echo "device $DEV bus $BUS bound=$bound mclk_en=$mc"
[ "$bound" = yes ] && [ "$mc" = 1 ] || { echo "NOT READY, aborting"; exit 1; }

r() { i2ctransfer -f -y "$BUS" "$@" 2>/dev/null | tr -d ' '; }

echo
echo "=== sensor 0x56 ==="
echo "A. repeated START  : $(r w2@0x56 0x00 0x00 r2) $(r w2@0x56 0x00 0x00 r2)"
for i in 1 2 3; do
	r w2@0x56 0x00 0x00 >/dev/null
	printf 'B. write+STOP then read : %s\n' "$(r r2@0x56)"
done
r w2@0x56 0x00 0x00 >/dev/null
echo "C. two 1-byte reads: $(r r1@0x56) $(r r1@0x56)"
echo "D. 1-byte reg addr : $(r w1@0x56 0x00 r2)"

echo
echo "=== control EEPROM 0x50, register 0x000d must be 0x5355 ==="
echo "A. repeated START  : $(r w2@0x50 0x00 0x0d r2)"
r w2@0x50 0x00 0x0d >/dev/null
echo "B. write+STOP+read : $(r r2@0x50)"
