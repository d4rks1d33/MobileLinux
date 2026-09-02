# Modem support

This is what makes the radio work on motorola-rhodep. Install with:

	sudo apt install libqrtr-dev libzstd-dev
	sudo ./install.sh

Before this, the modem booted, registered 48 QMI services, answered DMS/NAS/UIM
and reported its IMEI, but sat in DMS operating mode `offline` and refused
**every** operating mode transition (`online` -> DeviceNotReady, `low-power` ->
InvalidTransition). After it, the phone registers on LTE and mobile data works.

## What was actually wrong: the modem's remote file system

`modem.mbn` is not the whole modem. At run time the modem fetches files from the
application processor over QMI service 4096 (TFTP), which `tqftpserv` answers.
On Android those requests resolve against the vendor image, the `fsg` partition
and `/mnt/vendor/persist`. On this port none of that existed, so every request
was answered "file not found" — silently, because nothing was logging them.

Running `tqftpserv -d` for one boot showed it immediately:

	12  /readonly/firmware/image/modem_pr/so/205_0_0.mbn   <- twelve retries
	 2  /readonly/vendor/fsg/mcfg_sw/mbn_sw.dig            <- "invalid path"
	 4  /readwrite/datablock/id_00
	 2  /readwrite/shob.bin
	 2  /readwrite/dhob.bin
	 1  /readwrite/mot_rfs/imei_sv

`205_0_0.mbn` is a **Motorola-signed Hexagon ELF** (`e_machine` 0xa4, 764 KiB)
that the modem loads into itself at run time, and it exists on the device, in
the `modem` partition under `image/modem_pr/so/`. Copying `modem_pr` into
`/lib/firmware/qcom/sm6375/motorola/rhodep/` and rebooting was enough to take
the modem from `offline` to accepting `--dms-set-operating-mode=online`, and it
then camped on a cell.

That is what `rhodep-rfs-populate` does, for all three sources:

| partition | what it provides | goes to |
| --- | --- | --- |
| `modem_a` | `image/modem_pr/so/*.mbn`, run-time Hexagon objects | `/lib/firmware/qcom/sm6375/motorola/rhodep/modem_pr/` |
| `fsg_a` | carrier (MCFG) configuration set + `mcfg_sw/mbn_sw.dig` | `.../rhodep/fsg/` |
| `persist` | `rfs/msm/mpss/`: RF calibration (`cal_rfs`), Motorola datablock / SIM-lock records, `shob.bin`, `dhob.bin`, `mot_rfs/imei_sv` | `/var/lib/tqftpserv/` |

The persist tree is **copied, not bind mounted**: the modem writes into it
(`mcfg.tmp`, `ticf.bin`, `*_report.txt`) and the stock partition is left alone.

It runs once, at first boot, and is a no-op afterwards.

### A free modem-side log

Two of the files the modem writes into `/var/lib/tqftpserv` are its own
diagnostics, in plain text with source file and line number:

	datablock_report.txt   mot_datablock.c, 148: Datablock path to validate:
	                       /readwrite/datablock/id_00
	simlock_report.txt     mot_simlock_mirror.c, 292: HMAC missmatch
	hob_report.txt         mot_s_hob_stg.c, 229: HOB not yet created at the factory

This is the closest thing to a modem console this port has, and it costs
nothing. Note the files copied from `persist` already contain years of history
written under Android — check the mtime before believing an entry is yours.

## tqftpserv, patched

Upstream `translate.c` maps `/readonly/firmware/image/`,
`/readonly/firmware/modem_pr`, `/readonly/vendor/firmware/`,
`/readonly/vendor/firmware_mnt/image/` and `/readwrite/`, and **rejects
everything else before even looking at the filesystem**. This modem asks for
`/readonly/vendor/fsg/mcfg_{sw,hw}/mbn_{sw,hw}.dig`, which on Android is a
symlink into the `fsg` partition (AOSP target package
`rfs_msm_mpss_readonly_vendor_fsg_symlink`, referenced from
`sm6375-common/common.mk`).

`tqftpserv/` holds upstream (BSD-3-Clause, commit b6bb92d) plus
`0001-translate-serve-readonly-vendor-fsg.patch`, three lines that add a
catch-all for the rest of `/readonly/vendor/`, resolved under the remoteproc
firmware directory. It is built as `/usr/local/bin/tqftpserv-rhodep` and
selected with a systemd drop-in, so `/usr/bin/tqftpserv` still belongs to the
held distro package.

`-d` is left on deliberately. It is a handful of lines per boot and it is the
only visibility there is into what the modem wants.

## rmtfs must NOT have -r

In rmtfs, `-r` means **read-only** (`rmtfs.c:526 read_only = true`), and
read-only means every write the modem makes goes into a per-open RAM shadow and
is thrown away at reboot. The packaged drop-in shipped `-r` with a comment
claiming it meant read/write; `install.sh` overrides it with `-P -s`.

This matters because the modem keeps the UIM provisioning session in NV. With
`-r`, a SIM the modem has not seen before never gets one.

**Back up `modemst1`, `modemst2` and `fsc` before turning this on.** There is a
copy under `_common/backups-historicos/modem-efs-*/`.

## rhodep-uim-provision

Even with a read/write EFS, this modem does not open a provisioning session by
itself. Without one the UICC application never leaves `detected`:

	Provisioning applications:
		Primary GW:   session doesn't exist
	Application [1]:
		Application state: 'detected'
		Personalization state: 'unknown'

and ModemManager gives up with "Couldn't check unlock status" and reports
`failed: sim-missing` — which is what the earlier sessions of this port were
chasing as a SIM problem. One QMI call fixes it:

	qmicli -d qrtr://0 --uim-change-provisioning-session=\
	    "activate=yes,session-type=primary-gw-provisioning,slot=1,aid=<AID>"

	-> Application state: 'ready'
	   Personalization state: 'ready'

This is not a workaround, it is what the vendor stack does: on Android qcrild
issues `QMI_UIM_CHANGE_PROVISIONING_SESSION` during UIM bring-up. ModemManager
1.24 does not, because most QMI modems auto-provision the first card.

The service runs before ModemManager so MM's single probe sees a ready card. It
is idempotent: once the EFS is writable the session survives reboots and the
service just logs "already exists". It is still needed for any new SIM.

## memshare

The modem asks the application processor for 5 MB over QMI service 52 during
bring-up. `memshare-daemon.c` answers it, and `kali-boot-v93`+ reserves the
region at 0x8ab00000. This was investigated at length and is **not** what was
blocking the radio (see HANDOFF-SESSION4.md sessions 13b/13c), but answering it
is correct and it is kept.

Do not advertise a size on an image whose device tree lacks the region: the
daemon would hand out memory Linux is still using.

## Also required, outside this directory

`kernel/patches/0042-net-ipa-sm6375-fix-imem-address.patch`. The IPA data for
SM6375 inherited qcm2290's IMEM address (0x146a8000); on this SoC the modem's
route and filter tables are at **0x0c123000**. Nothing notices until the modem
actually registers, at which point the IPA reads them through its own SMMU
context bank, faults, and the watchdog takes the SoC down:

	arm-smmu c600000.iommu: Unhandled context fault: fsr=0x402,
		iova=0x0c123080, cbfrsynra=0x4a0
	androidboot.bootreason=watchdog

`ipa.ko` is a module, so this one does not need a reflash: rebuild, copy the
module, `depmod -a 7.2.0-rc5`, reboot.

## READ THIS FIRST: the IPA is held out of the boot on purpose

`install.sh` drops `/etc/modprobe.d/rhodep-ipa-hold.conf`, which keeps `ipa.ko`
from loading. **That single file is the only thing between this port and
working mobile data**, and it is there because of a bug that has nothing to do
with the modem work above:

> With `ipa.ko` loaded **and** the modem registered on a network, the SoC
> watchdog-resets every 3 to 10 minutes, silently. Either one alone is stable.

While the file is in place:

- no mobile data (`rmnet_ipa0` / `qmapmux0.0` never appear)
- **no ModemManager**, therefore no dialer and no SMS UI. MM refuses a QMI
  modem that has no net port:
  `couldn't create modem for device 'qcom-soc': Failed to find a net port`
- the QMI layer still works: `qmicli -d qrtr://0 --dms-get-ids`, etc.

Delete the file, reboot, and everything documented below comes back: LTE
registration, ~24 Mbit/s of data, calls and SMS. Do that if you are working on
the bug, and read the comments in the file first — it describes the debug loop
that keeps the causal window a few seconds wide instead of a few minutes.

The bisection and the leads are in HANDOFF-SESSION4.md session 14, "OPEN BUG".
The best one is that patch 0025 gives SM6375 `interconnect_count = 0`, which
was verified for probe and never for operation, while the vendor IPA node asks
for `ipa_to_ebi1`, `ipa_to_imem` and `appss_to_ipa`.

## Verifying

(with `rhodep-ipa-hold.conf` removed and after a reboot)

	mmcli -m any | grep -E "state:|registration|operator|access tech"
	  state: registered   registration: home
	  operator name: PERSONAL   access tech: lte

	nmcli con up personal          # gsm connection, apn datos.personal.com
	curl --interface qmapmux0.0 https://ifconfig.me

If it is not registered, in order: `journalctl -u rhodep-rfs-populate -b`,
`journalctl -u tqftpserv -b | grep -E "reject|invalid"` (there should be nothing
but `datablock/id_01`, which is the second SIM slot, and `ota_firewall/ruleset`,
which the modem creates), then `journalctl -u rhodep-uim-provision -b`.

## Voice and SMS

Calls connect and SMS works. Verified on the network, not inferred:

	mmcli -m any --voice-create-call=number=111
	  call state changed: unknown -> dialing -> active -> terminated
	mmcli -m any --3gpp-ussd-initiate="*111#"
	  -> "USSD terminated by network"          (a real answer)

A call to `*2447` was answered by Personal and the SMS it replies with arrived.

**There is no audio during a call, and that is not a modem problem.** On
Qualcomm the voice audio never reaches the application processor: it goes
modem <-> ADSP <-> codec, and the AP only sets up the MVM/CVS/CVP sessions over
APR. Mainline has q6afe/q6asm/q6adm/q6routing and no q6voice, which shows up in
this port's boot log as APR services 3, 4, 7, 8 and nothing else — the voice
services are 0x09/0x0A/0x0B (`techpack/audio/include/ipc/apr.h`), driven by
`techpack/audio/dsp/q6voice.c`, 10565 lines. See HANDOFF-SESSION4.md session 14
for what implementing it would take.
