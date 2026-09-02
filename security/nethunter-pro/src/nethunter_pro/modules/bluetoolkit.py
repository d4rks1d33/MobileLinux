"""BlueToolkit -- black-box BR/EDR vulnerability framework.

Upstream: https://github.com/sgxgsx/BlueToolkit (WOOT'25 paper).
Installed under ``/opt/bluetoolkit`` via ``install.sh`` which lays
down a virtualenv in ``/opt/bluetoolkit/.venv`` and pulls the
templates repo.

Coverage without extra hardware (the "auto-testable" set):

  * BlueBorne (CVE-2017-0785, CVE-2017-1000250, CVE-2017-1000251)
  * BleedingTooth family (CVE-2020-12351/-12352/-24490)
  * Braktooth V1..V16 fuzz cases (CVE-2021-28139 etc.)
    -- some variants need the ESP-WROVER-KIT for the *active* PoC;
    the *check-only* variants work standalone.
  * MyName Is Keyboard (CVE-2023-45866) HID injection
  * Legacy / Just-Works pairing weaknesses
  * SDP oversized element / unknown type parsers

Coverage with external hardware (info-only in the UI):

  * KNOB (CVE-2019-9506)     -- Nexus 5 + InternalBlue
  * BIAS (CVE-2020-10135)    -- Cypress CYW920819
  * BLUR (CVE-2020-15802)    -- Cypress CYW920819
  * BLUFFS (CVE-2023-24023)  -- Cypress CYW920819
  * CVE-2018-5383, -2018-19860 -- Nexus 5

The UI is a target picker + exploit chooser (checkboxes for the
auto-testable set) + a results directory viewer.  Everything else
lives in ``modules/bluetooth.py``.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

BLUETOOLKIT_DIR = Path("/opt/bluetoolkit")
BLUETOOLKIT_REPO = "https://github.com/sgxgsx/BlueToolkit"
BLUEKIT_BIN = BLUETOOLKIT_DIR / ".venv" / "bin" / "bluekit"

# The exploits that work without extra hardware. Names are the
# templates BlueToolkit ships in its exploits/ folder; keeping this
# list in sync with the upstream repo is a manual chore.
_AUTO_EXPLOITS = [
    ("blueborne_0785",           "BlueBorne SDP info leak (CVE-2017-0785)"),
    ("bleedingtooth_badchoice",  "BleedingTooth BadChoice info leak"),
    ("bleedingtooth_badkarma",   "BleedingTooth BadKarma RCE"),
    ("bleedingtooth_badvibes",   "BleedingTooth BadVibes DoS"),
    ("mynameiskeyboard",         "MyName Is Keyboard HID injection"),
    ("invalid_max_slot",         "Braktooth invalid max slot"),
    ("au_rand_flooding",         "Braktooth AU_RAND flooding"),
    ("feature_pages_execution",  "Braktooth feature pages exec"),
    ("truncated_sco",            "Braktooth truncated SCO"),
    ("duplicated_iocap",         "Braktooth duplicated IOCAP"),
    ("feature_resp_flooding",    "Braktooth feature resp flooding"),
    ("lmp_auto_rate_overflow",   "Braktooth LMP auto-rate overflow"),
    ("lmp_2dh1_overflow",        "Braktooth LMP 2DH1 overflow"),
    ("lmp_dm1_overflow",         "Braktooth LMP DM1 overflow"),
    ("truncated_lmp_accepted",   "Braktooth truncated LMP accepted"),
    ("invalid_setup_complete",   "Braktooth invalid setup complete"),
    ("host_conn_flooding",       "Braktooth host connection flooding"),
    ("same_host_connection",     "Braktooth same host connection"),
    ("max_slot_length_overflow", "Braktooth max slot length overflow"),
    ("invalid_timing_accuracy",  "Braktooth invalid timing accuracy"),
    ("paging_scan_deadlock",     "Braktooth paging scan deadlock"),
    ("sdp_unknown_element_type", "SDP unknown element type"),
    ("sdp_oversized_element_size", "SDP oversized element size"),
    ("feature_req_ping_pong",    "Feature-req ping-pong"),
    ("lmp_invalid_transport",    "LMP invalid transport"),
]


@register
class BlueToolkit(NHModule):
    title = "BlueToolkit"
    icon = "bluetooth-symbolic"
    description = ("BR/EDR vulnerability framework by sgxgsx: 40+ "
                   "exploits, YAML templates, JSON reports. 20+ of "
                   "them run with just the phone's own BT adapter.")
    required_tools = []

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        # Checkbox map: exploit_id -> Gtk.CheckButton
        self._exploit_boxes: dict[str, Gtk.CheckButton] = {}

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['bluetooth']))

        # ---- install
        inst = Adw.PreferencesGroup(
            title="Install",
            description="Clones the repo into /opt/bluetoolkit and "
                        "runs ``install.sh`` which builds the venv "
                        "and pulls the templates submodule.")
        self.inst_row = Adw.ActionRow(
            title="bluetoolkit",
            subtitle=str(BLUETOOLKIT_DIR) +
                     (" -- installed" if BLUEKIT_BIN.exists()
                      else " -- not installed"))
        inst_btn = Gtk.Button(label="Install",
                              valign=Gtk.Align.CENTER)
        inst_btn.add_css_class("suggested-action")
        inst_btn.connect("clicked", lambda _b: self._install())
        self.inst_row.add_suffix(inst_btn)
        inst.add(self.inst_row)
        box.append(inst)

        # ---- target
        target = Adw.PreferencesGroup(
            title="Target",
            description="MAC of the BR/EDR device under test.")
        self.mac = Adw.EntryRow(title="Target MAC")
        target.add(self.mac)

        for label, subtitle, arg in (
            ("Check reachability",
             "bluekit -t $MAC -ct", "-ct"),
            ("Recon (capabilities + config)",
             "bluekit -t $MAC -r", "-r"),
            ("Setup check (which HW is present?)",
             "bluekit -c", "-c"),
        ):
            row = Adw.ActionRow(title=label, subtitle=subtitle)
            btn = Gtk.Button(label="Run",
                             valign=Gtk.Align.CENTER)
            btn.connect(
                "clicked",
                lambda _b, a=arg: self._run_bluekit([a]))
            row.add_suffix(btn)
            target.add(row)
        box.append(target)

        # ---- exploits (checkboxes)
        exp_group = Adw.PreferencesGroup(
            title="Auto-testable exploits",
            description="No extra hardware needed. Pick the ones "
                        "you want and press Run selected.")
        # Header row with Select All / Select None
        hdr = Adw.ActionRow(title="Selection",
                            subtitle="%d checks available"
                                     % len(_AUTO_EXPLOITS))
        all_btn = Gtk.Button(label="All",
                             valign=Gtk.Align.CENTER)
        all_btn.connect("clicked",
                        lambda _b: self._select_all(True))
        hdr.add_suffix(all_btn)
        none_btn = Gtk.Button(label="None",
                              valign=Gtk.Align.CENTER)
        none_btn.connect("clicked",
                         lambda _b: self._select_all(False))
        hdr.add_suffix(none_btn)
        exp_group.add(hdr)

        # One CheckButton per exploit inside an ActionRow.
        for exploit_id, label in _AUTO_EXPLOITS:
            row = Adw.ActionRow(title=label,
                                subtitle=exploit_id)
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            self._exploit_boxes[exploit_id] = check
            row.add_prefix(check)
            exp_group.add(row)

        run_row = Adw.ActionRow(
            title="Run selected",
            subtitle="bluekit -t <MAC> -e <ids...>")
        self.run_btn = Gtk.Button(label="Run",
                                  valign=Gtk.Align.CENTER)
        self.run_btn.add_css_class("destructive-action")
        self.run_btn.connect("clicked",
                             lambda _b: self._run_selected())
        run_row.add_suffix(self.run_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked",
                              lambda _b: self._stop())
        run_row.add_suffix(self.stop_btn)
        exp_group.add(run_row)
        box.append(exp_group)

        # ---- results / report
        report = Adw.PreferencesGroup(
            title="Results",
            description="bluekit writes per-exploit dirs under "
                        "~/toolkit/data/<MAC>/<exploit>/.")
        open_row = Adw.ActionRow(
            title="Open results folder",
            subtitle="xdg-open ~/toolkit/data/<MAC>/")
        open_btn = Gtk.Button(label="Open",
                              valign=Gtk.Align.CENTER)
        open_btn.connect("clicked",
                         lambda _b: self._open_results())
        open_row.add_suffix(open_btn)
        report.add(open_row)

        json_row = Adw.ActionRow(
            title="Generate JSON report",
            subtitle="bluekit -t $MAC -rej")
        json_btn = Gtk.Button(label="Report",
                              valign=Gtk.Align.CENTER)
        json_btn.connect("clicked",
                         lambda _b: self._run_bluekit(["-rej"]))
        json_row.add_suffix(json_btn)
        report.add(json_row)

        resume_row = Adw.ActionRow(
            title="Resume from checkpoint",
            subtitle="Picks up after a crash")
        resume_btn = Gtk.Button(label="Resume",
                                valign=Gtk.Align.CENTER)
        resume_btn.connect("clicked",
                           lambda _b: self._run_bluekit(["-ch"]))
        resume_row.add_suffix(resume_btn)
        report.add(resume_row)
        box.append(report)

        # ---- HW capabilities (info only)
        hw = Adw.PreferencesGroup(
            title="Extra-hardware exploits",
            description="These need dedicated boards. Cannot run "
                        "from the phone's built-in adapter.")
        for label, hw_needed in (
            ("KNOB (CVE-2019-9506)",       "Nexus 5 + InternalBlue"),
            ("BIAS (CVE-2020-10135)",       "Cypress CYW920819"),
            ("BLURtooth (CVE-2020-15802)",  "Cypress CYW920819"),
            ("BLUFFS (CVE-2023-24023)",     "Cypress CYW920819"),
            ("Broadcom mem exec CVE-2018-19860", "Nexus 5"),
        ):
            hw.add(Adw.ActionRow(title=label, subtitle=hw_needed))
        box.append(hw)

        # ---- log
        self.output = OutputView()
        box.append(self.output)
        return box

    # ---------------------------------------------------------- install
    def _install(self) -> None:
        script = (
            "set -e; "
            "mkdir -p /opt; "
            "if [ ! -d %s/.git ]; then "
            "  git clone --depth 1 %s %s; "
            "else "
            "  git -C %s pull --ff-only || true; "
            "fi; "
            "chmod +x %s/install.sh; "
            "%s/install.sh; "
            "chown -R kali:kali %s || true; "
            "echo ---installed---"
        ) % (BLUETOOLKIT_DIR, BLUETOOLKIT_REPO, BLUETOOLKIT_DIR,
             BLUETOOLKIT_DIR, BLUETOOLKIT_DIR,
             BLUETOOLKIT_DIR, BLUETOOLKIT_DIR)
        self.output.append("# installing bluetoolkit…\n")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            self.inst_row.set_subtitle(
                str(BLUETOOLKIT_DIR) +
                (" -- installed" if BLUEKIT_BIN.exists()
                 else " -- install failed"))
            toast(self.app_window,
                  "bluetoolkit install finished")

        run_async(["sh", "-c", script], done,
                  root=True, timeout=900)

    # ---------------------------------------------------------- run
    def _select_all(self, on: bool) -> None:
        for c in self._exploit_boxes.values():
            c.set_active(on)

    def _selected_exploits(self) -> list[str]:
        return [eid for eid, cb in self._exploit_boxes.items()
                if cb.get_active()]

    def _run_bluekit(self, extra_args: list[str]) -> None:
        if self._proc is not None and self._proc.running:
            return
        if not BLUEKIT_BIN.exists():
            toast(self.app_window,
                  "Install bluetoolkit first")
            return
        mac = self.mac.get_text().strip()
        # -c doesn't take a MAC.
        if "-c" not in extra_args and not mac:
            toast(self.app_window, "Set a target MAC first")
            return
        argv = ["sh", "-c",
                 ". %s/.venv/bin/activate && "
                 "sudo -E env PATH=$PATH bluekit"
                 % BLUETOOLKIT_DIR
                 + (" -t " + mac if "-c" not in extra_args else "")
                 + " " + " ".join(extra_args)]
        self.output.append("$ " + argv[-1] + "\n")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "bluetoolkit",
            "bluekit-%s-%s.log" % (
                mac.replace(":", "") or "check", stamp))
        loot_id = get_loot_store().record(
            module="bluetoolkit", type="bluekit_log",
            target=mac or "checksetup",
            path=log_path,
            notes="args=" + " ".join(extra_args))

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass

        def on_done(code: int) -> None:
            self.output.append("[bluekit exited: %d]\n" % code)
            self._proc = None
            self.run_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            get_loot_store().refresh_size(loot_id)

        self._proc = Process(argv, on_line, on_done,
                              root=False)
        self._proc.start()
        self.run_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _run_selected(self) -> None:
        sel = self._selected_exploits()
        if not sel:
            toast(self.app_window,
                  "Pick at least one exploit checkbox")
            return
        self._run_bluekit(["-e", " ".join(sel)])

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()

    def _open_results(self) -> None:
        mac = self.mac.get_text().strip()
        if not mac:
            path = os.path.expanduser("~/toolkit/data")
        else:
            path = os.path.expanduser("~/toolkit/data/") + mac
        run_async(
            ["sh", "-c", "xdg-open " + path + " >/dev/null 2>&1 &"],
            lambda _r: None, root=False, timeout=5)

    def set_target(self, target: str) -> None:
        if target:
            self.mac.set_text(target.split("|")[0].strip())
