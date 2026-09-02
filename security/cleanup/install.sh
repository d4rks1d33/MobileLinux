#!/bin/sh
# rhodep-cleanup: a weekly disk-space cleanup and a permanent cap on the journal.
# Run as root on the phone. Idempotent, safe to re-run.
#
# It deletes only regenerable junk (caches, coredumps, apt archives, rotated
# logs) -- nothing dpkg owns, so no apt hold is touched -- and installs a
# systemd timer to do it weekly, plus a journald drop-in so the journal cannot
# grow large again.
set -e

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

# A previous run may have made these immutable (see the protection step at the
# end), so drop the flag before overwriting or the installs fail. Fail open:
# rhodep-protect-files may not exist yet, and chattr on a missing file is fine.
for f in /usr/local/sbin/rhodep-cleanup \
	/etc/systemd/system/rhodep-cleanup.service \
	/etc/systemd/system/rhodep-cleanup.timer \
	/etc/systemd/journald.conf.d/99-rhodep-cap.conf; do
	[ -e "$f" ] && chattr -i "$f" 2>/dev/null || true
done

# ---------------------------------------------------------------- the script
install -m 0755 "$here/rhodep-cleanup" /usr/local/sbin/rhodep-cleanup
echo "rhodep-cleanup: installed /usr/local/sbin/rhodep-cleanup"

# ---------------------------------------------------------------- journald cap
install -d /etc/systemd/journald.conf.d
install -m 0644 "$here/journald/99-rhodep-cap.conf" \
	/etc/systemd/journald.conf.d/99-rhodep-cap.conf
echo "rhodep-cleanup: capped the journal at 100M"

# ---------------------------------------------------------------- systemd units
install -m 0644 "$here/systemd/rhodep-cleanup.service" \
	/etc/systemd/system/rhodep-cleanup.service
install -m 0644 "$here/systemd/rhodep-cleanup.timer" \
	/etc/systemd/system/rhodep-cleanup.timer

# ---------------------------------------------------------------- docs
install -d /usr/share/doc/rhodep-cleanup
install -m 0644 "$here/README.md" /usr/share/doc/rhodep-cleanup/README 2>/dev/null || true

# ---------------------------------------------------------------- protect
# The same protection the rest of the port uses for its non-package files: a
# snapshot plus chattr +i, so a stray rm or a well-meaning cleanup cannot take
# them out silently, and rhodep-holds-enforce restores them if they do go
# missing. Root can still edit one on purpose (chattr -i first). Fails open if
# the apt component that provides rhodep-protect-files is not installed.
if [ -x /usr/local/sbin/rhodep-protect-files ]; then
	/usr/local/sbin/rhodep-protect-files register cleanup 0755 \
		/usr/local/sbin/rhodep-cleanup 2>/dev/null || true
	/usr/local/sbin/rhodep-protect-files register cleanup-conf 0644 \
		/etc/systemd/system/rhodep-cleanup.service \
		/etc/systemd/system/rhodep-cleanup.timer \
		/etc/systemd/journald.conf.d/99-rhodep-cap.conf 2>/dev/null || true
	echo "rhodep-cleanup: files registered with rhodep-protect-files (immutable)"
else
	echo "rhodep-cleanup: rhodep-protect-files not installed; skipping immutable"
	echo "  protection (run userspace/apt/apply-holds.sh first to get it)"
fi

# ---------------------------------------------------------------- activate
# Only if systemd is actually running (during an image build it is not; the
# files are laid down and the timer comes up on first boot instead).
if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	systemctl restart systemd-journald
	systemctl enable --now rhodep-cleanup.timer
	echo "rhodep-cleanup: timer enabled; next run:"
	systemctl list-timers rhodep-cleanup.timer --no-pager | sed -n '2p'
else
	echo "rhodep-cleanup: no running systemd; enable the timer on first boot with"
	echo "  systemctl enable --now rhodep-cleanup.timer"
fi

echo "rhodep-cleanup: done. Run 'sudo rhodep-cleanup' now, or '-n' for a dry run."
