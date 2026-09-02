# Typing in Electron apps (VS Code)

VS Code left you with a text field, a cursor and no keyboard. The measurements,
in the order they were made, because two of them ruled out the obvious answers.

**It was on XWayland.** `code-oss` was started as plain `code-oss %F`, with no
ozone flags, so it fell back to XWayland -- and XWayland clients cannot speak
the Wayland text-input protocol at all. KWin says so directly:

	activeClientSupportsTextInput    false

**Forcing the keyboard does not help.** KWin exposes
`org.kde.kwin.VirtualKeyboard.forceActivate`, which raises the keyboard over
anything. With VS Code focused it does nothing -- `visible` stays false for
three seconds of sampling -- because KWin will not raise a keyboard over a
window that cannot receive text. So a "show keyboard" button was never going to
be the answer, which is worth knowing before writing one.

**Wayland alone is not enough either.** Adding
`--ozone-platform-hint=auto --enable-features=UseOzonePlatform` moved it onto
Wayland -- confirmed by the process holding two wayland connections and zero
X11 ones -- and the keyboard still never appeared. 60 samples over two minutes,
`activeClientSupportsTextInput` false in every one.

**Two more flags were needed, and both of them.** `--enable-wayland-ime` turns
on input-method support and `--wayland-text-input-version=3` picks the protocol
version KWin offers (it advertises `zwp_text_input_manager_v2` and `v3`).

	with both          the keyboard appears
	dropping the ime flag   no keyboard at all
	dropping the version    no keyboard at all

Chromium ignores flags it does not recognise without a word, so each of these
could only be told apart by trying it.

`applications/kali-code-oss.desktop` is the resulting user-level override; drop
it in `~/.local/share/applications/` where it takes precedence over the packaged
one. VS Code is single-instance, so it has to be fully quit before the new flags
take effect -- reopening while the old instance runs just hands the file to the
old one.

## Still open: it comes back after being minimised

With the keyboard working, minimising it with its own button hides it for a few
seconds and then it returns. The port's input method is not the cause -- it is a
thin wrapper that adds terminal keys to the stock `plasma-keyboard` and execs it.

Sampling KWin's properties every 0.3 s while typing shows what is actually
happening, and it is not what it looked like:

	01:22:47.10  active=true  visible=false
	01:22:47.46  active=true  visible=true
	01:22:48.14  active=true  visible=false
	01:22:48.50  active=true  visible=true

`active` never wavers -- KWin knows the application wants text input the whole
time -- while `visible` flips every 0.4 to 0.7 s. So the application is not
re-asking for the keyboard; the keyboard surface itself is going up and down
underneath a stable request. And after the keyboard is dismissed with its own
button, tapping the editor does not bring it back, which fits: the dismissal
never reaches Chromium, so it has no reason to ask again.

Two candidate explanations were then ruled out by measurement:

  * **The port's input method is not at fault.** It is a wrapper that adds
    terminal keys to the stock `plasma-keyboard` and execs it.
  * **The keyboard process is not crashing and being respawned.** Its pid is
    stable across the flicker, and there are no crashes in the journal.

`--wayland-text-input-version=2` was worth trying, because v2 carries explicit
show and hide panel messages and v3 leaves the decision to the compositor --
which would explain a dismissal that goes nowhere. Chromium does not implement
v2: with it, no keyboard appears at all. So v3 is the only version both sides
speak, and its missing hide path is the thing that hurts.

That puts the fault upstream, between Chromium's text-input-v3 implementation
and KWin, where no browser flag reaches. What is left is either KWin's writable
`mode` (currently 1), which applies to every application including the terminal
where this already works, or typing in something other than VS Code.

## X11 and Java apps: there is no way in from outside KWin

Burp is Java on XWayland and never shows a keyboard at all. Three things were
tried and all three are closed:

**`forceActivate` over an X11 window does nothing.** Measured with Burp focused
and a cursor in a text field:

	before      supportsTextInput=false  active=false  visible=false
	t+1s..t+5s                           active=false  visible=false

KWin will not activate the virtual keyboard when the focused client cannot
receive text, and there is no property or method that overrides it. `mode` is
advertised as writable and is not: writing 0, 2 or 3 leaves it at 1.

**No third-party keyboard can inject keys.** KWin offers only
`zwp_input_method_v1` to ordinary clients; `zwp_virtual_keyboard_manager_v1` is
reserved for the input method KWin launches itself. So wvkbd, squeekboard and
anything like them cannot put a keystroke anywhere.

**onboard works and is unpleasant.** It is an X11 client and injects through
XTEST, which reaches every X11 app including Burp without asking permission --
the only mechanism tried that does not need the application's cooperation. It
was rejected on how it feels to use, not on whether it works. Uninstalled.

What is left is a change inside KWin: either letting forceActivate raise the
keyboard over any surface, or having KWin offer text-input on behalf of
XWayland surfaces. That is a C++ patch and a KWin rebuild, not configuration.

Note for anyone testing this over ssh: do not name a shell variable `path`.
zsh ties it to `PATH` and the rest of the script loses every command it needs.

## The KWin patch (written)

`kwin-patches/0001-inputmethod-forced-activation-for-xwayland.patch` takes the
first of those two routes -- letting `forceActivate` raise the keyboard over any
surface and *keep* it there. Build and deploy it with `build-kwin.sh`
(`--install` to overwrite the packaged KWin; it holds and protects the result so
an upgrade cannot silently revert it).

**What the patch does.** `forceActivate()` already bypasses the text-input gate
once, but nothing makes it stick: the research into KWin's `InputMethod`
(`src/inputmethod.cpp`, Plasma/6.2) showed three separate things tear a forced
keyboard back down within a moment of it appearing:

  * `refreshActive()` re-imposes the text-input requirement and calls
    `setActive(false)` on the next text-input enabled-changed signal;
  * `shouldShowOnActive()` only shows the panel when the last input was touch or
    tablet, so a mouse/keyboard-driven XWayland window never gets it;
  * `setTrackedWindow()` clears `m_shouldShowPanel` on every focus change.

The patch adds one bit of state, `m_forcedActive`, set by `forceActivate()` and
cleared when the user dismisses the keyboard (`hide()`). While it is set,
`refreshActive()` forces the keyboard active, `shouldShowOnActive()` returns
true (the same escape hatch `KWIN_IM_SHOW_ALWAYS` gives), and a focus change
keeps the panel up and re-shows it. It is deliberately small -- one member, four
short edits -- and touches nothing on the ordinary text-input path, so Wayland
apps behave exactly as before.

**Measure this before trusting it -- it decides whether the patch can work at
all.** The patch only helps if `plasma-keyboard` sends **keycodes** (through
`zwp_virtual_keyboard_v1`): keycodes go to the seat's keyboard focus, which is
the focused window, XWayland included, so once the keyboard is allowed to show,
the keys already have somewhere to land. If instead it **commits strings**
through the text-input protocol, there is nothing for an X11 window to receive
and a bigger patch is needed (KWin synthesising key events from the committed
text, the job XTEST does for onboard, done inside the compositor). Tell the two
apart on the device:

	WAYLAND_DEBUG=1 plasma-keyboard 2>&1 | grep -Ei 'virtual_keyboard|text_input'

`build-kwin.sh` repeats this warning at the top; do not flash the rebuild and
expect Burp to type until the keycode path is confirmed.

**Once installed**, with the X11/Java app focused: `rhodep-keyboard on`. The
keyboard should appear and stay up across clicks into other fields, and `off`
(or its own dismiss button) should take it down and clear the forced state.
