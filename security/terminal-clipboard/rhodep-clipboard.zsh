# Clipboard keys for the terminal on motorola-rhodep.
#
# Why this lives in the shell and not in the keyboard: the on-screen keyboard
# cannot do it. Ctrl+Shift+C/V never reach the terminal, because plasma-keyboard
# sends the protocol's modifier field as a hardcoded 0 and KWin ignores it
# anyway, deriving modifiers from the keymap instead. And the keyboard cannot
# read the clipboard either: it has no keyboard focus, so KWin never sends it a
# selection offer - a protocol trace shows not one data_offer.
#
# What the keyboard can do is send bytes. So:
#
#   Ctrl+V         -> 0x16          -> paste
#   Ctrl+O         -> 0x0f          -> copy
#   Ctrl+Shift+C   -> Esc then "C"  -> copy
#   Ctrl+Shift+V   -> Esc then "V"  -> paste
#
# Ctrl+Shift+C has to be a different sequence rather than a control character,
# because control characters ignore case: Ctrl+C and Ctrl+Shift+C are both 0x03,
# and 0x03 has to stay as it is - that is how you cancel a running command.
#
# ^V was quoted-insert and ^O was accept-line-and-down-history. Neither is
# something anyone uses on a phone.
#
# These are ZLE widgets, so they work at the prompt. While a program is running
# it receives the bytes itself, and Ctrl+C keeps cancelling it, which is the
# behaviour you want.

# Under sudo, su or a root login the session's Wayland socket is not in the
# environment, so wl-paste has nothing to talk to and copy/paste would silently
# do nothing. Find the logged-in user's runtime directory instead - root can read
# it. Costs nothing in a normal shell, where both variables are already set.
rhodep-wayland-env() {
	[[ -n ${WAYLAND_DISPLAY:-} && -n ${XDG_RUNTIME_DIR:-} ]] && return 0
	local dir sock
	for dir in /run/user/*(N/); do
		for sock in $dir/wayland-<->(N) $dir/wayland-*(N); do
			[[ $sock == *.lock ]] && continue
			export XDG_RUNTIME_DIR=$dir
			export WAYLAND_DISPLAY=${sock:t}
			return 0
		done
	done
	return 1
}

if (( $+commands[wl-paste] )); then
	rhodep-paste-clipboard() {
		rhodep-wayland-env || return
		local text
		text=$(wl-paste --no-newline 2>/dev/null) || return
		[[ -n $text ]] || return
		LBUFFER+=$text
	}
	zle -N rhodep-paste-clipboard
	bindkey '^V' rhodep-paste-clipboard
	bindkey '\eV' rhodep-paste-clipboard
fi

if (( $+commands[wl-copy] )); then
	# Selected text first - that is what someone means by copy after dragging a
	# finger across the screen, and a terminal puts a selection in the primary
	# buffer. With nothing selected, fall back to the command line, which is the
	# only other thing here that could be meant.
	rhodep-copy-clipboard() {
		rhodep-wayland-env || return
		local sel
		sel=$(wl-paste --primary --no-newline 2>/dev/null)
		if [[ -n $sel ]]; then
			print -rn -- $sel | wl-copy
		else
			print -rn -- $BUFFER | wl-copy
		fi
	}
	zle -N rhodep-copy-clipboard
	bindkey '^O' rhodep-copy-clipboard
	bindkey '\eC' rhodep-copy-clipboard
fi
