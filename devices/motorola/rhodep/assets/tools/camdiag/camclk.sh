#!/bin/sh
# Does the sensor need MCLK to answer on I2C?
#
# Nothing here is hardcoded, deliberately. The i2c bus number is assigned
# dynamically and moves across reboots (the sensor was 4-0056 and came back as
# 1-0056), so a script with the bus baked in stops talking to the device and
# reports NAK everywhere -- which reads exactly like a dead sensor.
DRV=/sys/bus/i2c/drivers/camdiag

find_dev() { for d in "$DRV"/*-0056; do [ -e "$d" ] && basename "$d" && return; done; }

DEV=$(find_dev)
if [ -z "$DEV" ]; then
	# not bound yet: find the i2c device node instead
	for d in /sys/bus/i2c/devices/*-0056; do [ -e "$d" ] && DEV=$(basename "$d"); done
fi
[ -n "$DEV" ] || { echo "no *-0056 device found"; exit 1; }
BUS=${DEV%%-*}
echo "device $DEV on i2c bus $BUS"

state() {
	b=no; [ -e "$DRV/$DEV" ] && b=yes
	mc=$(grep -E 'camss_mclk1_clk ' /sys/kernel/debug/clk/clk_summary | awk '{print $2}')
	vio=$(for d in /sys/class/regulator/*/; do
		[ "$(cat "$d/name" 2>/dev/null)" = cam_vio ] && cat "$d/state"; done)
	printf 'bound=%s mclk_en=%s vio=%s' "$b" "$mc" "$vio"
}

rd() { i2ctransfer -f -y "$BUS" w2@"$1" 0x00 0x00 r2 2>/dev/null | tr -d ' '; }

cycle() {	# $1 = enable_mclk (Y/N)
	echo "$1" > /sys/module/camdiag/parameters/enable_mclk
	echo "$DEV" > "$DRV/unbind" 2>/dev/null
	sleep 1
	echo "$DEV" > "$DRV/bind" 2>/dev/null
	sleep 2
	s=$(rd 0x56); e=$(rd 0x50)
	printf '  enable_mclk=%s  %s  sensor=%-8s eeprom=%s\n' \
		"$1" "$(state)" "${s:-NAK}" "${e:-NAK}"
}

echo "start: $(state)"
cycle Y
cycle N
cycle Y
cycle N
