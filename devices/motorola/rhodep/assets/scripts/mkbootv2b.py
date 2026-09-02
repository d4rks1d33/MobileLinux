import struct, sys
PS = 4096
def pad(b):
    r = len(b) % PS
    return b + b"\0"*(PS-r) if r else b

kernel_img, dtb_f, ramdisk_f, cmdline, out = sys.argv[1:6]
k = open(kernel_img,"rb").read()
d = open(dtb_f,"rb").read()
r = open(ramdisk_f,"rb").read()
kernel = k + d          # append_dtb=true, como dubai

h = bytearray(4096)
h[0:8] = b"ANDROID!"
struct.pack_into("<I", h, 8,  len(kernel))
struct.pack_into("<I", h, 12, 0x00008000)   # kernel_addr
struct.pack_into("<I", h, 16, len(r))
struct.pack_into("<I", h, 20, 0x01000000)   # ramdisk_addr
struct.pack_into("<I", h, 24, 0)            # second_size
struct.pack_into("<I", h, 28, 0x00000000)   # second_addr
struct.pack_into("<I", h, 32, 0x00000100)   # tags_addr
struct.pack_into("<I", h, 36, PS)
struct.pack_into("<I", h, 40, 2)            # header_version
struct.pack_into("<I", h, 44, 0x16000194)   # os_version = stock
c = cmdline.encode()
h[64:64+min(len(c),511)] = c[:511]
if len(c) > 512:
    h[608:608+min(len(c)-512,1023)] = c[512:512+1023]
struct.pack_into("<I", h, 1632, 0)          # recovery_dtbo_size
struct.pack_into("<Q", h, 1636, 0)
struct.pack_into("<I", h, 1644, 1660)       # header_size
struct.pack_into("<I", h, 1648, len(d))     # dtb_size
struct.pack_into("<Q", h, 1652, 0x01f00000) # dtb_addr
img = bytes(h) + pad(kernel) + pad(r) + pad(d)
open(out,"wb").write(img)
print(out, len(img))
