#!/usr/bin/env python3
"""Watch TrustZone's log in IMEM and record every change, before the reset.

TZ is the one participant that has not been heard from. It is what deasserts
PS_HOLD -- the PMIC says so -- and neither Linux nor the modem notices anything
wrong when it happens. Its log lives in IMEM at 0xc125720 and contains
subsystem names in plain text, so if it writes something before pulling the
plug, it is written here.

It cannot be read afterwards. The reset is a power cycle and IMEM is on-chip
SRAM on the same rails, so this has to be polled while the machine still runs,
with every change flushed to flash before the next read.

Only the part inside the 4 KiB IMEM page is readable: 0xc125720 to 0xc126000.
The vendor declares 0x3000 for the node and reading that much freezes the
phone. rhodep_dbgmem enforces the smaller window.

    sudo insmod rhodep_dbgmem.ko
    sudo rhodep-tzlog-watch.py --log /var/log/rhodep-tzlog.log &
    ... then do whatever kills the machine
"""

import argparse
import hashlib
import os
import sys
import time

SRC = "/sys/kernel/debug/rhodep_dbgmem/tzlog_imem"
BODY = "/sys/kernel/debug/rhodep_dbgmem/tzlog_body"


def uptime():
    with open("/proc/uptime") as f:
        return f.read().split()[0]


def printable(b):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/var/log/rhodep-tzlog.log")
    ap.add_argument("--source", default=SRC,
                    help="which region to watch; pass %s for the diagnostic "
                         "structure with reset_info and the log ring" % BODY)
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--seconds", type=float, default=600.0)
    args = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("needs root")
    src = args.source
    if not os.path.exists(src):
        raise SystemExit("%s is not there; insmod rhodep_dbgmem.ko first" % src)

    out = open(args.log, "w")

    def say(m):
        out.write("[%s] %s\n" % (uptime(), m))
        out.flush()
        os.fsync(out.fileno())

    def grab():
        with open(src, "rb") as f:
            return f.read()

    prev = grab()
    say("watching %d bytes every %.0f ms; %d printable chars at the start"
        % (len(prev), args.interval * 1000,
           sum(1 for c in prev if 32 <= c < 127)))
    say("initial digest %s" % hashlib.sha256(prev).hexdigest()[:16])

    end = time.time() + args.seconds
    changes = 0
    while time.time() < end:
        cur = grab()
        if cur != prev:
            changes += 1
            diffs = [i for i in range(min(len(cur), len(prev)))
                     if cur[i] != prev[i]]
            say("CHANGE %d at %d offsets, first %s" %
                (changes, len(diffs), diffs[:8]))
            # Dump the neighbourhood of the first change, both ways round.
            o = (diffs[0] // 16) * 16 if diffs else 0
            say("  was %s | %s" % (prev[o:o+32].hex(), printable(prev[o:o+32])))
            say("  now %s | %s" % (cur[o:o+32].hex(), printable(cur[o:o+32])))
            prev = cur
        time.sleep(args.interval)
    say("SURVIVED %.0fs, %d changes" % (args.seconds, changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
