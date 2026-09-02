#!/bin/sh
# Blocks until the sound card is actually usable, for PipeWire to start after.
#
# PipeWire declares its sink statically (see pipewire/51-rhodep-sink.conf) and
# exits if it cannot open it, which on this port is not a hypothetical: the card
# only appears once the ADSP is up, roughly twenty seconds into boot, and the
# PCM cannot be opened before a mixer route exists on top of that.
#
# Without this, PipeWire loses that race, fails five times in a couple of
# seconds, trips systemd's start limit and then stays dead until someone logs in
# over the network and restarts it by hand. The drop-in that calls this also
# clears the start limit, so a failure past the timeout still gets retried
# instead of being final.
CARD=/proc/asound/card0
DEADLINE=$(( $(date +%s) + 120 ))

route_is_up() {
	# Either backend counts: speaker on Secondary MI2S, or headphones on the
	# codec DMA. hw:0,0 opens once one of them is routed.
	for c in 'SEC_MI2S_RX Audio Mixer MultiMedia1' \
		 'RX_CODEC_DMA_RX_1 Audio Mixer MultiMedia1'; do
		if amixer -c 0 cget name="$c" 2>/dev/null | grep -q ': values=on'; then
			return 0
		fi
	done
	return 1
}

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
	if [ -e "$CARD/id" ] && route_is_up; then
		exit 0
	fi
	sleep 0.5
done

# Do not fail: a hard failure here would stop PipeWire from starting at all, and
# a PipeWire with no working sink is still better than no PipeWire.
echo "rhodep: sound card not ready after 120s, starting anyway" >&2
exit 0
