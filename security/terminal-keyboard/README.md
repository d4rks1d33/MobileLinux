# Terminal keys for the Plasma Mobile keyboard

	sudo ./install.sh
	# then log out and back in, or reboot

## Every language, not just one

All 52 layout files Qt ships get the terminal row — 39 `main.qml` and 13
`symbols.qml`. That matters because **38 of the 44 locales carry a `main.qml` of
their own**, so an earlier version that patched only `fallback/` meant switching
to Spanish for the sake of the `ñ` cost you Esc, Tab, Ctrl and the arrows.

	es_ES: terminal row yes, ñ yes
	es_MX: terminal row yes, ñ yes

There is no `es_AR` in Qt's set; `es_MX` is the Latin American layout, and both
Spanish layouts have `ñ` as a real key. The fallback layout does not — its `n`
offers `ņńnň` — which is why the language key matters.

### The language key has to be switched on

Reaching those layouts needs one more thing, and without it the language key is
greyed out and does nothing when tapped. `plasmakeyboardrc` ships with
`enabledLocales` **empty**, and plasma-keyboard reads an empty list as *one
language only*:

	if (PlasmaKeyboardSettings.enabledLocales.length === 0)
	    VirtualKeyboardSettings.activeLocales = [locale];

So `install.sh` writes the list from `enabled-locales`, `en_US,es_MX,es_ES` by
default. Edit that file to change it.

The enforcer repairs this **only when the setting is empty** — the broken state.
A list you changed yourself is yours:

	emptied by hand         put back, and logged
	set to de_DE,fr_FR      left alone

`generate.py` does the transformation at install time and **verifies every file
with `qmllint`, keeping Qt's original whenever a transform cannot be verified**. A
layout that will not load is a phone you cannot type on, so the stock file always
wins. On this device all 52 verified.

## The letters page

	Esc Tab Ctrl Alt  ←  ↑  ↓  →
	q  w  e  r  t  y  u  i  o  p
	 a  s  d  f  g  h  j  k  l
	⇧   z  x  c  v  b  n  m    ⌫
	&123 ⚙ ,  space  .  ⌨  ⏎

The same row sits on top of both `&123` pages, so Esc, the arrows and the
modifiers are always in the same place. No digit row: the digits are one tap
away on `&123`, and long press on `q..p` still reaches them.

`noModifier: true` on that row is what stops Shift from turning the labels into
`ESC TAB CTRL ALT` — `BaseKey` does `uppercased: InputContext.uppercase &&
!noModifier` — and those keys have no business being shifted anyway. The word
keys are wider than the arrow keys (`weight: 210` against `120`), because equal
widths left "Ctrl" cramped while an arrow swam in space.

**Ctrl and Alt arm rather than type**, the way Termux's extra keys row works.
Tap `Ctrl`, then `c`, and the terminal gets Ctrl+C. Confirmed working, along
with `^D ^Z ^R ^A ^E ^U ^K ^W ^L` and `Alt+`anything.

### How it is delivered, and the two ways that do not work

This took a protocol trace and a read of plasma-keyboard's source. Passing
`Qt.ControlModifier` to `virtualKeyClick` does nothing at all, because
`src/inputlisteneritem.cpp` does:

	m_input.keysym(timestamp, key, InputPlugin::Pressed, 0);
	                                                    ^ hardcoded

The protocol's modifier field is always zero, so no mask ever arrives and the
terminal saw a bare `C`. Holding `Control_L` down around the letter does not work
either: `InputEngine` delivers a key press *on release*, so the trace showed the
letter going out first and `Control_L` after it:

	sym=67     state=1   <- the letter
	sym=67     state=0
	sym=65507  state=1   <- Control_L, too late
	sym=65507  state=0

The same file shows the way through — a key whose event carries text is
committed as a string instead of a keysym:

	if (event->text().isEmpty() || key == XKB_KEY_Return)
	    m_input.keysym(...);
	else
	    m_input.commit(event->text());

and **Ctrl+C on a tty is not a combination, it is a byte**: `0x03`. So these keys
send the control character as text. `0x40..0x5f` folds onto `0x00..0x1f`, which
is exactly what Ctrl does — which is also why Esc and Ctrl+[ are the same key on
a terminal — and Alt is an Escape prefix, which is what every shell expects.

### What this cannot do

`Ctrl+Shift+C` for terminal copy. That one is not a character, it is a
combination the terminal interprets, and combinations are what the hardcoded
zero throws away. Fixing it needs a patched plasma-keyboard, which is tracked
separately.

Termux itself is no help as code — it is an Android app with its own terminal
widget, and this is Wayland — but its extra-keys row is the right design and
this borrows it.

## The symbols page

One tap on **&123** and the terminal keys are there — they are the *first* page
of the symbols layout, not a page you have to walk to:

	Esc    Tab     Home   End
	 ←      ↑       ↓      →
	^C     ^V      ^X     ⇧^C    ⇧^V
	ABC    1/3    space    ⌫      ⏎

Four and five keys per row, deliberately. The first version put ten per row,
copying the stock layout, and labels like `Esc`, `PgUp` and `⇧^C` are far wider
than a digit: they overflowed their keys and ran into each other, and `PgDn`
ended up half under the Enter key. Fewer keys, wide enough to hit.

`1/3` cycles on to the two stock symbol pages, so nothing that was there before
is lost.

## Why not an existing keyboard

Checked before writing anything. Nothing available works on this device, and
the reason is one line of protocol support:

	KWin advertises   zwlr_layer_shell_v1, zwp_input_method_v1,
	                  zwp_text_input_manager_v2/v3
	KWin does NOT     zwp_virtual_keyboard_manager_v1

- **wvkbd** is packaged, small, and has exactly the terminal layer wanted — and
  it injects keys through `virtual_keyboard_v1`. It would draw itself and type
  nothing.
- **squeekboard** speaks `input_method_v2`, from wlroots. KWin only has v1.
  That, and not some subtle bug, is what its crashes here were always about;
  README.md's note about it can be read that way now.
- **maliit-keyboard** is Qt5, drags in half of the old stack, and has no
  terminal keys either.
- No terminal in the archive ships its own key bar.

So the shortest and safest path is to extend the keyboard that is already
integrated with KWin.

## The part that is not obvious

`plasma-keyboard` **does not read `/usr/share/plasma/keyboard/layouts`.** That
directory belongs to `qml6-module-qtquick-virtualkeyboard`; plasma-keyboard
prefers the layouts compiled into its own qrc:

	strings /usr/bin/plasma-keyboard
	  QT_VIRTUALKEYBOARD_LAYOUT_PATH
	  prefer :/qt/qml/org/kde/plasma/keyboard/

Dropping a `symbols.qml` into that directory changes nothing at all, which is
exactly what happened on the first attempt.

`QT_VIRTUALKEYBOARD_LAYOUT_PATH` is the way in, but **on its own it does
nothing either**: plasma-keyboard carries its own copy of the layouts and
ignores the variable until `PLASMA_KEYBOARD_USE_QT_LAYOUTS=1` tells it to use
Qt Virtual Keyboard's layouts instead. The two names sit next to each other in
`strings /usr/bin/plasma-keyboard`, which is the only documentation there is.
Setting the path replaces the *whole* layout set, so `install.sh` copies the
stock tree to `/usr/local/share/rhodep-keyboard/layouts` and changes one file
in it.

Not guesswork — `strace` on the process settles it:

	# both variables set
	openat(... "/usr/local/share/rhodep-keyboard/layouts/fallback/main.qml")
	# only the path set
	(nothing from that tree)

The variable has to reach the process KWin starts, and that is its own small
trap: editing `Exec=` in Plasma's `.desktop` file is not enough, KWin goes on
launching the binary it already knows. It takes a `.desktop` file of our own
that KWin is pointed at with `kwinrc [Wayland] InputMethod` — and **KWin reads
that setting only at startup**, so the change needs a logout or a reboot.
Plasma's own file is left untouched, so an upgrade cannot fight us over it.

The layout was checked with `qmllint` from `qt6-declarative-dev-tools` before
asking for that reboot. It reports only *unqualified access* style warnings —
the same ones Qt's own stock `symbols.qml` produces.

And a third trap, which is why an `en_US`-only layout still showed nothing with
everything else correct: **Qt Virtual Keyboard picks the layout directory from
the input locale**, and this session runs `LANG=C.UTF-8`. So the layout goes
into `fallback/`, which is where 30 of the 44 locales — `C.UTF-8` and `en_GB`
among them — resolve through their `symbols.fallback` marker. The 13 that ship
a symbols page of their own (Arabic, Hebrew, CJK, `es_ES`, `es_MX`) are left
alone because their symbols are language specific; `install.sh` says so if the
session is using one of them.

Quick way to tell whether the layout is live at all: tap **&123** and read the
page indicator. Stock says **1/2**, ours says **1/3**.

## The Ctrl keys are real key events

They do not fake control characters. Each calls

	InputContext.inputEngine.virtualKeyClick(Qt.Key_C, "", Qt.ControlModifier)

which is the documented API and produces a key event with the modifier set.
That is why `⇧^C` arrives at the terminal as copy rather than as a stray
character.

## If something goes wrong

The keyboard is how you type, so there is a way back that needs no keyboard —
over SSH:

	kwriteconfig6 --file kwinrc --group Wayland --key InputMethod \
	    /usr/share/applications/org.kde.plasma.keyboard.desktop

and log in again. The stock `.desktop` file is also kept as
`org.kde.plasma.keyboard.desktop.rhodep-orig`.

## No language name on the space bar

Breeze draws the current language's own name across the whole space bar —
`Qt.locale(InputContext.locale).nativeLanguageName`, which for a Spanish layout is
long enough to fill the whole key — and it never goes away. Its own style holds the
opacity up:

	property real inputLocaleIndicatorOpacity: 1.0
	onTriggered: inputLocaleIndicatorOpacity = 0.5   // a timer, one second
	onInputLocaleChanged: inputLocaleIndicatorOpacity = 1.0

so the label sits there at half opacity for good, and it comes back to full every
time the language changes.

Editing the style is not an option, and this is the same trap as the layouts: its
`qmldir` says

	prefer :/qt/qml/QtQuick/VirtualKeyboard/Styles/Breeze/

meaning the style is loaded from inside `libbreezestyle.so` and the `style.qml`
sitting on disk is never read. Editing it changes nothing.

So the layouts take that property to 0 and **keep it there**. A `Binding` would
be destroyed the moment the style assigns `1.0` on the next language change, so
this re-asserts on every change of the property instead:

	Connections {
	    target: keyboard.style
	    function onInputLocaleIndicatorOpacityChanged() { rhodepHideLocaleLabel() }
	    Component.onCompleted: rhodepHideLocaleLabel()
	}

Checked for runtime QML warnings afterwards, not just with qmllint — a wrong
property name on a style object is invisible until it runs. None appeared.

## Reinstalling restarts the keyboard

`install.sh` rebuilds the layout tree with `rm -rf` and a fresh copy, which pulls
the files out from under a running keyboard. It keeps its old, now-deleted layouts
mapped and cannot load anything it has not already cached, so the panel simply
stops appearing — with no error anywhere, since nothing crashed. Confirmed by
looking:

	$ ls -l /proc/$(pgrep -f plasma-keyboard)/map_files | grep -c deleted
	66

So the installer kills the keyboard at the end and lets KWin start it again.

## Protection

The layouts, the wrapper, the launcher and the enforcer are registered with
`rhodep-protect-files`, so they carry the immutable bit and a snapshot, and the
packages behind them are held and marked `Protected: yes` in dpkg:

	plasma-keyboard                       hold=1 protected=yes
	qml6-module-qtquick-virtualkeyboard    hold=1 protected=yes
	wl-clipboard                           hold=1 protected=yes
	qmlkonsole                             hold=1 protected=yes
	tmux                                   hold=1 protected=yes

`qml6-module-qtquick-virtualkeyboard` is on that list because `install.sh` copies
the stock layout tree from it. Lose the package and a reinstall has nothing to
build on.

Two things deliberately have **no** immutable bit, because locking them would
break what they belong to:

	kwinrc    KWin rewrites it constantly
	.zshrc    it belongs to the user, who edits it

Both `/home/kali/.zshrc` and `/root/.zshrc` are covered: half of what happens on
this phone happens under `sudo -i`, and a root shell that cannot paste is a root
shell where long commands get retyped by hand. root's `.zshrc` usually does not
exist at all, so the enforcer creates it.

Those get `rhodep-keyboard-enforce` instead, at boot and every 30 minutes. It
puts the `InputMethod` entry and the `.zshrc` source line back and says so in the
journal. Both failures are silent otherwise — a missing `InputMethod` just gives
you the stock keyboard with no terminal row, and a missing `.zshrc` line just
stops copy and paste working.

Verified by attacking it, which is the only way to know:

	apt-get purge plasma-keyboard          refused
	dpkg -P plasma-keyboard                "this is a protected package"
	rm  the wrapper                        Operation not permitted
	overwrite a layout                     Operation not permitted
	apt-mark unhold                        put back by the enforcer
	break kwinrc InputMethod               put back by the enforcer
	delete the .zshrc line                 put back by the enforcer
