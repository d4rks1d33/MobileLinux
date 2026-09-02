#!/usr/bin/env python3
"""Read TrustZone's interrupt counters by name, and watch them for changes.

TZ's diagnostic structure carries a table of the interrupts it handles, each
with a name and a per-CPU count. The names are the interesting part:

    CPUWakeUp  SGITAWDog  SGIFtlErr  SGITeeNotifier  SGIReset
    SWDogBark  NSWDogBite  RPMWDogBite  RPMErrInd  SPI VMIDMT ERR

"SPI VMIDMT ERR" is a VMID mapping table error -- an access made with a stream
ID that is not allowed to make it. That is the shape of fault this port has
been circling: something is touching what it may not touch, and the machine is
switched off without either Linux or the modem noticing.

Watching raw bytes was too blunt: counters elsewhere in the structure tick
constantly and drown the signal. This parses the table instead, so a change can
be reported as "SPI VMIDMT ERR went from 0 to 1 on CPU 3" rather than as an
offset.

    sudo insmod rhodep_dbgmem.ko allow_tzlog_body=1
    sudo rhodep-tz-interrupts.py                 # print the table once
    sudo rhodep-tz-interrupts.py --watch --log /var/log/tzint.log &
    ... then do the thing that kills it

Only the first 0x3000 of the region answers; past that the AP hangs. The module
enforces it, and this only reads what the header points at anyway.
"""

import argparse
import os
import struct
import sys
import time

SRC = "/sys/kernel/debug/rhodep_dbgmem/tzlog_body"
# struct tzdbg_int_t: u16 int_info, u8 avail, u8 spare, u32 int_num,
#                     char desc[16], u64 int_count[cpu_count]
DESC_LEN = 16


def read_all(path, size=0x3000):
    with open(path, "rb") as f:
        return f.read(size)


def parse(blob):
    """Walk TZ's interrupt table.

    The layout was measured rather than assumed, because the downstream struct
    definition does not match what is here. Names sit 56 bytes apart starting
    at 0x23c, and int_info_off from the header is 0x234, so each entry is:

        +0   u32  interrupt number
        +4   u32  info/availability
        +8   char desc[16]
        +24  u32  count[8]      one per CPU, 32-bit, not 64

    56 bytes in total, which is what the spacing between names says.
    """
    magic, version, cpu_count = struct.unpack_from("<III", blob, 0)
    if magic != 0x747A6461:
        raise SystemExit("no tzda magic at the start of %s (got %#x)"
                         % (SRC, magic))
    offs = struct.unpack_from("<8I", blob, 12)
    int_off = offs[3]
    entry = 8 + DESC_LEN + 4 * cpu_count
    out = []
    off = int_off
    while off + entry <= len(blob):
        num, info = struct.unpack_from("<II", blob, off)
        name = blob[off + 8:off + 8 + DESC_LEN].split(b"\0")[0]
        try:
            name = name.decode("ascii").strip()
        except UnicodeDecodeError:
            break
        if not name or not name.isprintable():
            break
        counts = struct.unpack_from("<%dI" % cpu_count, blob, off + 8 + DESC_LEN)
        out.append((num, name, counts))
        off += entry
    return version, cpu_count, out


def show(entries):
    for num, name, counts in entries:
        total = sum(counts)
        mark = "   <-- nonzero" if total else ""
        print("  %-18s irq %-5d total %-6d %s%s"
              % (name, num, total, list(counts), mark))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--log", default="/var/log/rhodep-tz-interrupts.log")
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--seconds", type=float, default=600.0)
    args = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("needs root")
    if not os.path.exists(SRC):
        raise SystemExit("%s missing; insmod rhodep_dbgmem.ko allow_tzlog_body=1"
                         % SRC)

    version, cpus, entries = parse(read_all(SRC))
    if not args.watch:
        print("TrustZone diag version %#x, %d CPUs, %d interrupts"
              % (version, cpus, len(entries)))
        show(entries)
        return 0

    out = open(args.log, "w")

    def say(m):
        out.write("[%s] %s\n" % (open("/proc/uptime").read().split()[0], m))
        out.flush()
        os.fsync(out.fileno())

    say("watching %d interrupts every %.0f ms" % (len(entries), args.interval * 1000))
    for num, name, counts in entries:
        if sum(counts):
            say("  start: %-18s = %d" % (name, sum(counts)))
    prev = {n: c for _, n, c in entries}
    end = time.time() + args.seconds
    while time.time() < end:
        try:
            _, _, now = parse(read_all(SRC))
        except Exception as e:
            say("parse failed: %s" % e)
            break
        for num, name, counts in now:
            if prev.get(name) != counts:
                say("CHANGE %-18s %s -> %s" % (name, list(prev.get(name, ())), list(counts)))
                prev[name] = counts
        time.sleep(args.interval)
    say("SURVIVED %.0fs" % args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
