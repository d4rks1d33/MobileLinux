# firmware-motorola-rhodep (NOT redistributable — build from your own device)

The vendor firmware blobs (WCN3990 WiFi/BT, Adreno GPU zap/gmu/sqe, modem PD
`.jsn`) are **not redistributable** and are therefore **not included** in this
repo. `FIRMWARE-FILE-LIST.txt` lists exactly which files are needed.

## How to obtain them (from your own phone / stock ROM)
These live in the stock `vendor` partition (logical partition inside `super`),
under `/firmware/` and `/bt_firmware/`. Kernel search paths:
- GPU:  `qcom/a630_sqe.fw`, `qcom/a619_gmu.bin`,
        `qcom/sm6375/motorola/rhodep/a615_zap.mdt` + `.b00..b02`
- BT:   `qca/crbtfw21.tlv`, `qca/crnv21.bin`
- WiFi: `ath10k/WCN3990/hw1.0/firmware-5.bin`, `board-2.bin`
- WLAN PD: `qcom/sm6375/motorola/rhodep/wlanmdsp.mbn` + the `.jsn` files

The large modem/adsp/cdsp blobs (`modem.mdt`+`.bNN`, etc.) are NOT packaged here:
they are served at runtime from the phone's `modem_a` partition by the
`rhodep-modem-support` package (mounts it read-only and symlinks `.mbn->.mdt`).

## Build the .deb once you have the blobs
Put the files under `lib/firmware/` following the paths in FIRMWARE-FILE-LIST.txt,
then:
```
fakeroot dpkg-deb --build -Zxz firmware-motorola-rhodep firmware-motorola-rhodep_1_arm64.deb
```
