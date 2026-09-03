# camdiag

Holds the rear camera sensor powered so it can be probed by hand.

The problem this exists for: `s5kjn1.ko` powers the part, reads 0 where 0x38e1
is expected, fails probe and powers everything back down. That leaves nothing to
talk to, so every hypothesis used to cost a driver rebuild, an `scp` and a
module reload. `camdiag` binds to the same device node, brings up the rails,
MCLK and reset, and then just stays there, so `/dev/i2c-4` can be used
interactively.

Every step of the power sequence is a module parameter, and the parameters are
writable through sysfs, so varying the sequence costs an unbind and a bind.

## Build it on the phone

No cross build and no reflash: `linux-headers-7.2.0-rc5` is installed and held.

```
mkdir -p /root/camdiag && cd /root/camdiag
cp camdiag.c .
printf 'obj-m += camdiag.o\n' > Makefile
make -C /lib/modules/$(uname -r)/build M=$PWD modules
```

If that stops with `/lib/modules/7.2.0-rc5/build: No such file or directory`,
the symlink is missing because the modules were reinstalled after the headers:

```
ln -sfn /usr/src/linux-headers-7.2.0-rc5 /lib/modules/7.2.0-rc5/build
```

## Use

```
rmmod s5kjn1                       # it owns the same node
insmod /root/camdiag/camdiag.ko
i2ctransfer -f -y 4 w2@0x56 0x00 0x00 r2      # read register 0x0000
```

`-f` is needed because the address belongs to a bound driver. Reads are
two-byte register address, two-byte data, which is what the sensor and the
EEPROM both use.

Changing the sequence without reloading:

```
echo 200 > /sys/module/camdiag/parameters/post_reset_ms
echo 4-0056 > /sys/bus/i2c/drivers/camdiag/unbind
echo 4-0056 > /sys/bus/i2c/drivers/camdiag/bind
```

Parameters: `pre_reset_ms`, `post_rail_ms`, `post_mclk_ms`, `post_reset_ms`,
`mclk_hz`, `assert_reset_first`, `mclk_before_reset`, `release_reset`.
The defaults are the vendor's own sequence, the one written out in DT form in
`yupik-camera-sensor-rb3.dtsi`: assert reset, 1 ms, rails, MCLK at 24 MHz, 1 ms,
release reset, 18 ms.

Scripts, all of which discover the bus and device themselves:

	camprobe.sh   the first registers across several power-up sequences
	camclk.sh     does the sensor need MCLK to answer? (it does)
	camshape.sh   repeated-START vs write-STOP-then-read (no difference)
	cammap.sh     which register addresses answer, and whether writes stick
	campmic.sh    dump the FAN53870 with the camera rails on and off

Each prints the EEPROM as a control, because a result from the sensor alone
cannot be told apart from a broken rig.

## Two traps that produce completely wrong answers

Both were hit here, and both looked like real discoveries for a few minutes.

**`rmmod camdiag` intermittently returns `EBUSY` and leaves the module loaded
and bound.** A power-cycle test written as `rmmod; scan` then measures a part
that is still fully powered, and reports that an unpowered sensor still answers
on I2C. Always confirm with the rails and the PMIC's own enable register:

```
cat /sys/class/regulator/*/name | ...            # cam_vio/cam_vana/cam_vdig
i2cget -f -y 0 0x35 0x03                         # 0x24 = camera rails off
grep camss_mclk1_clk /sys/kernel/debug/clk/clk_summary
```

**And when `rmmod` does work, a following `insmod` can leave the module loaded
but not bound**, at which point every address on the bus stops answering and it
reads as if the whole bus died. Check `/sys/bus/i2c/drivers/camdiag/4-0056`.

Use unbind/bind and sysfs parameters instead of reloading; that is what
`camprobe.sh` does, and why.

**And nothing may hardcode a bus number or an i2c address.** The i2c bus numbers
are assigned in probe order and move across reboots -- the sensor was `4-0056`
and came back as `1-0056` -- so a script with the bus baked in talks to nothing
and reports NAK everywhere, which reads exactly like a dead sensor. Worse, the
camera PMIC is at 0x35 and so is the AW88261 earpiece amplifier: picking the
PMIC by address instead of by driver dumped the audio amplifier's registers and
presented them as the camera PMIC's. Find devices by driver:

	ls -d /sys/bus/i2c/drivers/fan53870/*-*
	ls -d /sys/bus/i2c/drivers/camdiag/*-*

## What it has established so far

With the rig, each input can be varied and the sensor's response watched, which
is stronger than reading a register that says "enabled":

- **0x56 is the sensor.** It stops answering when the camera rails go, and
  separately when `gpio35` is driven low. Only the sensor has a reset line in
  the vendor device tree; the EEPROM, actuator and OIS do not, so the reset
  gating is what identifies it.
- **The reset GPIO works and its polarity is right.** `gpio35` reads
  `out high` released and `out low` asserted, and the sensor's ACK follows it.
- **The CCI read path is byte-exact.** The EEPROM at 0x50 reads ASCII "SUN" and
  register 0x0d returns 0x5355, which is exactly what the module blob says to
  expect for a Sunny module. So bytes coming off the sensor are what the sensor
  put on the bus.
- **The sensor's register file is not a register file.** Reading 0x0000 to
  0x000e in two-byte steps across six different power-up sequences:

		A  8b01 becb 8b01 a55b 67ae becb 8b01 becb
		B  8b21 be8b 8b21 a5db 67ae be8b 8b21 be8b
		C  8b05 be89 8b05 a553 67ae be89 8b05 be89
		D  8b21 be9d 8b21 a55b 67ae be9d 8b21 be9d
		E  8b25 bedd 8b25 a553 67af bedd 8b25 bedd
		F  8b05 be8f 8b05 a553 67ae be8f 8b05 be8f

  The high bytes are the same in every row and every power cycle
  (8b be 8b a5 67 be 8b be), the low bytes change per power-up and are stable
  within one, and the addresses alias: 0x00 = 0x04 = 0x0c and 0x02 = 0x0a = 0x0e.
  A part with a real register map does not do that, and neither does noise.

- Read length is limited: `r2` works, `r8` and `r32` fail on *every* device
  including the EEPROM. That is the CCI, not the sensor.

## The four addresses on CCI master 1

From the vendor overlay, by which rails each one needs:

| addr | rails it needs | reads |
| --- | --- | --- |
| 0x50 | `cam_vio` only | ASCII calibration, "SUN" | 
| 0x52 | `cam_vio` only | 0x73f0, stable |
| 0x56 | vio + vana + vdig, and gpio35 released | the pattern above |
| 0x72 | | 0x0000, stable |

Note that the actuator and the OIS also need `cam_vaf` (LDO3, which happens to
be on already) and `cam_v_custom1` (`cam_ois_ldo`, TLMM 86, which is not), so
cutting the camera rails does not tell those apart from the sensor. Only the
reset line does.
