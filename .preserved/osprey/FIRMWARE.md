# Vendor firmware for Motorola Moto G 2015 (motorola-osprey)

Some firmware for this device is **proprietary and NOT redistributable**, so it is not shipped in this repository. You must extract it from your own device (from its stock vendor partitions) and place it in the firmware package before building, OR install it on the running phone.

Target firmware package: `firmware-qcom-adreno-a300` (files land under `/usr/lib/firmware/...`).

## Blobs to extract

| Firmware file | For | Source |
|---------------|-----|--------|
| `wcnss NV blob` | wifi | stock modem/vendor |
| `adreno a300 fw` | gpu | stock vendor |

## How to extract them

From the stock ROM or a running Android/pmOS on the device, the blobs live under the vendor/system partitions (often inside the Android dynamic `super` partition) and the modem partition. Two common routes:

1. **From the running phone** (rescue shell or a booted Linux): mount the vendor/modem partitions read-only and copy the files listed above into the firmware package's `lib/firmware/` tree, preserving the paths in the table.
2. **From the stock firmware image**: unpack the vendor/super image (e.g. with `lpunpack` + `simg2img`) and copy the same files out.

Then rebuild the firmware package and re-run the build, or `dpkg -i` the firmware package on the device. Until the blobs are present, the corresponding hardware (WiFi/BT/GPU-zap/audio) will not initialize even though all the software is in place.

> These instructions are generated from the device definition's `firmware.extract_from_device`; keep that list accurate.
