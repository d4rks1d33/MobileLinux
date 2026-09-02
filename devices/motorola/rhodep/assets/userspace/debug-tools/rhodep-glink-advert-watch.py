#!/usr/bin/env python3
"""Watch which channels the modem advertises, fast, across a modem restart.

The downstream diag driver is a passive rpmsg peer: it never opens a channel,
it waits for the modem to advertise one. So the observable for "did the modem's
diag come up" is not whether the AP can open DIAG -- opening is what asserts
the modem -- but whether a DIAG name ever appears on the mpss edge.

Every check so far has been a snapshot taken after the modem settled. A channel
that is advertised during boot and withdrawn again would not show up in one.
This polls the list itself, from sysfs rather than through the AT console, so it
is cheap enough to run at 100 ms across a whole modem restart.

It opens nothing and creates no endpoint, so it cannot assert the modem.

    sudo rhodep-glink-advert-watch.py --restart --seconds 90
"""

import argparse
import os
import sys
import time

EDGE = "6080000.remoteproc"
CRASH = "/sys/kernel/debug/remoteproc/remoteproc0/crash"
STATE = "/sys/class/remoteproc/remoteproc0/state"


def uptime():
    with open("/proc/uptime") as f:
        return f.read().split()[0]


def state():
    try:
        with open(STATE) as f:
            return f.read().strip()
    except OSError:
        return "?"


def channels():
    """Names the modem currently advertises on its glink edge.

    Device names look like

        6080000.remoteproc:glink-edge.DATA1.-1.-1

    so the channel is what sits between "glink-edge." and the two trailing
    address fields. Splitting on "." alone gets the edge name instead, which is
    what the first version of this did.
    """
    out = set()
    base = "/sys/bus/rpmsg/devices"
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for n in names:
        if not n.startswith(EDGE):
            continue
        if "glink-edge." not in n:
            continue
        out.add(n.split("glink-edge.", 1)[-1].rsplit(".", 2)[0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--restart", action="store_true",
                    help="crash the modem first, so the whole bring-up is seen")
    ap.add_argument("--log", default="/var/log/rhodep-advert.log")
    args = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("needs root")

    out = open(args.log, "w")

    def say(m):
        out.write("[%s] %s\n" % (uptime(), m))
        out.flush()
        os.fsync(out.fileno())

    seen = channels()
    say("start: %d channels, rproc=%s" % (len(seen), state()))
    say("  %s" % " ".join(sorted(seen)))

    if args.restart:
        say("crashing the modem so the whole bring-up is watched")
        try:
            with open(CRASH, "w") as f:
                f.write("1")
        except OSError as e:
            say("cannot crash it: %s" % e)
            return 1

    ever = set(seen)
    last_state = state()
    end = time.time() + args.seconds
    while time.time() < end:
        st = state()
        if st != last_state:
            say("rproc %s -> %s" % (last_state, st))
            last_state = st
        now = channels()
        if now != seen:
            gone = seen - now
            new = now - seen
            if new:
                say("APPEARED: %s" % " ".join(sorted(new)))
                ever |= new
            if gone:
                say("withdrawn: %s" % " ".join(sorted(gone)))
            seen = now
        time.sleep(args.interval)

    diag = sorted(n for n in ever if "DIAG" in n.upper())
    say("done. %d channels ever seen." % len(ever))
    say("  all: %s" % " ".join(sorted(ever)))
    say("  DIAG ever advertised: %s" % (" ".join(diag) if diag else "NO"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
