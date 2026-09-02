#!/bin/sh
# Terminal keys for the Plasma Mobile keyboard on motorola-rhodep.
#
# Adds a third page to the symbols layout with Esc, Tab, the arrows and real
# Ctrl combinations, so the terminal is usable without an external keyboard.
# Reach it with the &123 key, then 1/3 twice.
#
# Two things about this are not obvious, and both cost an attempt each:
#
#  1. plasma-keyboard does NOT read /usr/share/plasma/keyboard/layouts. That
#     directory belongs to qml6-module-qtquick-virtualkeyboard, while
#     plasma-keyboard prefers the layouts compiled into its own qrc - see
#     "prefer :/qt/qml/org/kde/plasma/keyboard/" in the binary. A symbols.qml
#     dropped there does nothing at all. The way in is
#     QT_VIRTUALKEYBOARD_LAYOUT_PATH, and that replaces the whole layout set,
#     so the stock tree is copied first and one file in it is changed.
#
#  2. The variable has to reach the process KWin starts, and editing
#     Exec= in Plasma's own .desktop file is not enough - KWin keeps launching
#     the binary it already knows. It needs a .desktop file of our own that
#     kwinrc points at, and KWin reads that setting ONLY at startup, so the
#     change takes effect on the next login.
#
# Usage:  sudo ./install.sh          then log out and back in
#         sudo ./install.sh --revert
set -e

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
DEST=/usr/local/share/rhodep-keyboard/layouts
STOCK_LAYOUTS=/usr/share/plasma/keyboard/layouts
WRAPPER=/usr/local/bin/rhodep-plasma-keyboard
STOCK_DESKTOP=/usr/share/applications/org.kde.plasma.keyboard.desktop
OUR_DESKTOP=/usr/share/applications/rhodep-terminal-keyboard.desktop
PROTECT=/usr/local/sbin/rhodep-protect-files

[ "$(id -u)" = 0 ] || { echo "run me as root" >&2; exit 1; }

# The config belongs to the desktop user, not to root.
user=${SUDO_USER:-kali}
id "$user" >/dev/null 2>&1 || { echo "no such user: $user" >&2; exit 1; }

# HOME is set explicitly: under sudo it would otherwise be root's, and the
# setting would land in a kwinrc that KWin never reads.
home=$(getent passwd "$user" | cut -d: -f6)
kwin_set() {
	su "$user" -c "HOME='$home' kwriteconfig6 --file kwinrc --group Wayland --key InputMethod '$1'" 2>/dev/null
	got=$(su "$user" -c "HOME='$home' kreadconfig6 --file kwinrc --group Wayland --key InputMethod" 2>/dev/null)
	[ "$got" = "$1" ] || { echo "could not set kwinrc InputMethod (got '$got')" >&2; return 1; }
}

if [ "$1" = "--revert" ]; then
	[ -x "$PROTECT" ] && "$PROTECT" release "$WRAPPER" "$OUR_DESKTOP" \
		2>/dev/null || true
	[ -d "$DEST" ] && chattr -R -i "$DEST" 2>/dev/null || true
	kwin_set "$STOCK_DESKTOP"
	rm -f "$OUR_DESKTOP" "$WRAPPER"
	rm -rf /usr/local/share/rhodep-keyboard
	# The first attempt wrote here; make sure nothing is left behind.
	rm -f "$STOCK_LAYOUTS/en_US/symbols.qml"
	[ -e "$STOCK_LAYOUTS/en_US/symbols.fallback.orig" ] && \
		mv -f "$STOCK_LAYOUTS/en_US/symbols.fallback.orig" "$STOCK_LAYOUTS/en_US/symbols.fallback"
	[ -e "$STOCK_DESKTOP.rhodep-orig" ] && mv -f "$STOCK_DESKTOP.rhodep-orig" "$STOCK_DESKTOP"
	echo "reverted; log out and back in to get the stock keyboard."
	exit 0
fi

[ -d "$STOCK_LAYOUTS/fallback" ] || {
	echo "no stock layouts in $STOCK_LAYOUTS" >&2
	echo "install qml6-module-qtquick-virtualkeyboard first." >&2; exit 1; }
[ -x /usr/bin/plasma-keyboard ] || { echo "/usr/bin/plasma-keyboard is missing" >&2; exit 1; }

# A previous run registers these with rhodep-protect-files, which sets the
# immutable bit; without lifting it first the tree cannot be replaced.
[ -x "$PROTECT" ] && "$PROTECT" release "$WRAPPER" "$OUR_DESKTOP" \
	"$STOCK_DESKTOP" \
	/usr/local/sbin/rhodep-keyboard-enforce \
	/etc/systemd/system/rhodep-keyboard-enforce.service \
	/etc/systemd/system/rhodep-keyboard-enforce.timer 2>/dev/null || true
[ -d "$DEST" ] && chattr -R -i "$DEST" 2>/dev/null || true

# An earlier version of this script edited Plasma's own .desktop file instead of
# shipping one. Undo that if it is still there, or the stock launcher keeps
# pointing at our wrapper for good - and if it was registered for protection it
# is immutable, which is why the release above lists it too.
if [ -e "$STOCK_DESKTOP.rhodep-orig" ]; then
	cp -a "$STOCK_DESKTOP.rhodep-orig" "$STOCK_DESKTOP" && rm -f "$STOCK_DESKTOP.rhodep-orig"
	L=/usr/local/share/rhodep/protect.d/keyboard-conf.list
	if [ -e "$L" ]; then
		grep -v "[[:space:]]$STOCK_DESKTOP\$" "$L" > "$L.tmp" 2>/dev/null && mv "$L.tmp" "$L"
	fi
	rm -f "/usr/local/share/rhodep/files/$(printf '%s' "$STOCK_DESKTOP" | tr '/' '%')"
fi
# Same for the first attempt's stray layout file, which plasma-keyboard never
# read anyway.
rm -f "$STOCK_LAYOUTS/en_US/symbols.qml"
[ -e "$STOCK_LAYOUTS/en_US/symbols.fallback.orig" ] && \
	mv -f "$STOCK_LAYOUTS/en_US/symbols.fallback.orig" "$STOCK_LAYOUTS/en_US/symbols.fallback"
: 

# Whole tree, because QT_VIRTUALKEYBOARD_LAYOUT_PATH replaces the set entirely
# and every other locale still has to resolve through fallback/.
rm -rf "$DEST"
install -d "$(dirname "$DEST")"
cp -a "$STOCK_LAYOUTS" "$DEST"

# Every layout, not just one. 38 of the 44 locales ship a main.qml of their own,
# so patching only fallback/ meant that switching to Spanish for the sake of the
# "n" key cost you Esc, Tab, Ctrl and the arrows. generate.py transforms the whole
# tree and verifies each file with qmllint, leaving anything it cannot verify
# exactly as Qt shipped it - a layout that will not load is a phone you cannot
# type on.
command -v python3 >/dev/null 2>&1 || { echo "python3 is needed to build the layouts" >&2; exit 1; }

QMLLINT=/usr/lib/qt6/bin/qmllint
QMLDIR=$(ls -d /usr/lib/*/qt6/qml 2>/dev/null | head -1)
set -- "$STOCK_LAYOUTS" "$DEST"
[ -x "$QMLLINT" ] && set -- "$@" --qmllint "$QMLLINT"
[ -n "$QMLDIR" ] && set -- "$@" --qml-import "$QMLDIR"
python3 "$here/generate.py" "$@"

install -D -m 0755 /dev/stdin "$WRAPPER" <<'WRAP'
#!/bin/sh
# Both variables are needed, and the first one is the whole trick.
#
# plasma-keyboard carries its own copy of the layouts in its qrc and uses those
# by default, ignoring QT_VIRTUALKEYBOARD_LAYOUT_PATH entirely.
# PLASMA_KEYBOARD_USE_QT_LAYOUTS is the switch that makes it take Qt Virtual
# Keyboard's layouts instead - and only then does the path below mean anything.
# Both names sit next to each other in `strings /usr/bin/plasma-keyboard`.
#
# Verified with strace: with both set, the process opens
# /usr/local/share/rhodep-keyboard/layouts/fallback/main.qml; with only the
# path set, it opens nothing from that tree.
export PLASMA_KEYBOARD_USE_QT_LAYOUTS=1
export QT_VIRTUALKEYBOARD_LAYOUT_PATH=/usr/local/share/rhodep-keyboard/layouts
exec /usr/bin/plasma-keyboard "$@"
WRAP

# Our own launcher, so Plasma's file stays untouched and upgrades cannot fight
# us over it.
sed -e "s|^Exec=.*|Exec=$WRAPPER|" \
    -e "s|^Name=.*|Name=Plasma Keyboard (terminal keys)|" \
    "$STOCK_DESKTOP" > "$OUR_DESKTOP"
chmod 0644 "$OUR_DESKTOP"
grep -q "^Exec=$WRAPPER\$" "$OUR_DESKTOP" || { echo "failed to write $OUR_DESKTOP" >&2; exit 1; }

su "$user" -c "XDG_RUNTIME_DIR=/run/user/\$(id -u) kbuildsycoca6 --noincremental" >/dev/null 2>&1 || true
kwin_set "$OUR_DESKTOP"

# The language key is greyed out until this is set: plasmakeyboardrc ships with
# enabledLocales empty, and plasma-keyboard reads an empty list as "one language
# only". Without it, reaching the Spanish layout - and its ñ - is impossible from
# the keyboard itself.
install -D -m 0644 "$here/enabled-locales" /usr/local/share/rhodep/keyboard/enabled-locales
locales=$(grep -v '^#' "$here/enabled-locales" | grep -v '^$' | head -1)
current=$(su "$user" -c "HOME='$home' kreadconfig6 --file plasmakeyboardrc --group General --key enabledLocales" 2>/dev/null)
if [ -z "$current" ]; then
	su "$user" -c "HOME='$home' kwriteconfig6 --file plasmakeyboardrc --group General --key enabledLocales '$locales'" 2>/dev/null \
		&& echo "keyboard languages set to $locales"
else
	echo "keyboard languages already set to $current, left alone"
fi

# The two settings that cannot be made immutable - kwinrc, which KWin rewrites,
# and .zshrc, which belongs to the user - get a boot-time repair instead.
install -D -m 0755 "$here/rhodep-keyboard-enforce" /usr/local/sbin/rhodep-keyboard-enforce
install -D -m 0644 "$here/README.md" /usr/local/share/rhodep/keyboard/README.md
if [ -d /run/systemd/system ]; then
	install -D -m 0644 "$here/systemd/rhodep-keyboard-enforce.service" \
		/etc/systemd/system/rhodep-keyboard-enforce.service
	install -D -m 0644 "$here/systemd/rhodep-keyboard-enforce.timer" \
		/etc/systemd/system/rhodep-keyboard-enforce.timer
	systemctl daemon-reload 2>/dev/null || true
	systemctl enable --now rhodep-keyboard-enforce.service 2>/dev/null || true
	systemctl enable --now rhodep-keyboard-enforce.timer 2>/dev/null || true
fi

if [ -x "$PROTECT" ]; then
	"$PROTECT" register keyboard 0755 "$WRAPPER" /usr/local/sbin/rhodep-keyboard-enforce 2>/dev/null || true
	"$PROTECT" register keyboard-conf 0644 "$OUR_DESKTOP" \
		/etc/systemd/system/rhodep-keyboard-enforce.service \
		/etc/systemd/system/rhodep-keyboard-enforce.timer 2>/dev/null || true
	# Every generated layout, so a stray rm or a bad edit is both refused and
	# recoverable from the snapshot.
	find "$DEST" \( -name main.qml -o -name symbols.qml \) -print | while read -r f; do
		"$PROTECT" register keyboard-layouts 0644 "$f" 2>/dev/null || true
	done
fi

# The layout tree is deleted and rebuilt above, which pulls the files out from
# under a running keyboard: it keeps its old, now-deleted layouts mapped and fails
# to load anything it has not already cached, so the panel stops appearing. Seen
# for real, with 66 deleted files still mapped by the process. KWin restarts the
# input method by itself, so killing it is enough.
if [ -d /run/systemd/system ] && pgrep -f plasma-keyboard >/dev/null 2>&1; then
	pkill -f '/usr/bin/plasma-keyboard' 2>/dev/null || true
	echo "keyboard restarted, so it picks up the rebuilt layouts"
fi

cat <<'MSG'
installed.

Log out and back in - KWin reads the input method setting only at startup.
Then: &123, and 1/3 twice.

If the keyboard does not come back, over SSH:
  sudo ./install.sh --revert
MSG
