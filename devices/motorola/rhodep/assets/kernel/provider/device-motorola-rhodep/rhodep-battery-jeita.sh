#!/bin/sh
# Cold side battery protection for the Motorola Moto G82 5G (rhodep).
#
# The hot side is handled by the kernel: the CW2217 gauge is registered as an OF
# thermal sensor and the SGM41542 charger as a cooling device, tied together by
# the battery-thermal zone in the device tree. That throttles the charge current
# at 45 C and stops charging at 50 C without any userspace involvement.
#
# What the thermal framework cannot express is the cold side: it only acts on
# rising temperature. Charging a lithium cell below 0 C plates metallic lithium,
# which is permanent damage, so that part is handled here.
#
# Threshold from the vendor DT (jeita_temp_t0 in discrete_charging_rhodep.dtsi).
set -u

GAUGE=/sys/class/power_supply/cw2217-battery
CHG=/sys/class/power_supply/bq256xx-charger
INTERVAL=30

# In tenths of a degree. 3 C of hysteresis so it does not flap at the edge.
COLD_STOP=0
COLD_RESUME=30

log() { echo "rhodep-jeita-cold: $*"; }

[ -r "$GAUGE/temp" ] || { log "no $GAUGE/temp, nothing to watch"; exit 0; }
[ -w "$CHG/charge_type" ] || { log "no $CHG/charge_type, nothing to do"; exit 0; }

# Empty means undecided, so the first state is always logged.
cold=""

while :; do
	t=$(cat "$GAUGE/temp" 2>/dev/null || echo "")
	if [ -z "$t" ]; then
		sleep "$INTERVAL"
		continue
	fi

	if [ "$cold" != "yes" ] && [ "$t" -lt "$COLD_STOP" ]; then
		cold=yes
		log "$((t / 10)) C: below 0 C, stopping charge"
		echo "N/A" > "$CHG/charge_type" 2>/dev/null
	elif [ "$cold" != "no" ] && [ "$t" -ge "$COLD_RESUME" ]; then
		# Only re-enable if we are the ones who stopped it. On a normal
		# boot the kernel has already enabled charging.
		if [ "$cold" = "yes" ]; then
			log "$((t / 10)) C: safe temperature, re-enabling charge"
			echo "Fast" > "$CHG/charge_type" 2>/dev/null
		fi
		cold=no
	fi

	sleep "$INTERVAL"
done
