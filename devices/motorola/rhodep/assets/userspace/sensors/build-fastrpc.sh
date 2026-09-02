#!/bin/sh
# rhodep: build Qualcomm's FastRPC userspace against the MAINLINE kernel driver.
#
# This is what makes the sensors work, and it is not a reimplementation of
# anything: quic/fastrpc is Qualcomm's own libadsprpc, BSD-3-Clause, and it has
# supported the upstream drivers/misc/fastrpc.c uapi since 2024
# (src/fastrpc_ioctl.c, "File only compiled when support to upstream kernel is
# required", -DENABLE_UPSTREAM_DRIVER_INTERFACE, on by default).
#
# What it gives us:
#   libadsprpc.so                   the FastRPC client
#   libadsp_default_listener.so     the reverse-RPC listener + apps_std server
#   rhodep-sscrpcd                  our single-shot daemon (see the patch)
#
# Build on the phone, or on any aarch64 machine with the same or older glibc.
#
#   ./build-fastrpc.sh [--install]
#
# Build deps:
#   apt install build-essential autoconf automake libtool pkg-config \
#               libyaml-dev libbsd-dev
set -e

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
WORK=${WORK:-/tmp/rhodep-fastrpc}
REPO=${REPO:-https://github.com/quic/fastrpc}
# Pinned: this is the tree the port was built and tested against.
COMMIT=${COMMIT:-228d98b}

mkdir -p "$WORK"
if [ ! -d "$WORK/fastrpc/.git" ]; then
	git clone "$REPO" "$WORK/fastrpc"
fi
cd "$WORK/fastrpc"
git fetch --all --quiet 2>/dev/null || true
git checkout --quiet "$COMMIT" 2>/dev/null || \
	echo "warning: could not check out $COMMIT, building whatever is here" >&2

# Our changes: the sensors-PD daemon, and an opt-in trace of every file the DSP
# asks the application processor for.  Idempotent.
if ! git diff --quiet -- src/rhodep_sscrpcd.c 2>/dev/null && [ -f src/rhodep_sscrpcd.c ]; then
	echo "patch already applied"
elif [ -f src/rhodep_sscrpcd.c ]; then
	echo "patch already applied"
else
	git apply "$here/patches/0001-rhodep-sensors-PD-listener-and-DSP-request-trace.patch"
	echo "applied rhodep patch"
fi

autoreconf -i
./configure --prefix=/usr/local
make -j"$(nproc)"

echo
echo "built:"
ls -1 src/.libs/libadsprpc.so.* src/.libs/libadsp_default_listener.so.* \
       src/rhodep-sscrpcd 2>/dev/null

if [ "$1" = "--install" ]; then
	make install
	ldconfig
	# The stock daemons are NOT wanted here: each libadsprpc client costs two
	# of the five ADSP FastRPC sessions, and adsprpcd + adsprpcd_audiopd
	# together leave none for the sensors PD.  Their udev rule starts them
	# automatically, so it goes.
	rm -f /usr/lib/udev/rules.d/60-fastrpc.rules
	for u in adsprpcd adsprpcd_audiopd cdsprpcd cdsp1rpcd sdsprpcd \
	         gdsp0rpcd gdsp1rpcd; do
		rm -f "/usr/lib/systemd/system/$u.service"
	done
	echo "installed (stock adsprpcd/audiopd/cdsprpcd units removed, see README)"
fi
