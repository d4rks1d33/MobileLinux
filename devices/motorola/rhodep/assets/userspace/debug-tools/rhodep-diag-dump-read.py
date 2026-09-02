#!/usr/bin/env python3
"""Read a DIAG capture written by rhodep-diag-server.py --dump.

Records are: magic 'DGPK', u32 payload length, u32 uptime in milliseconds, then
the payload. Written with an fsync per packet, so the file survives the reset
this port is chasing and its tail is the modem's own account of its last
moments.

Full decoding of DIAG F3 messages needs Qualcomm's message database, which is
not public. It is not needed to read them: this firmware's log packets carry
their text in the clear, so pulling printable runs out of the payloads gives
real modem log lines.

    rhodep-diag-dump-read.py /var/log/diag-death.bin --tail 40
"""

import argparse
import re
import struct
import sys

PRINTABLE = re.compile(rb"[ -~\t]{6,}")


def records(path):
    with open(path, "rb") as f:
        blob = f.read()
    off = 0
    while off + 12 <= len(blob):
        if blob[off:off + 4] != b"DGPK":
            off += 1
            continue
        ln, ms = struct.unpack_from("<II", blob, off + 4)
        payload = blob[off + 12:off + 12 + ln]
        yield ms / 1000.0, payload
        off += 12 + ln


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tail", type=int, default=30,
                    help="how many of the last text lines to print")
    ap.add_argument("--since", type=float, default=None,
                    help="only records at or after this uptime")
    ap.add_argument("--grep", default=None)
    args = ap.parse_args()

    lines = []
    first = last = None
    n = 0
    for ts, payload in records(args.path):
        n += 1
        if first is None:
            first = ts
        last = ts
        if args.since is not None and ts < args.since:
            continue
        for m in PRINTABLE.finditer(payload):
            text = m.group(0).decode("ascii", "replace").strip()
            if len(text) < 6:
                continue
            if args.grep and args.grep.lower() not in text.lower():
                continue
            lines.append((ts, text))

    print("%d packets, uptime %.2f to %.2f, %d text runs"
          % (n, first or 0, last or 0, len(lines)))
    print("--- last %d ---" % args.tail)
    for ts, text in lines[-args.tail:]:
        print("[%9.2f] %s" % (ts, text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
