#!/bin/bash
# Fast deathwatch, second pass: adds the modem's own signals.
#
# cr  is the SMEM crash reason via patch 0076, sampled at 10 Hz. The HANDOFF
#     sampled it at 4 Hz and always saw the placeholder; this is 2.5x that,
#     and the seven-second silence before the reset is where a late write
#     would land.
# w/f are the modem's watchdog (GIC 339) and fatal (smp2p 0) interrupt counts.
exec </dev/null
IPA=/sys/devices/platform/soc@0/5840000.ipa
RP=/sys/devices/platform/soc@0/6080000.remoteproc
echo on > $IPA/power/control

python3 /home/kali/qmiraw.py bands 0x4E 2>&1 | while read -r l; do echo "LTEW2: $l" > /dev/kmsg; done
echo "LTEW2: band 4 forced, watching at 10 Hz" > /dev/kmsg

i=0
while [ $i -lt 900 ]; do
    i=$((i+1))
    ipa=$(awk '/ipa$/{print $2}' /proc/interrupts)
    gsi=$(awk '/gsi$/{print $2}' /proc/interrupts)
    w=$(awk '$NF=="wdog" && /339/{s+=$2+$3+$4+$5+$6+$7+$8+$9} END{print s+0}' /proc/interrupts)
    f=$(awk '/smp2p-modem/ && /fatal/{s+=$2+$3+$4+$5+$6+$7+$8+$9} END{print s+0}' /proc/interrupts)
    mgs=$(cat $IPA/modem_gsi_state 2>/dev/null)
    cr=$(cat $RP/crash_reason_text 2>/dev/null | cut -c1-24)
    echo "LTEW2 $i t=$(cut -d' ' -f1 /proc/uptime) ipa=$ipa gsi=$gsi w=$w f=$f mgs=$mgs cr=[$cr]" > /dev/kmsg
    sleep 0.1
done
