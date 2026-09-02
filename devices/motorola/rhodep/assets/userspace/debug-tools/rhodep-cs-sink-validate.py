#!/usr/bin/python3
"""Use the ETF's own SRAM as the sink. No DDR, no AXI: isolates the funnel path."""
import mmap, os, struct, time
TMC_ETF, TMC_ETR = 0x8047000, 0x8048000
CS_LAR, KEY = 0xfb0, 0xc5acce55
RSZ, STS, RRD, RRP, RWP, CTL, MODE, FFCR = 0x004, 0x00c, 0x010, 0x014, 0x018, 0x020, 0x028, 0x304
STM_BASE, STM_STIM = 0x8002000, 0xe280000

def blk(pa, size=0x1000, w=True):
    fd = os.open("/dev/mem", (os.O_RDWR if w else os.O_RDONLY) | os.O_SYNC)
    p = mmap.PROT_READ | (mmap.PROT_WRITE if w else 0)
    return fd, mmap.mmap(fd, size, mmap.MAP_SHARED, p, offset=pa)
def rd(m,o): return struct.unpack("<I", m[o:o+4])[0]
def wr(m,o,v): m[o:o+4] = struct.pack("<I", v & 0xffffffff)

# stop the ETR so it is not competing for the stream
fd,m = blk(TMC_ETR); wr(m,CS_LAR,KEY); wr(m,CTL,0); wr(m,CS_LAR,0); m.close(); os.close(fd)

fd,m = blk(TMC_ETF); wr(m,CS_LAR,KEY)
wr(m,CTL,0)
for _ in range(200):
    if rd(m,STS) & 0x4: break
    time.sleep(0.001)
print("etf RSZ=0x%x (=%d bytes of SRAM)" % (rd(m,RSZ), rd(m,RSZ)*4))
wr(m,MODE,0x0)          # circular buffer: ETF is the sink
wr(m,FFCR,0x133)
wr(m,CTL,1)
print("etf enabled: MODE=%d CTL=%d STS=0x%08x RWP=0x%08x" % (rd(m,MODE),rd(m,CTL),rd(m,STS),rd(m,RWP)))
before = rd(m,RWP)
m.close(); os.close(fd)

fds,ms = blk(STM_STIM, 0x1000)
for i in range(4000):
    ms[0:4] = struct.pack("<I", 0xC0DE0000 | (i & 0xffff))
ms.close(); os.close(fds)
time.sleep(0.3)

fd,m = blk(TMC_ETF); wr(m,CS_LAR,KEY)
after = rd(m,RWP)
print("etf RWP  %08x -> %08x   STS=0x%08x" % (before, after, rd(m,STS)))
if after != before or (rd(m,STS) & 0x1):
    wr(m,CTL,0)
    for _ in range(200):
        if rd(m,STS) & 0x4: break
        time.sleep(0.001)
    wr(m,MODE,0x0); wr(m,RRP,0)
    words = [rd(m,RRD) for _ in range(32)]
    print("etf SRAM:", " ".join("%08x" % w for w in words))
else:
    print("NO TRACE reached the ETF")
wr(m,CS_LAR,0); m.close(); os.close(fd)
