#!/bin/sh
# Expose modem/adsp/cdsp firmware from the modem_a partition and start the
# remoteprocs. The kernel asks for <base>.mbn but the vendor partition ships
# <base>.mdt + .bNN segments, so symlink .mbn -> .mdt.
#
# Everything must land under the firmware_class search path, not a hardcoded
# /lib/firmware. On this image that path is /lib/firmware/postmarketos, and
# pd-mapper in particular derives where to look for its .jsn files from the
# remoteproc firmware path, which is resolved against firmware_class - so the
# .jsn links have to be there too or pd-mapper reports "no pd maps available".
FWBASE=$(cat /sys/module/firmware_class/parameters/path 2>/dev/null)
[ -n "$FWBASE" ] || FWBASE=/lib/firmware
FW="$FWBASE/qcom/sm6375/motorola/rhodep"
IMG=/readonly/firmware/image
mkdir -p "$FW"
[ -d "$IMG" ] || exit 0
for base in modem adsp cdsp wpss; do
    [ -e "$IMG/$base.mdt" ] || continue
    ln -sf "$IMG/$base.mdt" "$FW/$base.mbn"
    ln -sf "$IMG/$base.mdt" "$FW/$base.mdt"
    for seg in "$IMG/$base".b*; do
        [ -e "$seg" ] && ln -sf "$seg" "$FW/$(basename "$seg")"
    done
done

# pd-mapper reads /sys/class/remoteproc/<n>/firmware, derives the directory of
# that firmware, and looks for the protection-domain .jsn files *inside it* -
# i.e. this same directory. The modem_a partition already ships them, so link
# them in. Without this the AP publishes no SERVREG locator, no protection
# domain ever starts, and anything that depends on one (WLAN's wlan_pd, the
# sensor core's sensor_pd) silently never comes up.
for jsn in "$IMG"/*.jsn; do
    [ -e "$jsn" ] && ln -sf "$jsn" "$FW/$(basename "$jsn")"
done
# Start the remoteprocs (adsp, then modem, then cdsp) now that firmware is present.
for name_want in adsp modem cdsp; do
    for r in /sys/class/remoteproc/remoteproc*; do
        [ "$(cat "$r/name" 2>/dev/null)" = "$name_want" ] || continue
        state=$(cat "$r/state" 2>/dev/null)
        if [ "$state" = "offline" ]; then
            echo start > "$r/state" 2>/dev/null || true
            sleep 3
        fi
    done
done
exit 0
