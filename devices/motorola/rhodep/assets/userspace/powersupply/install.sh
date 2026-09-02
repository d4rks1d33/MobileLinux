#!/bin/sh
# Make the Powersupply app read the real battery. Run as root.
#
# The phone exposes three power supplies:
#
#   bq256xx-battery   type=Battery  scope=Device   no capacity, no status
#   bq256xx-charger   type=USB                     status=Charging
#   cw2217-battery    type=Battery                 capacity=95, status=Discharging
#
# The first is the charger's own view of a battery, and it carries nothing.
# Patch 0023 gives it scope=Device precisely so that userspace knows to skip it
# -- that is the kernel's documented way of saying "this belongs to a device,
# it is not the system battery", and it is why UPower already ignores it.
#
# powersupply-gtk does not look at scope. get_supply_types() groups everything
# by `type`, sorts each group alphabetically and the UI then takes [0]:
#
#	result[type].append(path)
#	result[type] = list(sorted(result[type]))
#	...
#	battery_capacity = os.path.join(supplies['bat'][0], 'capacity')
#
# "bq256xx-battery" sorts before "cw2217-battery", so it always wins and every
# field reads N/A. Nothing is wrong with the driver or the fuel gauge; the app
# is looking at the wrong one.
#
# The fix is to skip supplies whose scope is Device, which is both the correct
# rule and upstreamable. Deliberately not "prefer cw2217-battery": hardcoding a
# gauge's name would break the next time the hardware or the driver changes,
# and would not help anyone else's phone.

set -e

APP=/usr/share/powersupply-gtk/powersupply/__main__.py
PROTECT=/usr/local/sbin/rhodep-protect-files

if [ ! -f "$APP" ]; then
	echo "powersupply-gtk is not installed, nothing to do"
	exit 0
fi

if grep -q "scope is Device" "$APP"; then
	echo "already patched: $APP"
else
	[ -x "$PROTECT" ] && "$PROTECT" release "$APP" 2>/dev/null || true

	cp -a "$APP" "$APP.rhodep-orig"

	python3 - "$APP" <<'PY'
import sys

path = sys.argv[1]
src = open(path).read()

anchor = """            if not os.path.isfile(os.path.join(path, 'type')):
                print("Ignoring {} because it doesn't have a `type` file".format(path))
                continue
"""

added = anchor + """
            # A supply with scope=Device is not the system battery: it belongs
            # to some other device. On this phone the charger exposes an empty
            # one that way, and since the groups are sorted alphabetically
            # "bq256xx-battery" would come before "cw2217-battery" and every
            # field would read N/A. UPower skips these for the same reason.
            scope_file = os.path.join(path, 'scope')
            if os.path.isfile(scope_file):
                with open(scope_file) as handle:
                    if handle.read().strip() == 'Device':
                        print("Ignoring {} because its scope is Device".format(path))
                        continue
"""

if anchor not in src:
    raise SystemExit("anchor not found in %s -- upstream changed, patch by hand" % path)

open(path, "w").write(src.replace(anchor, added, 1))
print("patched %s" % path)
PY

	python3 -m py_compile "$APP"
	echo "syntax OK"
fi

if [ -x "$PROTECT" ]; then
	"$PROTECT" register powersupply 0644 "$APP" 2>/dev/null || true
fi

echo "powersupply: $(grep -c 'scope is Device' "$APP") guard in place"
