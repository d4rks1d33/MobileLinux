#!/bin/sh
# rhodep: map which register addresses the rear-camera devices answer, and
# whether writes stick. A device that NAKs some addresses and not others is
# decoding them, which tells us far more than the values do.
BUS=4
DRV=/sys/bus/i2c/drivers/camdiag

rd() {	# $1 addr, $2 reg(hex4)  -> value or NAK
	hi=0x$(echo "$2" | cut -c1-2); lo=0x$(echo "$2" | cut -c3-4)
	v=$(i2ctransfer -f -y $BUS w2@$1 $hi $lo r2 2>/dev/null | tr -d ' ')
	echo "${v:-NAK}"
}

wr() {	# $1 addr, $2 reg(hex4), $3 val(hex4) -> OK/NAK
	hi=0x$(echo "$2" | cut -c1-2); lo=0x$(echo "$2" | cut -c3-4)
	vh=0x$(echo "$3" | cut -c1-2); vl=0x$(echo "$3" | cut -c3-4)
	if i2ctransfer -f -y $BUS w4@$1 $hi $lo $vh $vl >/dev/null 2>&1; then
		echo OK
	else
		echo NAK
	fi
}

# make sure the sensor is powered
[ -e "$DRV/4-0056" ] || { echo 4-0056 > "$DRV/bind" 2>/dev/null; sleep 2; }

REGS="0000 0001 0002 0010 0040 0080 00ff 0100 0103 0136 01ff 0200 0300
      03ff 0400 0800 0f00 1000 2000 3000 4000 5000 6000 6028 602a 6f12
      7000 8000 a000 c000 e000 f000 fffe ffff"

for a in 0x56 0x50 0x52 0x72; do
	echo "=== $a ==="
	line=""
	for r in $REGS; do
		line="$line $r:$(rd $a $r)"
	done
	echo "$line" | tr ' ' '\n' | grep -v '^$' | paste -d'  ' - - - - 
	echo
done

echo "=== write/readback on 0x56 ==="
for r in 0002 0100 6028; do
	before=$(rd 0x56 $r)
	w=$(wr 0x56 $r 1234)
	after=$(rd 0x56 $r)
	echo "  reg $r  before=$before  write=$w  after=$after"
done

echo
echo "=== control: same write/readback on the EEPROM 0x50 (expect no change) ==="
for r in 0002; do
	before=$(rd 0x50 $r)
	w=$(wr 0x50 $r 1234)
	after=$(rd 0x50 $r)
	echo "  reg $r  before=$before  write=$w  after=$after"
done
