#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Drain rhodep_qrtrtrace to flash, so the last messages survive the reset.

The LTE reset is a power cycle: RAM does not survive it, pstore is always empty
and the kernel ring buffer is gone. Anything that is to be read afterwards has
to be on flash before the machine stops, which means one fsync per batch and no
buffering anywhere in between.

Entries carry a sequence number from the kernel. A gap in it means this poll was
too slow and messages were overwritten in the ring -- that is recorded in the
log rather than passed over, because a silent gap in the final second is exactly
the thing that would mislead.

  sudo rhodep-qrtrtrace-watch.py --log /var/log/qrtr.log --interval 0.05
"""

import argparse
import os
import sys
import time

MSGS = "/sys/kernel/debug/rhodep_qrtrtrace/msgs"


def read_entries():
    try:
        with open(MSGS) as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return None, exc
    out = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        f = line.split()
        if len(f) < 9:
            continue
        try:
            out.append((int(f[0]), line))
        except ValueError:
            continue
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/var/log/qrtr.log")
    ap.add_argument("--interval", type=float, default=0.05,
                    help="seconds between polls; the ring holds 2048 entries")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after this long; 0 means run until killed")
    args = ap.parse_args()

    if not os.path.exists(MSGS):
        sys.exit("no %s -- is rhodep_qrtrtrace loaded?" % MSGS)

    started = time.time()
    last_seq = -1
    written = 0
    with open(args.log, "a", buffering=1) as log:
        log.write("# rhodep-qrtrtrace-watch started, uptime %s\n"
                  % open("/proc/uptime").read().split()[0])
        log.flush()
        os.fsync(log.fileno())
        while True:
            entries, err = read_entries()
            if err is not None:
                log.write("# read failed: %s\n" % err)
                log.flush()
                os.fsync(log.fileno())
                break
            fresh = [(s, l) for s, l in entries if s > last_seq]
            if fresh:
                lowest = fresh[0][0]
                if last_seq >= 0 and lowest > last_seq + 1:
                    log.write("# GAP: %d messages missed between seq %d and %d"
                              " -- poll too slow\n"
                              % (lowest - last_seq - 1, last_seq, lowest))
                for seq, line in fresh:
                    log.write(line + "\n")
                    last_seq = max(last_seq, seq)
                written += len(fresh)
                log.flush()
                os.fsync(log.fileno())
            if args.seconds and time.time() - started >= args.seconds:
                log.write("# stopped after %.1fs, %d messages\n"
                          % (time.time() - started, written))
                log.flush()
                os.fsync(log.fileno())
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
