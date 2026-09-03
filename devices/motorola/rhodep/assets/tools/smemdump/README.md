# smemdump

Reads a Qualcomm SMEM item and prints it to the kernel log. Written to recover a
remoteproc crash reason: a subsystem writes its failure reason into SMEM before
dying, and SMEM is a nomap reserved region in DDR, so it survives a warm reset
and can be read on the next boot.

Item ids are the `crash_reason_smem` values from
`drivers/remoteproc/qcom_q6v5_pas.c`: **421** modem, **423** adsp, 424 cdsp.
sm6375 uses the sm6350 data, so the ADSP is 423.

`/dev/mem` cannot be used instead: `CONFIG_STRICT_DEVMEM` is enabled and reading
the SMEM region returns 0 bytes.

## Build it on the phone

No cross build and no reflash: `linux-headers-7.2.0-rc5` is installed (and held),
and gcc/make are present.

```
mkdir -p /root/smemdump && cd /root/smemdump
cp smemdump.c .
printf 'obj-m += smemdump.o\n' > Makefile
make -C /lib/modules/$(uname -r)/build M=$PWD modules
```

## Use

```
sudo insmod ./smemdump.ko item=423
sudo rmmod smemdump
dmesg | grep smemdump
```

Everything is printed at KERN_ERR so it also lands in the pstore/ramoops console,
which means it can be read even if the next thing that happens is a reset.

Example, after the ADSP died during an audio stream:

```
smemdump: item 423: 256 bytes
smemdump: item 423 text: "SFR Init: wdog or kernel error suspected."
```

That is the string the ADSP writes at its next init when the previous shutdown
was not clean. No file and no line means it never reached a fault handler: it
hung, and its watchdog bit. See
`docs/interconnect-sm6375-wip/AUDIO-SM6375.md` §4.
