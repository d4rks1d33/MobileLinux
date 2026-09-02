#!/bin/sh
# Enables the default routes before the audio server starts.
#
# The card's PCMs are DPCM front ends: hw:0,0 and hw:0,1 cannot be opened at all
# until a route is enabled. PipeWire opens its sink and source while starting
# up, so if this has not run first PipeWire exits and the machine has no audio
# whatsoever.
set -e

CARD="Motorola-Moto-G82-5G"

# The card only appears once the ADSP is up and apr has probed, which takes
# appreciably longer than this unit's own start, so wait properly.
# Keep the codec awake, or the headset jack cannot be detected at all: a
# suspended soundwire slave raises no MBHC interrupt. There is a udev rule for
# this too, but it depends on catching the bind event, so it is repeated here
# where the card is known to exist. Only the TX slave carries the MBHC block,
# and it is identified by having source ports; the RX one is left to suspend.
pin_codec_awake() {
	for d in /sys/bus/soundwire/devices/sdw:*/; do
		[ "$(cat "$d/dev-properties/source_ports" 2>/dev/null)" = "0x1f" ] || continue
		echo on > "$d/power/control" 2>/dev/null
	done
}

i=0
while [ $i -lt 450 ]; do
	if [ -e /proc/asound/card0/id ] && alsaucm -c "$CARD" list _verbs >/dev/null 2>&1; then
		alsaucm -c "$CARD" set _verb HiFi set _enadev Speaker set _enadev Mic
		pin_codec_awake
		exit 0
	fi
	i=$((i + 1))
	sleep 0.2
done

echo "sound card $CARD did not appear" >&2
exit 1
