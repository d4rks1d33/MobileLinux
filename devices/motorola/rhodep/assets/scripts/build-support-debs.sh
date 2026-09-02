#!/bin/sh
# Build the support .deb packages from the sources in packages/.
# Requires: fakeroot, dpkg-deb. Run from the repo root.
set -e
cd "$(dirname "$0")/../packages"
for pkg in rhodep-modem-support rhodep-usb-otg rhodep-battery-jeita \
           rhodep-phosh-wifi-guard rhodep-gpu-opencl rhodep-gpu-vulkan; do
    ver=$(grep '^Version:' "$pkg/DEBIAN/control" | awk '{print $2}')
    out="../${pkg}_${ver}_arm64.deb"
    echo "Building $out"
    find "$pkg" -type d -exec chmod 0755 {} \;
    fakeroot dpkg-deb --build -Zxz "$pkg" "$out"
done
echo ""
echo "firmware-motorola-rhodep is NOT built here (needs vendor blobs)."
echo "See packages/firmware-motorola-rhodep/README.md"
