#!/bin/bash
# Publish only QMI service ids that nobody provides on any node, and bridge the
# output into /dev/kmsg so that whatever the modem asks for during an LTE
# attach survives the reset in ramoops.
#
# The list is computed BEFORE publishing. Getting that order wrong once cost a
# session: service 69 is the WLAN firmware service the modem provides, ath10k
# binds to it, and offering a second one takes the WiFi link down.
exec </dev/null

TAKEN=$(qrtr-lookup 2>/dev/null | awk '{print $1}' | grep -E '^[0-9]+$' | sort -nu)
LIST=""
for i in $(seq 1 235) 256 271 312 770 771 4097 4098; do
    if ! echo "$TAKEN" | grep -qx "$i"; then LIST="$LIST $i"; fi
done
echo "SVCLTE: taken=$(echo $TAKEN | tr '\n' ' ')" > /dev/kmsg
echo "SVCLTE: publishing $(echo $LIST | wc -w) ids" > /dev/kmsg

stdbuf -oL /home/kali/service-probe --answer $LIST 2>&1 \
  | stdbuf -oL sed -u 's/^/SVCLTE: /' > /dev/kmsg
