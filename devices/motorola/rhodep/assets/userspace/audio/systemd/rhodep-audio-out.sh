#!/bin/sh
# Selects the audio output on the Moto G82 5G.
#
# Normally this is driven by rhodep-jack-watch, which follows the jack, so it is
# only needed by hand to override the choice: to force the earpiece, or to keep
# the speaker while something is plugged in.
#
# Speaker, Earpiece and Headphones are all MultiMedia1 and share hw:0,0, so
# switching is purely a UCM device change and the PipeWire sink is untouched.
#
# Known limitation, switching TO the speaker while a stream is already playing
# leaves it silent until that stream is closed and reopened. It is not this
# script: ASoC powers the DAPM widgets synchronously before it connects and
# triggers the new backend, so the Awinic amplifiers try to lock their PLL to an
# I2S clock that is still ~300 ms away, fail, and stay off. Retrying inside the
# amplifier driver cannot help, because the retry loop is what delays the
# backend start in the first place. Anything started afterwards plays normally.
set -e

CARD="Motorola-Moto-G82-5G"

usage() {
	echo "usage: ${0##*/} speaker|earpiece|headphones|status" >&2
	exit 2
}

# The state is read back from the card, not from UCM. UCM's notion of which
# device is enabled lives in the alsa-lib instance and dies with the process, so
# a separate invocation always reports nothing enabled.
ctl() {
	amixer -c 0 cget name="$1" 2>/dev/null | sed -n 's/^  : values=//p'
}

current() {
	if [ "$(ctl 'RX_CODEC_DMA_RX_1 Audio Mixer MultiMedia1')" = "on" ]; then
		echo "headphones"
	elif [ "$(ctl 'SEC_MI2S_RX Audio Mixer MultiMedia1')" = "on" ]; then
		# Speaker and Earpiece differ only in whether the loudspeaker
		# amplifier is muted and which Awinic profile the earpiece uses.
		if [ "$(ctl 'Speaker PCM Playback Volume')" = "0" ]; then
			echo "earpiece"
		else
			echo "speaker"
		fi
	else
		echo "none"
	fi
}

# One invocation: the verb tears the output side down and the device brings its
# own up, so this is correct regardless of what was playing before.
select_output() {
	alsaucm -c "$CARD" set _verb HiFi set _enadev "$1"
	echo "output: $(current)"
}

case "$1" in
	speaker)    select_output Speaker ;;
	earpiece)   select_output Earpiece ;;
	headphones) select_output Headphones ;;
	status)     echo "output: $(current)" ;;
	*)          usage ;;
esac
