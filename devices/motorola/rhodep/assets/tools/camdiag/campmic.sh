#!/bin/sh
# Dump the FAN53870 camera PMIC with the camera rails on and off.
#
# Hypothesis under test: the vendor gives the PMIC vin1-supply = <&S6A>, and the
# LDO voltage ranges split 600000-1800000 (ldo1, ldo2) against 1200000-4300000
# (ldo3-ldo7), which is a VIN1/VIN2 input split. LDO7 (cam_vio) is in the
# high-voltage group and demonstrably works -- the EEPROM answers. LDO1
# (cam_vdig, the sensor's digital core) is in the low group, fed from S6, and
# S6 reads disabled with no users on this port.
DRV=/sys/bus/i2c/drivers/camdiag

# Find it by driver, never by address: the AW88261 earpiece amplifier also sits
# at 0x35, and the i2c bus numbers move across reboots. Picking by address read
# the audio amplifier's registers and reported them as the camera PMIC's.
PMIC=$(ls -d /sys/bus/i2c/drivers/fan53870/*-* 2>/dev/null | head -1)
[ -n "$PMIC" ] || { echo "fan53870 not bound"; exit 1; }
PMIC=$(basename "$PMIC")
PBUS=${PMIC%%-*}

DEV=
for d in /sys/bus/i2c/devices/*-0056; do [ -e "$d" ] && DEV=$(basename "$d"); done
[ -n "$DEV" ] || { echo "no sensor node"; exit 1; }

dump() {
	printf '   '
	for r in 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f; do
		printf '%s ' "$(i2cget -f -y "$PBUS" 0x35 0x$r 2>/dev/null || echo '--')"
	done
	echo
}

echo "camera PMIC at $PMIC (bus $PBUS), sensor at $DEV"
echo "reg:     00   01   02   03   04   05   06   07   08   09   0a   0b   0c   0d   0e   0f"

echo "$DEV" > "$DRV/unbind" 2>/dev/null; sleep 1
echo "rails OFF:"; dump

echo "$DEV" > "$DRV/bind" 2>/dev/null; sleep 2
echo "rails ON :"; dump

echo
echo "S6 (the vendor's vin1 for this PMIC):"
for d in /sys/class/regulator/*/; do
	[ "$(cat "$d/name" 2>/dev/null)" = s6 ] && \
		echo "   state=$(cat "$d/state") uV=$(cat "$d/microvolts") users=$(cat "$d/num_users")"
done
echo
echo "LDO enable bits (reg 0x03), one per LDO, bit n = LDOn+1:"
v=$(i2cget -f -y "$PBUS" 0x35 0x03 2>/dev/null)
echo "   0x03 = $v"
echo "vsel registers 0x04..0x0a are LDO1..LDO7"
