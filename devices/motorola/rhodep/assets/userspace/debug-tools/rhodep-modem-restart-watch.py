#!/usr/bin/env python3
"""Crash the modem on purpose and watch what the bring-up does to the SoC.

Two runs of the glink hammer put the reset about four seconds after the modem
finished recovering, not when it died:

    [125.16] the edge went down, rproc=crashed
    [125.45] rproc crashed -> offline
    [130.74] rproc offline -> running
             ... SoC reset within the next few seconds

and the one time recovery hung forever inside ipa_modem_stop, the SoC was
never reset at all. So the suspicion is that it is the modem coming back up
that kills the machine, not the modem dying. If that holds, the hammer is not
needed: crashing the modem through debugfs is enough, and the whole experiment
takes fifteen seconds with no SIM and no radio.

Every line is fsync'd and echoed to /dev/kmsg before the next sample, because
the reset erases anything merely written. The last line is the answer.

    sudo python3 rhodep-modem-restart-watch.py --rounds 3
"""

import argparse
import os
import sys
import time

LOG_DEFAULT = "/var/log/rhodep-modem-restart-watch.log"
STATE = "/sys/class/remoteproc/remoteproc0/state"
CRASH = "/sys/kernel/debug/remoteproc/remoteproc0/crash"


def uptime():
    with open("/proc/uptime") as f:
        return f.read().split()[0]


def netdevs():
    """Which network interfaces exist, and whether IPA's modem one is among
    them. rmnet_ipa0 is registered by ipa_modem_start(), which only runs once
    the modem has finished its QMI handshake with IPA, so its appearance is a
    userspace-visible marker for 'the modem finished setting up the data
    path'. The kernel says nothing at all in the fatal window, so this is the
    only progress signal available without rebuilding anything."""
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return "?", False
    return ",".join(names), any(n.startswith("rmnet_ipa") for n in names)


def state():
    try:
        with open(STATE) as f:
            return f.read().strip()
    except OSError as e:
        return "unreadable(%s)" % e


class Log:
    def __init__(self, path):
        self.f = open(path, "a")

    def __call__(self, msg):
        line = "[%s] %s" % (uptime(), msg)
        self.f.write(line + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())
        try:
            with open("/dev/kmsg", "w") as k:
                k.write("RESTARTWATCH: " + line + "\n")
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--settle", type=float, default=40.0,
                    help="seconds to watch after each crash")
    ap.add_argument("--log", default=LOG_DEFAULT,
                    help="where to write the fsync'd trail "
                         "(default %(default)s)")
    args = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("needs root")

    say = Log(args.log)
    say("=== new run, %d round(s), kernel %s"
        % (args.rounds, open("/proc/version").read().split()[2]))

    for r in range(1, args.rounds + 1):
        before = state()
        say("round %d: state=%s, crashing the modem" % (r, before))
        try:
            with open(CRASH, "w") as f:
                f.write("1")
        except OSError as e:
            say("round %d: cannot write %s: %s" % (r, CRASH, e))
            return 1

        last = before
        nets_last, ipa_net_last = netdevs()
        say("round %d: interfaces at the start: %s (rmnet_ipa present: %s)"
            % (r, nets_last, ipa_net_last))
        up_seen = None
        deadline = time.time() + args.settle
        n = 0
        while time.time() < deadline:
            now = state()
            if now != last:
                say("round %d: %s -> %s" % (r, last, now))
                if now == "running":
                    up_seen = time.time()
                    say("round %d: modem is up; the next few seconds are the "
                        "ones that matter" % r)
                last = now
            nets_now, ipa_net_now = netdevs()
            if ipa_net_now != ipa_net_last:
                say("round %d: rmnet_ipa %s -- the modem finished setting up "
                    "the data path" % (r, "APPEARED" if ipa_net_now else "went away"))
                ipa_net_last = ipa_net_now
            elif nets_now != nets_last:
                say("round %d: interfaces now %s" % (r, nets_now))
            nets_last = nets_now
            if up_seen and time.time() - up_seen > 20:
                say("round %d: 20s after bring-up and still alive" % r)
                break
            if n % 50 == 0:
                say("round %d: still watching, state=%s" % (r, now))
            n += 1
            time.sleep(0.2)

        if not up_seen:
            say("round %d: never came back within %.0fs, state=%s. Recovery "
                "hangs in ipa_modem_stop on this machine, so this is the "
                "no-reset case." % (r, args.settle, state()))
        time.sleep(2)

    say("SURVIVED all %d round(s)" % args.rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
