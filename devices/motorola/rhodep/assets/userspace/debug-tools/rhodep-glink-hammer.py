#!/usr/bin/env python3
"""Kill the SoC through glink instead of through LTE.

Opening and closing an endpoint on the modem's DS channel, fast enough and
often enough, ends in the same reset the LTE attach produces: bootreason
watchdog, nothing in pstore, PON 088d=01 08c2=02 08c4=40. It needs no SIM, no
radio and no coverage, and it takes seconds rather than the ninety the GNSS
reproducer needs.

Every line is fsync'd to disk and echoed to /dev/kmsg before the next cycle
starts, because the reset erases anything that was merely written. The last
line in the file is where it died.

    C=$(rhodep-modem-at -l | sed -n "s/.*control device: //p")
    sudo python3 rhodep-glink-hammer.py --ctrl "$C" --cycles 500

--delay separates rate from count: the same number of cycles spread out tells
you whether it is the speed that matters or the total.
"""

import argparse
import fcntl
import glob
import os
import struct
import sys
import time
import traceback

CREATE_EPT = (1 << 30) | (40 << 16) | (0xB5 << 8) | 0x01
DESTROY_EPT = (0xB5 << 8) | 0x02
LOG_DEFAULT = "/var/log/rhodep-glink-hammer.log"


def uptime():
    with open("/proc/uptime") as f:
        return f.read().split()[0]


def rproc_state():
    try:
        with open("/sys/class/remoteproc/remoteproc0/state") as f:
            return f.read().strip()
    except OSError:
        return "?"


class Log:
    def __init__(self, path):
        self.f = open(path, "w")

    def __call__(self, msg):
        line = "[%s] %s" % (uptime(), msg)
        self.f.write(line + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())
        try:
            with open("/dev/kmsg", "w") as k:
                k.write("HAMMER: " + line + "\n")
        except OSError:
            pass


def clear_leftovers(say):
    for dev in glob.glob("/dev/rpmsg[0-9]*"):
        try:
            fd = os.open(dev, os.O_RDWR)
            fcntl.ioctl(fd, DESTROY_EPT)
            os.close(fd)
            say("cleared leftover %s" % dev)
        except OSError as e:
            say("could not clear %s: %s" % (dev, e))


def make_endpoint(ctrl, name, say):
    before = set(glob.glob("/dev/rpmsg[0-9]*"))
    fd = os.open(ctrl, os.O_RDWR)
    fcntl.ioctl(fd, CREATE_EPT,
                struct.pack("32sII", name.encode(), 0xFFFFFFFF, 0xFFFFFFFF))
    os.close(fd)
    for _ in range(20):
        time.sleep(0.1)
        new = sorted(set(glob.glob("/dev/rpmsg[0-9]*")) - before)
        if new:
            return new[-1]
    raise SystemExit("no /dev/rpmsgN appeared for %s" % name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctrl", required=True,
                    help="the modem's rpmsg control device; the number changes "
                         "between boots, so ask rhodep-modem-at -l for it")
    ap.add_argument("--cycles", type=int, default=500)
    ap.add_argument("--channel", default="DS")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds between cycles; use it to tell rate from count")
    ap.add_argument("--every", type=int, default=25, help="how often to log")
    ap.add_argument("--watch", type=float, default=30.0,
                    help="seconds to keep sampling after the burst. The reset "
                         "has twice now landed after the cycles finished, not "
                         "during them, so stopping at the last cycle is how "
                         "you miss the only part that matters")
    ap.add_argument("--log", default=LOG_DEFAULT,
                    help="where to write the fsync'd trail "
                         "(default %(default)s)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("needs root")

    say = Log(args.log)
    say("start channel=%s cycles=%d delay=%s rproc=%s"
        % (args.channel, args.cycles, args.delay, rproc_state()))
    try:
        clear_leftovers(say)
        time.sleep(0.5)
        dev = make_endpoint(args.ctrl, args.channel, say)
        say("endpoint %s" % dev)

        ok = bad = 0
        for i in range(args.cycles):
            try:
                fd = os.open(dev, os.O_RDWR)
                os.close(fd)
                ok += 1
            except OSError as e:
                bad += 1
                if bad <= 3 or bad % 50 == 0:
                    say("cycle %d failed: %s" % (i, e))
                if not os.path.exists(dev):
                    # The parent destroys an eptdev when the glink edge goes
                    # away, so the device vanishing means the modem died. That
                    # is the moment worth knowing about.
                    say("cycle %d: %s IS GONE, rproc=%s -- the edge went down"
                        % (i, dev, rproc_state()))
                    break
            if i % args.every == 0:
                say("cycle %d ok=%d bad=%d rproc=%s"
                    % (i, ok, bad, rproc_state()))
            if args.delay:
                time.sleep(args.delay)

        say("SURVIVED cycles=%d ok=%d bad=%d rproc=%s"
            % (args.cycles, ok, bad, rproc_state()))
        try:
            fd = os.open(dev, os.O_RDWR)
            fcntl.ioctl(fd, DESTROY_EPT)
            os.close(fd)
        except OSError:
            pass
        say("cleaned up")

        # The burst is not the interesting part. Both resets so far arrived
        # after it, so sit here sampling until either the modem goes or the
        # SoC does. Whichever happens, the last fsync'd line says when.
        if args.watch:
            say("watching for %.0fs: rproc state and endpoint every 200ms"
                % args.watch)
            deadline = time.time() + args.watch
            last = rproc_state()
            n = 0
            while time.time() < deadline:
                now = rproc_state()
                if now != last:
                    say("rproc %s -> %s" % (last, now))
                    last = now
                if n % 25 == 0:
                    say("watching rproc=%s endpoints=%d"
                        % (now, len(glob.glob("/dev/rpmsg[0-9]*"))))
                n += 1
                time.sleep(0.2)
            say("WATCH ENDED rproc=%s -- nothing died" % rproc_state())
    except Exception:
        say("EXCEPTION " + traceback.format_exc().replace("\n", " | "))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
