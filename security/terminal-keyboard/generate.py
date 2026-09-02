#!/usr/bin/env python3
"""Build the terminal-friendly layout tree from Qt's stock one.

Usage: generate.py <stock-layouts-dir> <destination-dir> [--qmllint PATH] [--qml-import DIR]

Every layout that Qt ships is transformed, not just one, because switching the
keyboard to Spanish for the sake of the "n" key should not cost you Esc, Tab,
Ctrl and the arrows. 38 of the 44 locales carry a main.qml of their own, so
patching only fallback/ left every one of them without the terminal row.

Each transformed file is checked with qmllint when it is available, and any file
that fails to verify is left as Qt shipped it. A keyboard layout that does not
load is a phone you cannot type on, so the stock file always wins over a clever
transform.

What gets added to a layout:

  * a first row with Esc, Tab, Ctrl, Alt and the four arrows
  * Ctrl and Alt as modifiers that arm rather than type
  * character keys that become part of a combination while one is armed

How the combination is delivered is the interesting part, and the reasoning is
in extra-tools/terminal-keyboard/README.md: modifier masks are discarded on the way out, so
these send control characters as text instead, which is what Ctrl+C on a tty
actually is.
"""

import os
import re
import shutil
import subprocess
import sys

SHARED = '''    property bool ctrlArmed: false
    property bool altArmed: false

    function armed() { return ctrlArmed || altArmed }

    function disarm() {
        ctrlArmed = false
        altArmed = false
    }

    // The 0x40..0x5f block is what Ctrl folds onto 0x00..0x1f on a tty: Ctrl+C
    // is 0x03, Ctrl+[ is 0x1b - which is why Esc and Ctrl+[ are the same key on
    // a terminal - and Ctrl+_ is 0x1f.
    function controlCharacter(keyCode) {
        if (keyCode >= 0x40 && keyCode <= 0x5f)
            return String.fromCharCode(keyCode & 0x1f)
        return ""
    }

    // Sent as text, not as a modifier mask. plasma-keyboard hardcodes the
    // protocol's modifier field to 0 and KWin ignores it regardless, deriving
    // modifiers from the keymap, so a mask never arrives. Text does.
    function sendCombination(keyCode, plainText) {
        var out = plainText

        // Ctrl+Shift+C and Ctrl+Shift+V cannot be told apart from Ctrl+C and
        // Ctrl+V as control characters - those ignore case, 0x03 either way -
        // and 0x03 has to keep cancelling. So they go out as Esc followed by the
        // uppercase letter, which nothing else produces, and the shell turns
        // that into copy and paste. See extra-tools/terminal-clipboard.
        if (ctrlArmed && InputContext.shiftActive && plainText.length > 0) {
            disarm()
            InputContext.sendKeyClick(keyCode,
                                      String.fromCharCode(0x1b) + plainText.toUpperCase(),
                                      0)
            return
        }

        if (ctrlArmed) {
            var c = controlCharacter(keyCode)
            if (c !== "")
                out = c
        }
        if (altArmed)
            out = String.fromCharCode(0x1b) + out // Esc prefix: Alt on a tty
        disarm()
        if (out.length === 0)
            return
        InputContext.sendKeyClick(keyCode, out, 0)
    }

    // The space bar carries the current language's own name across its whole
    // width - Qt.locale(...).nativeLanguageName, which for Spanish (Mexico) is
    // long enough to fill it - and it never goes away. Breeze's style holds
    // inputLocaleIndicatorOpacity at 1.0 and a one second timer only takes it to
    // 0.5, never to 0:
    //
    //     property real inputLocaleIndicatorOpacity: 1.0
    //     onTriggered: inputLocaleIndicatorOpacity = 0.5
    //     onInputLocaleChanged: inputLocaleIndicatorOpacity = 1.0
    //
    // Editing the style is not an option - its qmldir says
    // "prefer :/qt/qml/QtQuick/VirtualKeyboard/Styles/Breeze/", so it is loaded
    // from inside libbreezestyle.so and the style.qml on disk is never read.
    //
    // So take the property to 0 from here and keep it there. A Binding would be
    // destroyed by the style's own assignment on the next language change, which
    // is why this re-asserts instead.
    function rhodepHideLocaleLabel() {
        if (typeof keyboard !== "undefined" && keyboard && keyboard.style
                && keyboard.style.inputLocaleIndicatorOpacity !== 0)
            keyboard.style.inputLocaleIndicatorOpacity = 0
    }

    Connections {
        target: (typeof keyboard !== "undefined" && keyboard) ? keyboard.style : null
        function onInputLocaleIndicatorOpacityChanged() { ROOT.rhodepHideLocaleLabel() }
        Component.onCompleted: ROOT.rhodepHideLocaleLabel()
    }

    // A character key that turns into part of a combination while a modifier is
    // armed: noKeyEvent stops it typing its own character, and clicked still
    // fires, which is what sends the combination.
    component TermKey: Key {
        noKeyEvent: ROOT.armed()
        onClicked: {
            if (!ROOT.armed())
                return
            ROOT.sendCombination(key, uppercased ? text.toUpperCase() : text)
        }
    }
'''

TERM_ROW = '''// Esc, Tab, the modifiers and the arrows, in the same place in every layout.
KeyboardRow {
    Key { key: Qt.Key_Escape; displayText: "Esc"; functionKey: true; noModifier: true; weight: 210 }
    Key { key: Qt.Key_Tab; text: "\\t"; displayText: "Tab"; functionKey: true; noModifier: true; weight: 210 }
    Key {
        displayText: "Ctrl"
        functionKey: true
        noKeyEvent: true
        noModifier: true
        weight: 210
        highlighted: ROOT.ctrlArmed
        // A dot in the corner, so the label keeps its width. noModifier also
        // stops Shift rewriting these labels as ESC/TAB/CTRL/ALT, since BaseKey
        // does "uppercased: InputContext.uppercase && !noModifier".
        smallText: "\\u25cf"
        smallTextVisible: ROOT.ctrlArmed
        onClicked: ROOT.ctrlArmed = !ROOT.ctrlArmed
    }
    Key {
        displayText: "Alt"
        functionKey: true
        noKeyEvent: true
        noModifier: true
        weight: 210
        highlighted: ROOT.altArmed
        smallText: "\\u25cf"
        smallTextVisible: ROOT.altArmed
        onClicked: ROOT.altArmed = !ROOT.altArmed
    }
    Key { key: Qt.Key_Left; displayText: "\\u2190"; functionKey: true; noModifier: true; weight: 120 }
    Key { key: Qt.Key_Up; displayText: "\\u2191"; functionKey: true; noModifier: true; weight: 120 }
    Key { key: Qt.Key_Down; displayText: "\\u2193"; functionKey: true; noModifier: true; weight: 120 }
    Key { key: Qt.Key_Right; displayText: "\\u2192"; functionKey: true; noModifier: true; weight: 120 }
}'''


def indent_block(text, spaces):
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.split("\n"))


def brace_block(src, start):
    """The extent of the {...} that begins at or after start."""
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return i, j
    raise ValueError("unbalanced braces")


def to_termkeys(text):
    """Character keys become TermKey. Anything with its own onClicked, and every
    special key type, keeps its behaviour."""
    out, pos = [], 0
    for m in re.finditer(r'^([ \t]*)Key \{', text, re.M):
        start, end = brace_block(text, m.start())
        block = text[m.start():end + 1]
        out.append(text[pos:m.start()])
        if "text:" in block and "onClicked" not in block:
            block = re.sub(r'^([ \t]*)Key \{', r'\1TermKey {', block, count=1)
        out.append(block)
        pos = end + 1
    out.append(text[pos:])
    return "".join(out)


def transform(text):
    """Returns the transformed layout, or None if the shape is unfamiliar."""
    m = re.search(r'^(KeyboardLayout|KeyboardLayoutLoader)\s*\{', text, re.M)
    if not m:
        return None
    root_type = m.group(1)
    root_open = text.index("{", m.start())

    # The root's own id, if it has one; otherwise give it a name that cannot
    # collide with a key's id.
    body_start = root_open + 1
    rid = None
    id_match = re.search(r'^    id:\s*(\w+)\s*$', text[body_start:], re.M)
    if id_match and id_match.start() < 400:
        rid = id_match.group(1)

    text = to_termkeys(text)

    # Recompute after the rename, the offsets moved.
    m = re.search(r'^(KeyboardLayout|KeyboardLayoutLoader)\s*\{', text, re.M)
    root_open = text.index("{", m.start())

    inject = SHARED.replace("ROOT", rid or "rhodepRoot")
    if rid is None:
        inject = "    id: rhodepRoot\n\n" + inject
    text = text[:root_open + 1] + "\n" + inject + text[root_open + 1:]

    row = TERM_ROW.replace("ROOT", rid or "rhodepRoot")

    if root_type == "KeyboardLayout":
        # Straight into the root, ahead of its first row.
        first = re.search(r'^([ \t]*)KeyboardRow \{', text, re.M)
        if not first:
            return None
        pad = len(first.group(1))
        text = text[:first.start()] + indent_block(row, pad).lstrip() + "\n" + text[first.start():]
    else:
        # A loader: every page is its own KeyboardLayout and each one needs the
        # row, or it appears and disappears as you switch pages.
        pieces, pos, added = [], 0, 0
        for lm in re.finditer(r'^([ \t]*)KeyboardLayout \{', text, re.M):
            if lm.start() < pos:
                continue
            start, end = brace_block(text, lm.start())
            block = text[lm.start():end + 1]
            first = re.search(r'^([ \t]*)KeyboardRow \{', block, re.M)
            if not first:
                continue
            pad = len(first.group(1))
            block = block[:first.start()] + indent_block(row, pad).lstrip() + "\n" + block[first.start():]
            pieces.append(text[pos:lm.start()])
            pieces.append(block)
            pos = end + 1
            added += 1
        if not added:
            return None
        pieces.append(text[pos:])
        text = "".join(pieces)

    return text


def qmllint_ok(path, qmllint, imports):
    if not qmllint:
        return True
    cmd = [qmllint]
    for d in imports:
        cmd += ["-I", d]
    cmd.append(path)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:
        return True  # no verdict is not a failure
    bad = [ln for ln in (res.stdout + res.stderr).splitlines()
           if ln.startswith("Error") or (ln.startswith("Warning") and "nqualified" not in ln)]
    return not bad


def main():
    args = [a for a in sys.argv[1:]]
    qmllint, imports = None, []
    while "--qmllint" in args:
        i = args.index("--qmllint")
        qmllint = args[i + 1]
        del args[i:i + 2]
    while "--qml-import" in args:
        i = args.index("--qml-import")
        imports.append(args[i + 1])
        del args[i:i + 2]
    if len(args) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    stock, dest = args

    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(stock, dest)

    done, skipped, untouched = [], [], 0
    for locale in sorted(os.listdir(dest)):
        d = os.path.join(dest, locale)
        if not os.path.isdir(d):
            continue
        for name in ("main.qml", "symbols.qml"):
            path = os.path.join(d, name)
            if not os.path.exists(path):
                untouched += 1
                continue
            original = open(path, encoding="utf-8").read()
            try:
                new = transform(original)
            except Exception as exc:
                new = None
                reason = str(exc)
            else:
                reason = "shape not recognised"
            if new is None:
                skipped.append(f"{locale}/{name}: {reason}")
                continue
            open(path, "w", encoding="utf-8").write(new)
            if qmllint_ok(path, qmllint, imports):
                done.append(f"{locale}/{name}")
            else:
                open(path, "w", encoding="utf-8").write(original)
                skipped.append(f"{locale}/{name}: qmllint rejected it, left as Qt shipped it")

    print(f"transformed {len(done)} layout files")
    if skipped:
        print(f"left alone ({len(skipped)}):")
        for s in skipped:
            print("  " + s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
