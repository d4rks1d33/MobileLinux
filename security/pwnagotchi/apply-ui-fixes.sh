#!/bin/sh
# apply-ui-fixes.sh
#
# Two small patches to the pwnagotchi source tree (/opt/pwnagotchi) for the
# WEB UI on this port. They live in third-party source that the repo does not
# own, so they are reapplied here rather than committed to pwnagotchi:
#
#   1. The web frame felt static -- the "waiting Ns" countdown never ticked
#      down and nothing animated. That is ui.fps = 0 (event-driven frames
#      only). fps is set to 2 in config.toml (device-side), which is the real
#      fix; to match it, the browser poll interval in index.html is lowered
#      from 1000 ms to 500 ms so the <img> reloads twice a second.
#
#   2. With the dual-band adapter (rtl8821au) pwnagotchi hops 5 GHz channels,
#      whose numbers are three digits (149, 153, ...). The channel value at
#      x=0 then overlaps the "APS" label at x=28, so APS is moved to x=38.
#
# Both are idempotent: safe to run repeatedly, and they detect an already
# patched tree and skip. Re-run after any pwnagotchi update.
#
# Usage:  ./apply-ui-fixes.sh [/opt/pwnagotchi]

set -e

PWN="${1:-/opt/pwnagotchi}"
LAYOUT="$PWN/pwnagotchi/ui/hw/waveshare2in13_V4.py"
INDEX="$PWN/pwnagotchi/ui/web/templates/index.html"

[ -d "$PWN/pwnagotchi" ] || { echo "pwnagotchi source not found at $PWN"; exit 1; }

# --- Fix 1: web poll interval 1000 ms -> 500 ms --------------------------
# The active display here is waveshare_4 (physical display disabled), but the
# template is shared, so this just changes how often the browser reloads /ui.
if [ -f "$INDEX" ]; then
	if grep -q 'getTime(), 500)' "$INDEX"; then
		echo "  [1/2] web poll already 500 ms (skip)"
	elif grep -q 'getTime(), 1000)' "$INDEX"; then
		sed -i 's/getTime(), 1000)/getTime(), 500)/' "$INDEX"
		echo "  [1/2] web poll 1000 ms -> 500 ms"
	else
		echo "  [1/2] web poll line not found, leaving index.html untouched"
	fi
else
	echo "  [1/2] index.html not present (skip)"
fi

# --- Fix 2: move the APS widget right so 3-digit channels do not overlap --
if [ -f "$LAYOUT" ]; then
	if grep -q "self._layout\['aps'\] = (38, 0)" "$LAYOUT"; then
		echo "  [2/2] APS already at x=38 (skip)"
	elif grep -q "self._layout\['aps'\] = (28, 0)" "$LAYOUT"; then
		sed -i "s/self._layout\['aps'\] = (28, 0)/self._layout['aps'] = (38, 0)/" "$LAYOUT"
		echo "  [2/2] APS widget 28 -> 38 (room for 5 GHz channel numbers)"
	else
		echo "  [2/2] APS layout line not in the expected form, leaving it"
	fi
else
	echo "  [2/2] $LAYOUT not present (skip)"
fi

echo "Done. Restart pwnagotchi for the layout change to take effect:"
echo "  systemctl restart rhodep-pwnagotchi"
