#!/usr/bin/env python3
"""Ask the modem what it is doing in the seconds before the SoC is switched off.

Everything else in this investigation watches from the AP's side, and the AP's
side has nothing to say: it answers a sampler right up to the moment it stops
and writes not one line of complaint. The modem is the one participant that has
not been asked, and it answers on the AT channel until the very end -- during
the LTE work it was still returning +CEREG 200 ms before the machine died.

So: crash the modem, wait for it to come back, open the AT channel once, and
poll it as fast as it will answer until the machine goes. The last answer in
the file is the modem's own account of its final moment.

Every line is fsync'd before the next question. The reset is a power cycle --
PS_HOLD, and the boot after calls itself a Hard Reset -- so DDR does not
survive it and anything not already on flash is gone.

    sudo rhodep-modem-lastwords.py --log /var/log/rhodep-lastwords.log

This needs the reset to actually happen, which means the STOCK ipa.ko, not the
one carrying patch 0081:

    sudo cp -a /home/kali/ipa-original.ko \\
        /lib/modules/$(uname -r)/kernel/drivers/net/ipa/ipa.ko
    sudo depmod -a && sudo reboot
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
STATE = "/sys/class/remoteproc/remoteproc0/state"
CRASH = "/sys/kernel/debug/remoteproc/remoteproc0/crash"

# Cheap questions with short answers, asked in rotation. Registration state
# first: it is the one that moved during the LTE death.
QUESTIONS = ["AT+CEREG?", "AT+CFUN?", "AT+CSQ", "AT$QCSYSMODE?"]


def uptime():
    with open("/proc/uptime") as f:
        return f.read().split()[0]


def state():
    try:
        with open(STATE) as f:
            return f.read().strip()
    except OSError as e:
        return "unreadable(%s)" % e


class Log:
    def __init__(self, path):
        self.f = open(path, "w")

    def __call__(self, msg):
        self.f.write("[%s] %s\n" % (uptime(), msg))
        self.f.flush()
        os.fsync(self.f.fileno())


MODEM_EDGE = "6080000.remoteproc"


def modem_ctrl():
    """The rpmsg control device belonging to the modem's glink edge.

    Not /dev/rpmsg_ctrl0, and not the first one found. The number depends on
    the order the remoteprocs probe and moves between boots: it has been
    rpmsg_ctrl0, 2 and 3 on this phone across a single afternoon. Match on the
    edge in the sysfs path, which is what the AT console learnt to do after
    exactly this mistake, and which this tool then repeated by guessing.
    """
    for c in sorted(glob.glob("/sys/class/rpmsg/rpmsg_ctrl*")):
        try:
            target = os.path.realpath(c)
        except OSError:
            continue
        if MODEM_EDGE in target:
            dev = "/dev/" + os.path.basename(c)
            if os.path.exists(dev):
                return dev
    return None


def open_at(ctrl, name, say):
    for dev in sorted(glob.glob("/dev/rpmsg[0-9]*")):
        try:
            fd = os.open(dev, os.O_RDWR)
            fcntl.ioctl(fd, DESTROY_EPT)
            os.close(fd)
        except OSError:
            pass
    before = set(glob.glob("/dev/rpmsg[0-9]*"))
    fd = os.open(ctrl, os.O_RDWR)
    fcntl.ioctl(fd, CREATE_EPT,
                struct.pack("32sII", name.encode(), 0xFFFFFFFF, 0xFFFFFFFF))
    os.close(fd)
    for _ in range(30):
        time.sleep(0.1)
        new = sorted(set(glob.glob("/dev/rpmsg[0-9]*")) - before)
        if new:
            break
    else:
        say("no /dev/rpmsgN appeared for %s" % name)
        return None, None
    dev = new[-1]
    try:
        return os.open(dev, os.O_RDWR), dev
    except OSError as e:
        say("%s appeared but will not open: %s" % (dev, e))
        return None, dev


def ask(fd, cmd, wait=0.8):
    os.write(fd, (cmd + "\r").encode())
    end = time.time() + wait
    out = b""
    while time.time() < end:
        try:
            chunk = os.read(fd, 512)
        except BlockingIOError:
            time.sleep(0.02)
            continue
        except OSError as e:
            return "READ FAILED: %s" % e
        if chunk:
            out += chunk
            if b"OK" in out or b"ERROR" in out:
                break
        else:
            time.sleep(0.02)
    text = out.decode("utf-8", "replace")
    return " | ".join(l.strip() for l in text.splitlines() if l.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/var/log/rhodep-lastwords.log")
    ap.add_argument("--channel", default="DS")
    ap.add_argument("--settle", type=float, default=60.0,
                    help="seconds to keep asking after the modem is back")
    ap.add_argument("--after-up", type=float, default=0.1,
                    help="seconds to wait after the modem reports running "
                         "before trying to open the channel")
    ap.add_argument("--no-crash", action="store_true",
                    help="do not crash the modem, just poll whatever is there")
    args = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("needs root")

    say = Log(args.log)
    ctrl = modem_ctrl()
    say("start rproc=%s ctrl=%s" % (state(), ctrl))
    if not ctrl:
        say("no rpmsg control device; is the modem up?")
        return 1

    if not args.no_crash:
        say("crashing the modem")
        try:
            with open(CRASH, "w") as f:
                f.write("1")
        except OSError as e:
            say("cannot crash it: %s" % e)
            return 1

        last = None
        deadline = time.time() + 60
        while time.time() < deadline:
            now = state()
            if now != last:
                say("rproc %s -> %s" % (last, now))
                last = now
            if now == "running":
                break
            time.sleep(0.1)
        else:
            say("it never came back; recovery probably hung in ipa_modem_stop")
            return 0
        # No settling time. The machine has died 1.5 s after "running" here,
        # before the channel could even be created, so try immediately and let
        # the open fail if the modem is not ready yet.
        time.sleep(args.after_up)

    fd, dev = open_at(ctrl, args.channel, say)
    if fd is None:
        say("could not open the AT channel; nothing more to ask")
        return 1
    say("AT channel open on %s; asking until the machine stops" % dev)

    i = 0
    end = time.time() + args.settle
    while time.time() < end:
        q = QUESTIONS[i % len(QUESTIONS)]
        say("%-14s -> %s" % (q, ask(fd, q)))
        i += 1
    say("SURVIVED %.0fs of questions, rproc=%s" % (args.settle, state()))
    try:
        fcntl.ioctl(fd, DESTROY_EPT)
    except OSError:
        pass
    os.close(fd)
    say("cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
