#!/usr/bin/env python3
"""Drain the secure monitor call ring to flash, so the last calls survive.

rhodep_scmtrace keeps the calls in memory and the reset is a power cycle, so
the ring has to be copied out as it fills. Every new call is written and
fsync'd before the next read; the last lines in the log are the last things
the machine asked the secure world to do.

    sudo insmod rhodep_scmtrace.ko
    sudo rhodep-scmtrace-watch.py --log /var/log/rhodep-scm.log &

Service 5 is the I/O service, commands 1 and 2 being register read and write.
A reset request would be service 1, the boot service.
"""

import argparse
import os
import sys
import time

SRC = "/sys/kernel/debug/rhodep_scmtrace/calls"

SVC = {0x1: "BOOT", 0x2: "PIL", 0x3: "UTIL", 0x4: "TZ", 0x5: "IO",
       0x6: "INFO", 0x7: "SSAPI", 0xb: "MP", 0xc: "OCMEM", 0xd: "ES",
       0x12: "HDCP", 0x14: "SMMU", 0x1e: "QSEECOM"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/var/log/rhodep-scm.log")
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--seconds", type=float, default=600.0)
    args = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("needs root")
    if not os.path.exists(SRC):
        raise SystemExit("%s missing; insmod rhodep_scmtrace.ko first" % SRC)

    out = open(args.log, "w")

    def say(m):
        out.write("[%s] %s\n" % (open("/proc/uptime").read().split()[0], m))
        out.flush()
        os.fsync(out.fileno())

    def grab():
        with open(SRC) as f:
            lines = f.read().splitlines()
        total = 0
        if lines and lines[0].startswith("#"):
            try:
                total = int(lines[0].split()[1])
            except (IndexError, ValueError):
                pass
        return total, [l for l in lines if not l.startswith("#")]

    seen, rows = grab()
    say("draining every %.0f ms; %d calls already recorded" %
        (args.interval * 1000, seen))
    for r in rows[-5:]:
        say("  earlier: %s" % r)

    end = time.time() + args.seconds
    while time.time() < end:
        total, rows = grab()
        if total > seen:
            new = rows[-(total - seen):] if total - seen <= len(rows) else rows
            for r in new:
                f = r.split()
                if len(f) >= 3:
                    try:
                        svc = int(f[1], 0)
                        r += "    (%s)" % SVC.get(svc, "svc %#x" % svc)
                    except ValueError:
                        pass
                say("CALL %s" % r)
            seen = total
        time.sleep(args.interval)
    say("SURVIVED %.0fs, %d calls" % (args.seconds, seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
