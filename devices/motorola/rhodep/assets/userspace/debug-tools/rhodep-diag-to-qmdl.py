#!/usr/bin/env python3
"""Turn a rhodep-diag-server capture into a standard .qmdl file.

The DATA packets the modem sends are already the HDLC-framed diag stream that
Qualcomm's own tooling expects: each diag packet is followed by a CRC16 and a
0x7E terminator, exactly as in a QMDL log. So converting is concatenation --
no reframing, no reconstruction.

That matters because it hands the capture to every tool that already exists:

    SCAT            fgsect/scat, which has the version tables for every log
                    code and resolved this port's RRC header in minutes
    QCSuper         P1sec/QCSuper, which can also export to pcap for Wireshark
    MobileInsight   for the higher-level analysis

Writing our own decoder further would be reimplementing those badly. The one in
this directory stays as a zero-dependency reader for a quick look.

    rhodep-diag-to-qmdl.py /var/log/diag-death.bin diag-death.qmdl
    scat -t qc -d diag-death.qmdl ...
"""

import struct
import sys


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]

    with open(src, "rb") as f:
        blob = f.read()

    out = bytearray()
    off = n = 0
    first = last = None
    while off + 12 <= len(blob):
        if blob[off:off + 4] != b"DGPK":
            off += 1
            continue
        ln, ms = struct.unpack_from("<II", blob, off + 4)
        payload = blob[off + 12:off + 12 + ln]
        out += payload
        n += 1
        ts = ms / 1000.0
        if first is None:
            first = ts
        last = ts
        off += 12 + ln

    with open(dst, "wb") as f:
        f.write(out)

    frames = out.count(0x7E)
    print("%d captured packets, %d bytes, uptime %.2f to %.2f"
          % (n, len(out), first or 0, last or 0))
    print("%d frame terminators, so roughly that many diag packets" % frames)
    print("wrote %s" % dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
