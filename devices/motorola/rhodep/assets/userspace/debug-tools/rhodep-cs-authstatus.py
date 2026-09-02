#!/usr/bin/python3
"""Read CoreSight AUTHSTATUS (0xfb8) to see which debug classes the fuses allow.

Encoding per field: 0b00 not implemented, 0b10 disabled, 0b11 enabled.
  [1:0] NSNID non-secure non-invasive   [3:2] NSID non-secure invasive
  [5:4] SNID  secure non-invasive       [7:6] SID  secure invasive
"""
import mmap, os, struct
NAMES = {0b00:"n/i", 0b01:"?", 0b10:"DISABLED", 0b11:"enabled"}
BLOCKS = [(0x9040000,"etm0 (cpu0)"), (0x9740000,"etm7 (cpu7)"), (0x8002000,"stm"),
          (0x8047000,"tmc_etf"), (0x8048000,"tmc_etr"), (0x8800000,"tpdm_modem0"),
          (0x8804000,"funnel_modem0"), (0x8041000,"funnel_in0")]
for pa, name in BLOCKS:
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ, offset=pa)
    a = struct.unpack("<I", m[0xfb8:0xfbc])[0]
    d = struct.unpack("<I", m[0xfc8:0xfcc])[0]
    m.close(); os.close(fd)
    print("%-16s AUTHSTATUS=0x%02x  NSNID=%-8s NSID=%-8s SNID=%-8s SID=%-8s DEVID=0x%x"
          % (name, a, NAMES.get(a & 3), NAMES.get((a >> 2) & 3),
             NAMES.get((a >> 4) & 3), NAMES.get((a >> 6) & 3), d))
