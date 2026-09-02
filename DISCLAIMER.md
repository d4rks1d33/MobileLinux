# Disclaimer — use at your own risk

**MobileLinux flashes, repartitions and modifies mobile devices. You use it
entirely AT YOUR OWN RISK.**

## No liability

To the maximum extent permitted by law, the authors, maintainers and
contributors of MobileLinux accept **NO responsibility and NO liability** for
any damage, loss or harm of any kind arising from the use (or misuse) of this
software, including but not limited to:

- **bricked, bootlooping, or otherwise unusable devices;**
- **lost, corrupted or wiped data;**
- voided manufacturer warranties;
- damaged hardware (battery, storage, radios, etc.);
- any direct, indirect, incidental, special or consequential damages.

This software is provided **"AS IS", without warranty of any kind**, express or
implied, including but not limited to the warranties of merchantability, fitness
for a particular purpose and non-infringement. See the [LICENSE](LICENSE)
(GPL-3.0-or-later), sections 15–17 (Disclaimer of Warranty / Limitation of
Liability), which govern.

## Why the risk is *lower* here (but never zero)

By design, MobileLinux is conservative and **does not touch the bootloader
itself**:

- It never flashes, updates or erases the bootloader / `aboot` / GPT of the
  device.
- It only writes the partitions a device explicitly declares in its definition
  (e.g. `boot_a` and `userdata` for `rhodep`), and shows you exactly which ones
  before doing anything.
- Every destructive operation requires explicit confirmation, supports
  `--dry-run`, verifies image hashes, and refuses to run another device's
  commands (wrong-device protection).
- Most supported devices retain a working **fastboot / download / rescue**
  entry point, so a bad flash can normally be recovered by re-flashing a good
  boot image and re-writing the rootfs (see [docs/recovery.md](docs/recovery.md)).

**However, "normally recoverable" is not "guaranteed recoverable."** Hardware
faults, power loss at the wrong moment, locked/quirky bootloaders, vendor
anti-rollback, wrong images, user error, or simply bad luck can still leave a
device in a state you cannot recover. Some vendors also blow fuses / trip
warranty bits on unlock. If you are not prepared to potentially lose the device,
**do not flash it.**

## Your responsibilities

Before flashing anything, you are responsible for:

- **backing up everything you care about** — flashing typically erases user data;
- confirming the connected device matches the definition you selected;
- understanding your device's unlock, rescue and recovery procedure
  *before* you start (read [docs/recovery.md](docs/recovery.md));
- ensuring the device is adequately charged and the connection is stable;
- complying with all laws applicable to you, including around the security /
  pentest tooling shipped by some distros (e.g. Kali/NetHunter). Use it only on
  systems you own or are explicitly authorized to test.

By using MobileLinux you acknowledge that you have read and accepted this
disclaimer.
