#!/bin/sh
# apply-rtl8188eus-fix.sh
#
# Patch the Kali realtek-rtl8188eus-dkms driver so it builds against
# Linux 7.2 (mainline, motorola-rhodep port) and GCC 15.
#
# The upstream driver (5.3.9~git20260622.cbeae98) has three mechanical
# breakages on new kernels:
#
#   1. core/rtw_br_ext.c uses AppleTalk/AARP structs (struct elapaarp,
#      struct ddpehdr, AARP_PA_ALEN) that were removed from the kernel.
#      That is the NAT bridge helper (CONFIG_BR_EXT) and is not needed for
#      monitor mode or injection -> disable it in the Makefile.
#   2. strncpy() is no longer implicitly declared and GCC 15 fortify turns
#      the implicit builtin into an error -> replace with strscpy() (same
#      (dest, src, size) signature, always declared).
#   3. cfg80211_ops.remain_on_channel gained a trailing "const u8 *rx_addr"
#      parameter -> add it to cfg80211_rtw_remain_on_channel().
#
# The script is idempotent: safe to run more than once. Point it at the
# driver source tree (default: the DKMS source dir installed by Kali).
#
# Usage:
#   ./apply-rtl8188eus-fix.sh [/usr/src/realtek-rtl8188eus-<version>]
#
# After patching, (re)build with DKMS:
#   dkms build   -m realtek-rtl8188eus -v <version> -k $(uname -r) --force
#   dkms install -m realtek-rtl8188eus -v <version> -k $(uname -r) --force

set -e

SRC="$1"
if [ -z "$SRC" ]; then
    # auto-detect the DKMS source dir
    SRC=$(ls -d /usr/src/realtek-rtl8188eus-*/ 2>/dev/null | head -1)
fi
if [ -z "$SRC" ] || [ ! -f "$SRC/Makefile" ]; then
    echo "error: driver source not found. Pass the path, e.g." >&2
    echo "  $0 /usr/src/realtek-rtl8188eus-5.3.9~git20260622.cbeae98" >&2
    exit 1
fi
SRC="${SRC%/}"
echo "Patching rtl8188eus source: $SRC"

# --- Fix 1: disable the AppleTalk NAT bridge (removed structs) ------------
if grep -q '^CONFIG_BR_EXT = y' "$SRC/Makefile"; then
    sed -i 's/^CONFIG_BR_EXT = y/CONFIG_BR_EXT = n/' "$SRC/Makefile"
    echo "  [1/3] CONFIG_BR_EXT -> n"
else
    echo "  [1/3] CONFIG_BR_EXT already disabled (skip)"
fi

# --- Fix 2: strncpy -> strscpy across the whole tree ----------------------
FILES=$(grep -rln '\bstrncpy\b' "$SRC" 2>/dev/null || true)
if [ -n "$FILES" ]; then
    echo "$FILES" | xargs sed -i 's/\bstrncpy\b/strscpy/g'
    echo "  [2/3] strncpy -> strscpy in:"
    echo "$FILES" | sed 's/^/        /'
else
    echo "  [2/3] no strncpy left (skip)"
fi

# --- Fix 3: remain_on_channel gains a trailing const u8 *rx_addr ----------
CFG="$SRC/os_dep/linux/ioctl_cfg80211.c"
if [ -f "$CFG" ] && \
   grep -q 'unsigned int duration, u64 \*cookie)' "$CFG" && \
   grep -q 'cfg80211_rtw_remain_on_channel' "$CFG"; then
    # only the remain_on_channel signature ends with "duration, u64 *cookie)"
    sed -i 's/unsigned int duration, u64 \*cookie)/unsigned int duration, u64 *cookie, const u8 *rx_addr)/' "$CFG"
    echo "  [3/3] remain_on_channel: added const u8 *rx_addr"
else
    echo "  [3/3] remain_on_channel already patched or not present (skip)"
fi

echo "Done. Now rebuild with dkms (see header of this script)."
