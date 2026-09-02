#!/bin/sh
# Claude Code on opencode Zen's free models.
set -e

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
[ "$(id -u)" = 0 ] || { echo "run me as root" >&2; exit 1; }

user=${SUDO_USER:-kali}
home=$(getent passwd "$user" | cut -d: -f6) || { echo "no such user: $user" >&2; exit 1; }

command -v node >/dev/null 2>&1 || { echo "node is needed for the proxy" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is needed to read the model cache" >&2; exit 1; }

PROXY=/usr/local/libexec/rhodep/anthropic-proxy.mjs
PICKER=/usr/local/libexec/rhodep/claude-free-pick.mjs
LIBEXEC=/usr/local/libexec/rhodep
WRAPPER=/usr/local/bin/claude-free
PROTECT=/usr/local/sbin/rhodep-protect-files

[ -x "$PROTECT" ] && "$PROTECT" release "$PROXY" "$PICKER" "$WRAPPER" 2>/dev/null || true

# The proxy used to be Zen-only and named for it.
OLD_PROXY=/usr/local/libexec/rhodep/zen-anthropic-proxy.mjs
[ -x "$PROTECT" ] && "$PROTECT" release "$OLD_PROXY" 2>/dev/null || true
rm -f "$OLD_PROXY"

install -D -m 0644 "$here/anthropic-proxy.mjs" "$PROXY"
install -D -m 0755 "$here/claude-free-pick.mjs" "$PICKER"
install -D -m 0755 "$here/claude-free" "$WRAPPER"

# The picker uses @inquirer/prompts. Install it next to the picker so it resolves
# regardless of the caller's cwd. npm is only needed at install time.
install -D -m 0644 "$here/picker-package.json" "$LIBEXEC/package.json"
if command -v npm >/dev/null 2>&1; then
	( cd "$LIBEXEC" && npm install --no-audit --no-fund --silent ) \
		&& echo "installed the picker's node dependency (@inquirer/prompts)" \
		|| echo "warning: npm install failed; --model will use the static list"
else
	echo "npm not found; --model will use the static list until it is installed"
fi

# `claude` itself is not on root's PATH, because it installs and updates itself
# inside a home directory. This shim finds it wherever it lives.
#
# It does mean a root shell runs a binary the desktop user can replace. On a
# single-user machine where that user already has sudo it grants nothing new;
# CLAUDE_FREE_NO_SHIM=1 skips it if that trade is not acceptable here.
if [ "${CLAUDE_FREE_NO_SHIM:-0}" = "1" ]; then
	echo "skipping the /usr/local/bin/claude shim (CLAUDE_FREE_NO_SHIM=1)"
elif [ -e /usr/local/bin/claude ] && ! grep -q "rhodep" /usr/local/bin/claude 2>/dev/null; then
	echo "/usr/local/bin/claude exists and is not ours; left alone"
else
	[ -x "$PROTECT" ] && "$PROTECT" release /usr/local/bin/claude 2>/dev/null || true
	install -D -m 0755 "$here/claude-shim" /usr/local/bin/claude
	echo "claude is now on root's PATH too"
fi
install -D -m 0644 "$here/README.md" /usr/local/share/rhodep/claude-free/README.md

# A config file the user owns, so model choices survive a reinstall.
conf=$home/.config/rhodep/claude-free.conf
if [ ! -e "$conf" ]; then
	install -d -m 0755 -o "$user" -g "$user" "$home/.config/rhodep"
	cat > "$conf" <<'CONF'
# Models for claude-free, as provider/name. See `claude-free --model`.
#
# opencode/*-free needs no credential. Anything else uses the key opencode holds
# for that provider, so `opencode auth login` is what adds more.
CLAUDE_FREE_MODEL=opencode/nemotron-3-ultra-free
CLAUDE_FREE_SMALL_MODEL=opencode/ling-3.0-tiny-free
CLAUDE_FREE_PORT=8787
CONF
	chown "$user":"$user" "$conf"
	echo "wrote $conf"
else
	echo "$conf exists, left alone"
fi

if [ -x "$PROTECT" ]; then
	"$PROTECT" register claude-free 0755 "$WRAPPER" 2>/dev/null || true
	[ -e /usr/local/bin/claude ] && "$PROTECT" register claude-free 0755 /usr/local/bin/claude 2>/dev/null || true
	"$PROTECT" register claude-free-conf 0644 "$PROXY" 2>/dev/null || true
	"$PROTECT" register claude-free 0755 "$PICKER" 2>/dev/null || true
fi

cat <<'MSG'
installed.

Works in a root shell too: claude and claude-free both resolve, and opencode's
keys, catalog and model choice come from the desktop user's home.

  claude-free                 Claude Code on a free model
  claude-free --model         pick one interactively (type to filter, arrows)
  claude-free --model X       set it directly to provider/name
  claude-free --status        is the proxy up
  claude-free --stop          stop it

Models are set in ~/.config/rhodep/claude-free.conf
MSG
