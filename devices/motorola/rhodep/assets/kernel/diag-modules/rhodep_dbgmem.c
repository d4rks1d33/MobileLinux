// SPDX-License-Identifier: GPL-2.0
/*
 * Expose the three Motorola debug carveouts so they can be read after a reset.
 *
 * blair reserves three regions below ramoops, and patch 0067 reserves the same
 * three in this port, so their contents survive a reset with Linux never
 * touching them:
 *
 *   mmi_annotate@aefa1800    2 KiB   Motorola's boot annotation ring
 *   tzlog_dump@aefa2000    192 KiB   backup of TrustZone's log
 *   wdog_cpuctx@aefd2000   184 KiB   per-CPU context saved on a watchdog bite
 *
 * The addresses come from the vendor's include/dt-bindings/moto/
 * moto-mem-reserve.h, where they are expressed relative to RAMOOPS_BASE_ADDR
 * 0xaf000000, and they check out: WDOG_CPUCTX_BASE is 0xaf000000 minus
 * 0x5c00 * 8, which is 0xaefd2000, exactly what this port already reserves.
 *
 * They cannot be read through /dev/mem because CONFIG_STRICT_DEVMEM is set,
 * and a read of that address returns zero bytes with no error at all. Hence
 * this: memremap() the reserved region and hand it out through debugfs.
 *
 * Why bother: the PMIC says these resets are PS_HOLD, meaning the SoC itself
 * asked to be shut down, and the AP is demonstrably alive and silent when it
 * happens. That points at the secure world, and the secure world's log is the
 * one place that might say why. Whether the backup region is actually
 * populated -- by the bootloader from the previous boot, by a downstream
 * kernel that mainline does not have, or not at all -- is exactly what this
 * module is for finding out.
 *
 * Read-only on purpose. Writing to a region the bootloader or TZ owns is not
 * an experiment, it is a way to lose the phone.
 *
 *   make
 *   sudo insmod rhodep_dbgmem.ko
 *   sudo hexdump -C /sys/kernel/debug/rhodep_dbgmem/tzlog | head -40
 *   sudo strings /sys/kernel/debug/rhodep_dbgmem/tzlog | head
 */

#include <linux/debugfs.h>
#include <linux/seq_file.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/slab.h>

struct region {
	const char *name;
	phys_addr_t base;
	size_t size;
	void *va;
	struct debugfs_blob_wrapper blob;
};

static struct region regions[] = {
	{ .name = "mmi_annotate", .base = 0xaefa1800, .size = 0x800 },
	{ .name = "tzlog",        .base = 0xaefa2000, .size = 0x30000 },
	{ .name = "wdog_cpuctx",  .base = 0xaefd2000, .size = 0x2e000 },
};

static struct dentry *dir;

/*
 * The TCSR download-mode register, which patch 0082 taught qcom_scm about.
 * Reading it is the only way to tell whether writing "full" to
 * /sys/module/qcom_scm/parameters/download_mode actually reached the hardware:
 * the write goes through an SCM io-rmw and reports success either way, and
 * /dev/mem will not read it because CONFIG_STRICT_DEVMEM is set.
 */
#define TCSR_BASE	0x003c0000
#define TCSR_DLOAD_OFF	0x13000
static void __iomem *tcsr;

/*
 * READING THAT REGISTER RESETS THE PHONE. Off by default for that reason.
 *
 * The application processor is not allowed to touch it directly. A plain
 * readl() of 0x3d3000 takes the SoC down instantly and in complete silence:
 * no kernel message, an empty pstore, bootreason=watchdog and the PMIC
 * recording PS_HOLD -- which is, byte for byte, the signature of the reset
 * this whole directory is about.
 *
 * qcom_scm reaches the same register through an SCM io-rmw, which runs in the
 * secure world and is permitted; writing "full" to
 * /sys/module/qcom_scm/parameters/download_mode does not reset anything. That
 * is exactly why patch 0082 hands the address to qcom_scm rather than poking
 * it here.
 *
 * Kept, because a one-command way to produce that signature on demand is worth
 * having: it is the control for "does a protection violation look like this".
 *
 *   sudo insmod rhodep_dbgmem.ko allow_tcsr_read=1
 *   sudo cat /sys/kernel/debug/rhodep_dbgmem/tcsr_dload   # goodbye
 */
/*
 * TrustZone's log, which lives in IMEM rather than in DDR.
 *
 * blair.dtsi has  tz-log@c125720  reg = <0xc125720 0x3000>  compatible
 * "qcom,tz-log", and downstream reads it with an ordinary driver, so the
 * application processor is allowed here -- unlike the TCSR register above.
 *
 * Mainline's sm6375.dtsi describes IMEM as sram@c125000 with a size of only
 * 0x1000, and the log starts at +0x720 and runs 0x3000, so it extends two
 * pages past the end of that window. An earlier attempt to read it through the
 * 4 KiB mapping hung the phone, which is recorded in HANDOFF.md as "reading
 * past the end of the 4 KB IMEM window". That was a read beyond the mapping,
 * not a forbidden region: mapping the whole 0x3000 is a different thing.
 *
 * This is the one place that might say what TZ saw before it deasserted
 * PS_HOLD. It cannot be read after the fact -- the reset is a power cycle and
 * IMEM is on-chip SRAM on the same rails -- so it has to be polled while the
 * machine is still running.
 */
/*
 * The vendor says reg = <0xc125720 0x3000>, but only the part inside the 4 KiB
 * IMEM page is readable from here. Mainline describes IMEM as sram@c125000
 * with size 0x1000, and that is not an oversight: reading the full 0x3000
 * froze the phone hard enough to need the power button, while the first 256
 * bytes read back fine. So stop at the end of the page.
 *
 *   0xc125720 .. 0xc126000  =  0x8e0 bytes, readable
 *   beyond that             =  nothing answers, and the AP hangs
 */
#define TZLOG_IMEM_BASE	0x0c125720
#define TZLOG_IMEM_SIZE	0x8e0
static void __iomem *tzlog_imem;

#define TZLOG_BODY_BASE	0x0c116000
#define TZLOG_BODY_SIZE	0x4000
static void __iomem *tzlog_body;

static ssize_t io_read(void __iomem *base, size_t total, char __user *buf,
		       size_t len, loff_t *ppos)
{
	void *tmp;
	ssize_t ret;

	if (*ppos >= total)
		return 0;
	len = min_t(size_t, len, total - *ppos);
	tmp = kmalloc(len, GFP_KERNEL);
	if (!tmp)
		return -ENOMEM;
	memcpy_fromio(tmp, base + *ppos, len);
	ret = copy_to_user(buf, tmp, len) ? -EFAULT : (ssize_t)len;
	kfree(tmp);
	if (ret > 0)
		*ppos += ret;
	return ret;
}

static ssize_t tzlog_body_read(struct file *f, char __user *buf, size_t len,
			       loff_t *ppos)
{
	if (!tzlog_body)
		return -ENODEV;
	return io_read(tzlog_body, TZLOG_BODY_SIZE, buf, len, ppos);
}

static const struct file_operations tzlog_body_fops = {
	.owner	= THIS_MODULE,
	.read	= tzlog_body_read,
	.llseek	= default_llseek,
};

static ssize_t tzlog_imem_read(struct file *f, char __user *buf, size_t len,
			       loff_t *ppos)
{
	if (!tzlog_imem)
		return -ENODEV;
	/* memcpy_fromio, because this is IMEM and not ordinary memory. */
	{
		void *tmp;
		ssize_t ret;

		if (*ppos >= TZLOG_IMEM_SIZE)
			return 0;
		len = min_t(size_t, len, TZLOG_IMEM_SIZE - *ppos);
		tmp = kmalloc(len, GFP_KERNEL);
		if (!tmp)
			return -ENOMEM;
		memcpy_fromio(tmp, tzlog_imem + *ppos, len);
		ret = copy_to_user(buf, tmp, len) ? -EFAULT : (ssize_t)len;
		kfree(tmp);
		if (ret > 0)
			*ppos += ret;
		return ret;
	}
}

static const struct file_operations tzlog_imem_fops = {
	.owner	= THIS_MODULE,
	.read	= tzlog_imem_read,
	.llseek	= default_llseek,
};

/*
 * The first word of the TZ log header is a pointer, 0x0c116000 on this phone,
 * and the readable window at 0xc125720 holds only a header, some subsystem
 * names and a block of flags -- no log text. The text should be at that
 * pointer. It is in OCIMEM but outside the 4 KiB IMEM node mainline
 * describes, so it may not answer, and an address that does not answer hangs
 * this SoC. Hence a parameter, and a deliberately tiny read.
 */
static bool allow_tzlog_body;
module_param(allow_tzlog_body, bool, 0400);
MODULE_PARM_DESC(allow_tzlog_body,
		 "map the TZ log body at 0x0c116000. May hang the phone.");

static bool allow_tcsr_read;
module_param(allow_tcsr_read, bool, 0400);
MODULE_PARM_DESC(allow_tcsr_read,
		 "expose the TCSR download-mode register. Reading it resets the phone.");

static int tcsr_dload_show(struct seq_file *m, void *unused)
{
	if (!tcsr)
		return -ENODEV;
	seq_printf(m, "%#010x\n", readl_relaxed(tcsr + TCSR_DLOAD_OFF));
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(tcsr_dload);

static void rhodep_dbgmem_release(void)
{
	int i;

	debugfs_remove_recursive(dir);
	dir = NULL;

	if (tcsr) {
		iounmap(tcsr);
		tcsr = NULL;
	}

	if (tzlog_imem) {
		iounmap(tzlog_imem);
		tzlog_imem = NULL;
	}

	if (tzlog_body) {
		iounmap(tzlog_body);
		tzlog_body = NULL;
	}

	for (i = 0; i < ARRAY_SIZE(regions); i++) {
		if (regions[i].va) {
			memunmap(regions[i].va);
			regions[i].va = NULL;
		}
	}
}

static int __init rhodep_dbgmem_init(void)
{
	int i, mapped = 0;

	dir = debugfs_create_dir("rhodep_dbgmem", NULL);
	if (IS_ERR(dir))
		return PTR_ERR(dir);

	for (i = 0; i < ARRAY_SIZE(regions); i++) {
		struct region *r = &regions[i];

		/*
		 * MEMREMAP_WB: these are ordinary DDR that the kernel was told
		 * to leave alone, not device registers. If a region turns out
		 * to be owned by TZ the mapping will still succeed and the
		 * read is what would fail, so map each one separately and keep
		 * going rather than giving up on the first failure.
		 */
		r->va = memremap(r->base, r->size, MEMREMAP_WB);
		if (!r->va) {
			pr_warn("rhodep_dbgmem: could not map %s at %pa\n",
				r->name, &r->base);
			continue;
		}

		r->blob.data = r->va;
		r->blob.size = r->size;
		debugfs_create_blob(r->name, 0400, dir, &r->blob);
		pr_info("rhodep_dbgmem: %s at %pa, %zu bytes\n",
			r->name, &r->base, r->size);
		mapped++;
	}

	/* Registers, so ioremap rather than memremap. Only when asked for:
	 * see the warning above.
	 */
	tcsr = allow_tcsr_read ? ioremap(TCSR_BASE, 0x40000) : NULL;
	if (allow_tzlog_body) {
		tzlog_body = ioremap(TZLOG_BODY_BASE, TZLOG_BODY_SIZE);
		if (tzlog_body) {
			/*
			 * Read a little at load time and print it, so that even
			 * if reading it from userspace later hangs the machine,
			 * something was captured.
			 */
			u32 probe[4];

			memcpy_fromio(probe, tzlog_body, sizeof(probe));
			pr_warn("rhodep_dbgmem: tzlog body %#x: %08x %08x %08x %08x%s\n",
				TZLOG_BODY_BASE, probe[0], probe[1], probe[2],
				probe[3],
				probe[0] == 0x747a6461 ? "  (magic 'tzda')" : "");
			debugfs_create_file("tzlog_body", 0400, dir, NULL,
					    &tzlog_body_fops);
			mapped++;
		}
	}

	tzlog_imem = ioremap(TZLOG_IMEM_BASE, TZLOG_IMEM_SIZE);
	if (tzlog_imem) {
		debugfs_create_file("tzlog_imem", 0400, dir, NULL,
				    &tzlog_imem_fops);
		pr_info("rhodep_dbgmem: TrustZone log at %#x, %#x bytes\n",
			TZLOG_IMEM_BASE, TZLOG_IMEM_SIZE);
		mapped++;
	} else {
		pr_warn("rhodep_dbgmem: could not ioremap the TrustZone log\n");
	}

	if (tcsr) {
		debugfs_create_file("tcsr_dload", 0400, dir, NULL,
				    &tcsr_dload_fops);
		pr_warn("rhodep_dbgmem: tcsr download-mode register at %#x is "
			"exposed; READING IT WILL RESET THE PHONE\n",
			TCSR_BASE + TCSR_DLOAD_OFF);
		mapped++;
	} else if (allow_tcsr_read) {
		pr_warn("rhodep_dbgmem: could not ioremap TCSR\n");
	}

	if (!mapped) {
		rhodep_dbgmem_release();
		return -ENODEV;
	}

	pr_info("rhodep_dbgmem: %d of %d regions mapped read-only under "
		"/sys/kernel/debug/rhodep_dbgmem\n",
		mapped, (int)ARRAY_SIZE(regions));
	return 0;
}

static void __exit rhodep_dbgmem_exit(void)
{
	rhodep_dbgmem_release();
}

module_init(rhodep_dbgmem_init);
module_exit(rhodep_dbgmem_exit);

MODULE_DESCRIPTION("Read the Motorola debug carveouts that survive a reset");
MODULE_LICENSE("GPL");
