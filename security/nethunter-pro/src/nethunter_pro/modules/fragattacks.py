"""FragAttacks -- Mathy Vanhoef's fragmentation and aggregation test tool.

Upstream: https://github.com/vanhoefm/fragattacks

The tool is a research suite: 45+ individual test-cases, each aimed at
one CVE from the FragAttacks family (CVE-2020-24586..24588 and
CVE-2020-26139..26147). What every test does is variations on the
same idea:

  * associate to a target AP as a normal client, or run as a rogue AP
    while a victim client associates to us,
  * send crafted frames -- fragmented, aggregated (A-MSDU), or
    mismatched-key -- and check whether the peer accepts them,
  * report ``VULNERABLE`` / ``NOT VULNERABLE`` / ``ERROR`` in the
    stdout.

The unique payoff of FragAttacks vs everything else in this suite is
that a *successful* aggregation attack (CVE-2020-24588) lets you
inject arbitrary IP packets into the target network **without knowing
the WPA passphrase**. Router NAT/firewall then forwards them to LAN
hosts, so an unpatched printer or IP camera behind the AP is
suddenly reachable from the air. That is the only pivot in this
whole app that does not require the PSK.

Two important caveats surfaced up-front in the UI:

1. **Hardware.** The upstream test tool requires ``ath9k_htc``
   (Atheros AR9271 USB) with a modified firmware Vanhoef ships in the
   ``research`` folder of his repo. On the rhodep phone the internal
   wlan0 is a Qualcomm ``ath10k_snoc`` device and *cannot* run
   FragAttacks: the driver simply refuses the injection primitives
   the tool needs. Any external ``ath9k_htc`` USB dongle plugged
   into the phone's USB-C port works though.

2. **Software.** The tool is not packaged in Kali. The module clones
   it from GitHub under ``/opt/fragattacks``
   the first time you hit "Install", pip-installs the dependencies
   in a venv, and reads test names from the manifest. You need
   network access on the phone the first time.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async, which
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

_FRAG_REPO = "https://github.com/vanhoefm/fragattacks.git"
_FRAG_DIR = Path("/opt/fragattacks")
_FRAG_MAIN = _FRAG_DIR / "research" / "fragattack.py"

# A curated slice of the test-case names in fragattack.py, grouped
# by "what does this test actually prove". The tool has ~45 cases;
# these are the meaningful ones a pentester runs first.
_TEST_CASES = [
    # ("test name in fragattack.py", "user-friendly description")
    ("ping I,E", "Baseline: encrypted ping (should PASS)"),
    ("ping I,D,P",
     "Plaintext injection into cipher session (CVE-2020-26140)"),
    ("ping I,P,E",
     "Encrypted frame during PMF (CVE-2020-26143)"),
    ("ping-frag-sep",
     "Fragmentation cache attack (CVE-2020-24586)"),
    ("ping I,E,E --amsdu",
     "A-MSDU aggregation attack, KEY loot (CVE-2020-24588)"),
    ("ping I,E,D --amsdu",
     "A-MSDU with plaintext outer (CVE-2020-26144)"),
    ("linux-plain",
     "Linux driver plaintext acceptance (CVE-2020-26141)"),
    ("macos",
     "macOS-specific plaintext acceptance"),
    ("cache-poison",
     "Fragment cache poisoning between clients"),
    ("mixed-key",
     "Mixed key attack: reassemble across PTK rotations "
     "(CVE-2020-24587)"),
]


def _detect_chipset() -> tuple[str, bool]:
    """Return (driver_name, supported) for wlan0. Best-effort: we
    look at /sys/class/ieee80211/phyN/device/driver."""
    try:
        for phy in Path("/sys/class/ieee80211").iterdir():
            drv = phy / "device" / "driver"
            if drv.exists():
                target = os.readlink(drv)
                name = target.rsplit("/", 1)[-1]
                supported = name.startswith("ath9k")
                return name, supported
    except OSError:
        pass
    return "?", False


@register
class FragAttacks(NHModule):
    title = "FragAttacks"
    icon = "network-error-symbolic"
    description = ("Mathy Vanhoef's fragmentation/aggregation test "
                   "suite (CVE-2020-24586+). One of the few attacks "
                   "that gives LAN pivot without the PSK.")
    # We don't require any specific tool because the module knows how
    # to install its own -- the missing-tools banner would be scary
    # otherwise. Instead, we check for git/python3 on-demand.
    required_tools = []

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._loot_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ---- chipset check ----
        driver, ok = _detect_chipset()
        chip_group = Adw.PreferencesGroup(
            title="Hardware compatibility",
            description="FragAttacks needs an Atheros AR9271 "
                        "(ath9k_htc) USB adapter with the modified "
                        "firmware from Vanhoef's repo.")
        chip_row = Adw.ActionRow(
            title="wlan0 driver",
            subtitle=driver + (
                " — OK" if ok else
                " — cannot run FragAttacks. Plug an ath9k_htc dongle."))
        chip_group.add(chip_row)
        box.append(chip_group)

        # ---- install ----
        inst_group = Adw.PreferencesGroup(
            title="Installation",
            description="Clones the upstream repo and pip-installs "
                        "its dependencies. Needs the phone to be "
                        "online.")
        self.inst_row = Adw.ActionRow(
            title="fragattacks repo",
            subtitle=str(_FRAG_DIR) +
                     (" — installed" if _FRAG_MAIN.exists()
                      else " — not installed"))
        inst_btn = Gtk.Button(label="Install",
                              valign=Gtk.Align.CENTER)
        inst_btn.add_css_class("suggested-action")
        inst_btn.connect("clicked", lambda _b: self._install())
        self.inst_row.add_suffix(inst_btn)
        inst_group.add(self.inst_row)
        box.append(inst_group)

        # ---- test picker ----
        test_group = Adw.PreferencesGroup(
            title="Test",
            description="Pick a target AP, a monitor interface, and "
                        "the CVE to probe. Output is captured to "
                        "the loot store.")
        self.iface = Adw.EntryRow(title="ath9k_htc interface")
        self.iface.set_text("wlan1")
        test_group.add(self.iface)

        self.role = Adw.ComboRow(title="Role")
        self.role.set_model(Gtk.StringList.new([
            "Client (associate to target AP)",
            "Rogue AP (wait for victim client)",
        ]))
        test_group.add(self.role)

        self.target_ssid = Adw.EntryRow(title="Target ESSID")
        test_group.add(self.target_ssid)
        self.target_psk = Adw.PasswordEntryRow(
            title="Target PSK (client mode only)")
        test_group.add(self.target_psk)

        self.testcase = Adw.ComboRow(title="Test case")
        self.testcase.set_model(Gtk.StringList.new(
            [desc for _, desc in _TEST_CASES]))
        test_group.add(self.testcase)

        run_row = Adw.ActionRow(
            title="Run test",
            subtitle="Logs and outcome go to the Loot store")
        self.run_btn = Gtk.Button(label="Run",
                                  valign=Gtk.Align.CENTER)
        self.run_btn.add_css_class("suggested-action")
        self.run_btn.connect("clicked", lambda _b: self._run())
        run_row.add_suffix(self.run_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        run_row.add_suffix(self.stop_btn)
        test_group.add(run_row)
        box.append(test_group)

        # ---- output ----
        self.output = OutputView()
        box.append(self.output)
        return box

    # ------------------------------------------------------- install
    def _install(self) -> None:
        # Clone + venv + pip. Idempotent; if the repo already exists
        # Runs as root because /opt is not writable by the login
        # user; the venv inside gets chown'd to kali afterwards so
        # the run step (which drops privs to source the venv from
        # user context) can still read it.
        #
        # Vanhoef's fragattacks fork is from 2021 and needs three
        # compat patches to run on modern Kali. All applied here so
        # the operator gets a working install first-try:
        #
        #   * OpenSSL 3: add ``#include <openssl/provider.h>`` to
        #     ``src/crypto/crypto_openssl.c`` (dropped default
        #     visibility for the surviving EC accessors).
        #   * Scapy 2.5: ``L2Socket`` no longer star-exports from
        #     ``scapy.all``; import it explicitly in
        #     ``research/libwifi/wifi.py``.
        #   * Scapy 2.5: ``get_if_raw_hwaddr`` moved from
        #     ``scapy.arch.common`` to ``scapy.arch.linux``;
        #     rewrite the import in ``research/fraginternals.py``.
        #
        # We build the venv with python3.13 because scapy 2.5.0 --
        # the only version that still ships ``scapy.contrib.wpa_eapol``
        # AND works with libwifi -- fails to import on the newer
        # 3.14 Kali ships as default. Falls back to plain python3
        # if 3.13 isn't installed.
        self.output.append("# installing fragattacks…\n")
        script = (
            "set -e; "
            "mkdir -p %s; "
            "if [ ! -d %s/.git ]; then "
            "  git clone --depth 1 %s %s; "
            "else "
            "  git -C %s pull --ff-only; "
            "fi; "
            "if [ -f %s/src/crypto/crypto_openssl.c ] && "
            "   ! grep -q 'openssl/provider.h' "
            "        %s/src/crypto/crypto_openssl.c; then "
            "  sed -i '/#include <openssl\\/opensslv.h>/a "
            "#include <openssl/provider.h>' "
            "     %s/src/crypto/crypto_openssl.c; "
            "fi; "
            "( cd %s/research && ./build.sh ); "
            "PY=$(command -v python3.13 || command -v python3); "
            "cd %s; "
            "$PY -m venv .venv; "
            ". .venv/bin/activate; "
            "pip install --quiet --disable-pip-version-check "
            "  wheel scapy==2.5.0 pycryptodome; "
            "if [ -f %s/research/libwifi/wifi.py ] && "
            "   ! grep -q 'scapy.arch.linux import L2Socket' "
            "        %s/research/libwifi/wifi.py; then "
            "  sed -i '1a from scapy.arch.linux import L2Socket' "
            "     %s/research/libwifi/wifi.py; "
            "fi; "
            "sed -i 's|from scapy.arch.common import "
            "get_if_raw_hwaddr|from scapy.arch.linux import "
            "get_if_raw_hwaddr|' %s/research/fraginternals.py "
            "  2>/dev/null || true; "
            "chown -R kali:kali %s || true; "
            "echo ---installed---"
        ) % (
            _FRAG_DIR.parent, _FRAG_DIR, _FRAG_REPO, _FRAG_DIR,
            _FRAG_DIR,
            _FRAG_DIR, _FRAG_DIR, _FRAG_DIR,
            _FRAG_DIR, _FRAG_DIR,
            _FRAG_DIR, _FRAG_DIR, _FRAG_DIR,
            _FRAG_DIR, _FRAG_DIR,
        )

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            if _FRAG_MAIN.exists():
                self.inst_row.set_subtitle(str(_FRAG_DIR) + " — installed")
                toast(self.app_window, "fragattacks installed")
            else:
                self.inst_row.set_subtitle(
                    "install failed — see log")

        # Root needed to write /opt.
        run_async(["sh", "-c", script], done, root=True,
                  timeout=300)

    # ---------------------------------------------------------- run
    def _run(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        if not _FRAG_MAIN.exists():
            toast(self.app_window,
                  "Install FragAttacks first")
            return
        iface = self.iface.get_text().strip() or "wlan1"
        idx = self.testcase.get_selected()
        test_name = _TEST_CASES[idx][0]
        ssid = self.target_ssid.get_text().strip()
        psk = self.target_psk.get_text().strip()
        role = "client" if self.role.get_selected() == 0 else "ap"

        # fragattack.py needs to be run inside its own venv. We wrap
        # in a shell so we can source it. Argv from Vanhoef's README:
        #   ./fragattack.py wlan0 ping I,E [--target-ssid ...]
        parts = [
            "cd %s/research &&" % _FRAG_DIR,
            ". ../.venv/bin/activate &&",
            "sudo -E python3 fragattack.py",
            iface,
            test_name,
        ]
        if role == "client":
            if not ssid:
                toast(self.app_window,
                      "Client mode needs a target ESSID")
                return
            parts += ["--target-ssid", "'%s'" % ssid]
            if psk:
                parts += ["--target-psk", "'%s'" % psk]
        else:
            parts += ["--rogue-ap"]
            if ssid:
                parts += ["--ssid", "'%s'" % ssid]

        cmd = " ".join(parts)
        self.output.append("$ " + cmd + "\n")

        stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "fragattacks", "fragattack-%s-%s.log"
            % (re.sub(r"[^A-Za-z0-9]+", "_", test_name), stamp))
        self._loot_id = get_loot_store().record(
            module="fragattacks", type="fragattacks_log",
            target="%s: %s" % (role, ssid or "?"),
            path=log_path,
            notes="test-case=%s" % test_name)

        # Route output both to the UI and to the log file.
        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass

        def on_done(code: int) -> None:
            self.output.append("[fragattack exited: %d]\n" % code)
            self._proc = None
            self.run_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)
                # Look for the VULNERABLE / NOT VULNERABLE line in
                # the tail of the log so the loot row is annotated.
                try:
                    with open(log_path) as fp:
                        text = fp.read()
                except OSError:
                    text = ""
                verdict = "unknown"
                for line in text.splitlines()[-30:]:
                    up = line.upper()
                    if "NOT VULNERABLE" in up:
                        verdict = "not vulnerable"
                    elif "VULNERABLE" in up and verdict == "unknown":
                        verdict = "VULNERABLE"
                get_loot_store().append_notes(
                    self._loot_id,
                    "verdict: " + verdict)

        # We wrap in a shell because of the "cd + source venv" chain.
        self._proc = Process(
            ["sh", "-c", cmd], on_line, on_done, root=False)
        self._proc.start()
        self.run_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
