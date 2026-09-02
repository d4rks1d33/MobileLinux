#!/bin/sh
# build-kernel-headers.sh
#
# Build a linux-headers-<KVER>_*.deb (aarch64) that matches the installed
# rhodep kernel, so out-of-tree / DKMS modules (e.g. realtek rtl8188eus for
# the TP-Link RTL8188EUS USB Wi-Fi) can be compiled on-device in Kali.
#
# The pmbootstrap apk ships vmlinuz + dtbs + modules but NOT the headers.
# This produces the equivalent of /usr/src/linux-headers-<KVER> with:
#   - headers built from the SAME .config as the installed kernel,
#   - a full Module.symvers (incl. mac80211/cfg80211 symbols),
#   - aarch64-native scripts/ (modpost, fixdep),
#   - /lib/modules/<KVER>/build -> that tree.
#
# Requirements (build host): the linux source tarball, an aarch64 cross
# toolchain (aarch64-linux-gnu-gcc) or native aarch64, bc bison flex libssl
# libelf. Runs the full kernel build once to generate Module.symvers.
#
# Usage:
#   KSRC=/path/to/linux-7.2-rc5 CONFIG=/path/to/config-motorola-rhodep.aarch64 \
#       ./build-kernel-headers.sh
#
# Env:
#   KVER    kernel release string (default: 7.2.0-rc5)
#   KSRC    kernel source tree (already patched with the rhodep patches)
#   CONFIG  the kernel .config to use (must match the installed kernel)
#   CROSS   cross-compile prefix (default: aarch64-linux-gnu-)
#   DEBVER  .deb version (default: 7.2~rc5-rhodep2)

set -e

KVER="${KVER:-7.2.0-rc5}"
CROSS="${CROSS:-aarch64-linux-gnu-}"
DEBVER="${DEBVER:-7.2~rc5-rhodep2}"
: "${KSRC:?set KSRC to the (patched) kernel source tree}"
: "${CONFIG:?set CONFIG to the kernel .config matching the installed kernel}"

echo ">> Kernel: $KVER   src: $KSRC"
cp "$CONFIG" "$KSRC/.config"

cd "$KSRC"
# BTF tools need host libelf; the module symbols don't require BTF, so drop it
# for this headers build only. Does NOT affect the installed kernel.
sed -i 's/CONFIG_DEBUG_INFO_BTF=y/# CONFIG_DEBUG_INFO_BTF is not set/' .config || true
make ARCH=arm64 CROSS_COMPILE="$CROSS" olddefconfig

echo ">> Full build (generates Module.symvers)..."
make -j"$(nproc)" ARCH=arm64 CROSS_COMPILE="$CROSS" Image modules

DEST="/tmp/khdr/usr/src/linux-headers-$KVER"
rm -rf /tmp/khdr
mkdir -p "$DEST"

echo ">> Collecting headers..."
# Makefiles, Kconfigs and shell scripts across the whole tree
find . \( -name Makefile\* -o -name Kconfig\* -o -name "*.sh" \) \
    -exec cp --parents {} "$DEST/" \; 2>/dev/null || true
cp -a include "$DEST/"
mkdir -p "$DEST/arch/arm64"
cp -a arch/arm64/include "$DEST/arch/arm64/"
cp -a arch/arm64/Makefile "$DEST/arch/arm64/" 2>/dev/null || true
cp -a scripts "$DEST/"
cp Module.symvers "$DEST/"
cp .config "$DEST/"
cp System.map "$DEST/" 2>/dev/null || true
# trim intermediates but keep the built helper binaries (modpost/fixdep)
find "$DEST/scripts" -name "*.o" -delete 2>/dev/null || true
find "$DEST" -name "*.cmd" -delete 2>/dev/null || true

# /lib/modules/<KVER>/build -> headers tree
mkdir -p "/tmp/khdr/lib/modules/$KVER"
ln -sf "/usr/src/linux-headers-$KVER" "/tmp/khdr/lib/modules/$KVER/build"
ln -sf "/usr/src/linux-headers-$KVER" "/tmp/khdr/lib/modules/$KVER/source"

# Debian metadata
mkdir -p /tmp/khdr/DEBIAN
ISIZE=$(du -sk /tmp/khdr/usr | cut -f1)
cat > /tmp/khdr/DEBIAN/control <<EOF
Package: linux-headers-$KVER
Source: linux-motorola-rhodep
Version: $DEBVER
Architecture: arm64
Maintainer: rhodep port <nethunter@localhost>
Installed-Size: $ISIZE
Depends: gcc, make, libc6-dev, bc, flex, bison, libssl-dev, libelf-dev
Provides: linux-headers, linux-headers-2.6
Section: kernel
Priority: optional
Description: Header files for Linux $KVER (Motorola rhodep, NetHunter)
 Kernel headers for $KVER aarch64, same .config as the installed kernel, plus
 Module.symvers (mac80211/cfg80211). Enables DKMS modules such as realtek
 rtl8188eus. Creates /lib/modules/$KVER/build.
EOF
cat > /tmp/khdr/DEBIAN/postinst <<EOF
#!/bin/sh
set -e
KVER=$KVER
SRC=/usr/src/linux-headers-\$KVER
if [ -d /lib/modules/\$KVER ]; then
    ln -sf "\$SRC" /lib/modules/\$KVER/build
    ln -sf "\$SRC" /lib/modules/\$KVER/source
fi
# touch generated files so kbuild does not try to rebuild scripts on-device
if [ -d "\$SRC" ]; then
    find "\$SRC/include/config" "\$SRC/include/generated" -type f -exec touch {} + 2>/dev/null || true
    find "\$SRC/scripts" -type f -exec touch {} + 2>/dev/null || true
    touch "\$SRC/Module.symvers" 2>/dev/null || true
fi
exit 0
EOF
chmod 0755 /tmp/khdr/DEBIAN/postinst

OUT="linux-headers-${KVER}_${DEBVER}_arm64.deb"
fakeroot dpkg-deb --build -Zxz /tmp/khdr "$OUT"
echo ">> Built $OUT"
