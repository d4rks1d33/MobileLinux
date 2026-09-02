#!/usr/bin/env python3
"""Open one DIAG glink channel and listen, without sending anything.

Qualcomm's diag driver is not in mainline, so nothing on this phone ever asks
the modem for a diag channel, which is why none is advertised. Asking directly
for DIAG_DATA made the modem assert:

    fatal error received: glink_channel_migration.c:602:
      Assertion status == GLINK_STATUS_SUCCESS failed

so the name is recognised and the request goes somewhere real. Downstream opens
the control channel before the data one, and this tries them one at a time and
listens rather than sending, because the first thing to learn is which channels
open at all and what the modem says unprompted.

Every line is fsync'd: a failed attempt crashes the modem, and on this port a
modem crash used to take the SoC with it.

    sudo rhodep-diag-probe.py --channel DIAG_CNTL --seconds 10

One attempt per boot is the honest way to run this. A crashed modem leaves IPA
un-setup until reboot, so a second attempt is not measuring the same machine.
"""

import argparse
import fcntl
import glob
import os
import struct
import sys
import time

CREATE_EPT = (1 << 30) | (40 << 16) | (0xB5 << 8) | 0x01
DESTROY_EPT = (0xB5 << 8) | 0x02
MODEM_EDGE = "6080000.remoteproc"

# The names downstream uses, in the order it brings them up.
KNOWN = ["DIAG_CNTL", "DIAG_DATA", "DIAG_CMD", "DIAG_DCI_CMD", "DIAG_DCI_DATA"]


def uptime():
    with open("/proc/uptime") as f:
        return f.read().split()[0]


def modem_ctrl():
    for c in sorted(glob.glob("/sys/class/rpmsg/rpmsg_ctrl*")):
        try:
            if MODEM_EDGE in os.path.realpath(c):
                dev = "/dev/" + os.path.basename(c)
                if os.path.exists(dev):
                    return dev
        except OSError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="DIAG_CNTL",
                    help="one of %s, or any name" % ", ".join(KNOWN))
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--log", default="/var/log/rhodep-diag-probe.log")
    args = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("needs root")

    out = open(args.log, "a")

    def say(m):
        out.write("[%s] %s\n" % (uptime(), m))
        out.flush()
        os.fsync(out.fileno())

    def rproc():
        try:
            with open("/sys/class/remoteproc/remoteproc0/state") as f:
                return f.read().strip()
        except OSError:
            return "?"

    say("=== probing %s, modem is %s" % (args.channel, rproc()))
    ctrl = modem_ctrl()
    if not ctrl:
        say("no modem rpmsg control device")
        return 1
    say("control device %s" % ctrl)

    for dev in sorted(glob.glob("/dev/rpmsg[0-9]*")):
        try:
            fd = os.open(dev, os.O_RDWR)
            fcntl.ioctl(fd, DESTROY_EPT)
            os.close(fd)
            say("cleared leftover %s" % dev)
        except OSError:
            pass
    time.sleep(0.3)

    before = set(glob.glob("/dev/rpmsg[0-9]*"))
    say("creating an endpoint for %s" % args.channel)
    try:
        fd = os.open(ctrl, os.O_RDWR)
        fcntl.ioctl(fd, CREATE_EPT,
                    struct.pack("32sII", args.channel.encode(),
                                0xFFFFFFFF, 0xFFFFFFFF))
        os.close(fd)
    except OSError as e:
        say("create failed: %s   (modem is %s)" % (e, rproc()))
        return 1

    dev = None
    for _ in range(30):
        time.sleep(0.1)
        new = sorted(set(glob.glob("/dev/rpmsg[0-9]*")) - before)
        if new:
            dev = new[-1]
            break
    if not dev:
        say("no device appeared   (modem is %s)" % rproc())
        return 1
    say("device %s appeared" % dev)

    try:
        efd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        say("OPEN FAILED: %s   (modem is %s)" % (e, rproc()))
        say("that is the modem refusing the channel, or asserting on it")
        return 1

    say("OPENED %s -- the modem accepted the channel" % args.channel)
    end = time.time() + args.seconds
    got = 0
    while time.time() < end:
        try:
            data = os.read(efd, 4096)
        except BlockingIOError:
            time.sleep(0.05)
            continue
        except OSError as e:
            say("read failed: %s   (modem is %s)" % (e, rproc()))
            break
        if data:
            got += len(data)
            say("RX %d bytes: %s" % (len(data), data[:64].hex()))
            printable = "".join(chr(c) if 32 <= c < 127 else "."
                                for c in data[:64])
            say("      as text: %s" % printable)
    say("listened %.0fs, %d bytes, modem is %s" % (args.seconds, got, rproc()))

    try:
        fcntl.ioctl(efd, DESTROY_EPT)
    except OSError:
        pass
    os.close(efd)
    say("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
