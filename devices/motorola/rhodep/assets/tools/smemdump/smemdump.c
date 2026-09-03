// SPDX-License-Identifier: GPL-2.0
//
// Dump a Qualcomm SMEM item to the kernel log.
//
// Used to read a remoteproc crash reason: a Qualcomm subsystem writes its
// failure reason (SFR) into SMEM before dying, and SMEM lives in a nomap
// reserved region in DDR, so it survives a warm reset and can be read on the
// boot that follows the crash.
//
//   item 421 = modem (mpss), 423 = adsp, 424 = cdsp
//
// Those are the crash_reason_smem ids from drivers/remoteproc/qcom_q6v5_pas.c;
// sm6375 uses sm6350_adsp_resource, whose crash_reason_smem is 423.
//
// /dev/mem cannot be used for this: CONFIG_STRICT_DEVMEM is enabled and a read
// of the SMEM region returns 0 bytes. Hence this module.
//
// Build it ON THE DEVICE (linux-headers-7.2.0-rc5 is installed, and gcc/make
// are there), so there is no cross build and nothing has to be reflashed:
//
//	mkdir -p /root/smemdump && cd /root/smemdump
//	cp smemdump.c .
//	printf 'obj-m += smemdump.o\n' > Makefile
//	make -C /lib/modules/$(uname -r)/build M=$PWD modules
//	insmod ./smemdump.ko item=423 ; rmmod smemdump ; dmesg | grep smemdump
//
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/soc/qcom/smem.h>

static int item = 423;
module_param(item, int, 0444);
MODULE_PARM_DESC(item, "SMEM item id to dump (423=adsp, 421=modem, 424=cdsp)");

static int __init smemdump_init(void)
{
	size_t size = 0;
	void *p;
	int i, n;

	p = qcom_smem_get(QCOM_SMEM_HOST_ANY, item, &size);
	if (IS_ERR(p)) {
		pr_err("smemdump: item %d: error %ld\n", item, PTR_ERR(p));
		return 0;
	}
	pr_err("smemdump: item %d: %zu bytes\n", item, size);

	/* Print it as a string if it looks like one: a subsystem failure reason
	 * is usually a fault description with a file name and a line number.
	 */
	n = size > 512 ? 512 : size;
	for (i = 0; i < n; i++)
		if (((char *)p)[i] == 0)
			break;
	if (i > 3)
		pr_err("smemdump: item %d text: \"%.*s\"\n", item, i, (char *)p);
	else
		pr_err("smemdump: item %d has no leading string\n", item);

	print_hex_dump(KERN_ERR, "smemdump: ", DUMP_PREFIX_OFFSET, 16, 1, p, n, true);
	return 0;
}

static void __exit smemdump_exit(void) { }

module_init(smemdump_init);
module_exit(smemdump_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Dump a Qualcomm SMEM item to the kernel log");
