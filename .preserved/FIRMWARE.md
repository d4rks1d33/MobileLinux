# Vendor firmware for Moto G82 5G (rhodep)

Some firmware for this device is **proprietary and NOT redistributable**, so it is not shipped in this repository. You must extract it from your own device (from its stock vendor partitions) and place it in the firmware package before building, OR install it on the running phone.

Target firmware package: `firmware-motorola-rhodep` (files land under `/usr/lib/firmware/...`).

## Blobs to extract

| Firmware file | For | Source |
|---------------|-----|--------|
| `ath10k/WCN3990/hw1.0/board-2.bin` | wifi | stock vendor |
| `ath10k/WCN3990/hw1.0/firmware-5.bin` | wifi | stock vendor |
| `qca/crbtfw21.tlv` | bluetooth | stock vendor |
| `qca/crnv21.bin` | bluetooth | stock vendor |
| `qcom/a619_gmu.bin` | gpu | stock vendor |
| `qcom/a630_sqe.fw` | gpu | stock vendor |
| `qcom/sm6375/motorola/rhodep/a615_zap.mdt` | gpu | stock vendor |
| `aw88261_acf.bin` | audio | stock vendor (super) |

## Runtime firmware mounts

Large modem/DSP blobs are served at runtime directly from a device partition (not copied into the rootfs):

- `/dev/disk/by-partlabel/modem_a` -> `/readonly/firmware` (`ext4`, `ro,nosuid,nodev,noexec`)

## How to extract them

From the stock ROM or a running Android/pmOS on the device, the blobs live under the vendor/system partitions (often inside the Android dynamic `super` partition) and the modem partition. Two common routes:

1. **From the running phone** (rescue shell or a booted Linux): mount the vendor/modem partitions read-only and copy the files listed above into the firmware package's `lib/firmware/` tree, preserving the paths in the table.
2. **From the stock firmware image**: unpack the vendor/super image (e.g. with `lpunpack` + `simg2img`) and copy the same files out.

Then rebuild the firmware package and re-run the build, or `dpkg -i` the firmware package on the device. Until the blobs are present, the corresponding hardware (WiFi/BT/GPU-zap/audio) will not initialize even though all the software is in place.

> These instructions are generated from the device definition's `firmware.extract_from_device`; keep that list accurate.
