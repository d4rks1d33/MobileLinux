// SPDX-License-Identifier: GPL-2.0
/*
 * Read a kernel address, to see what the running kernel actually holds.
 *
 * This exists because of a specific trap. Deciding what the IPA driver tells
 * the modem meant reading drivers/net/ipa, and the source tree nearest to hand
 * turned out to be missing two of this port's own patches -- reasoning from it
 * would have described a kernel that is not the one running. There is no
 * /proc/kcore on this build to check against, and the boot image cannot be
 * asked about the value of a C variable.
 *
 * But kallsyms names static variables, and init_modem_driver_req.req is one:
 * the actual request structure sent to the modem over QMI, retained after the
 * handshake because the driver reuses it. Reading it answers what the modem was
 * told, on the running system, with no archaeology in between.
 *
 * copy_from_kernel_nofault() rather than a plain dereference, because a wrong
 * address on this SoC does not return an error, it takes the machine down.
 *
 *   make && sudo insmod rhodep_kvpeek.ko
 *   echo 0xffffd1b3421bfee4 | sudo tee /sys/kernel/debug/rhodep_kvpeek/addr
 *   echo 256 | sudo tee /sys/kernel/debug/rhodep_kvpeek/len
 *   sudo cat /sys/kernel/debug/rhodep_kvpeek/data
 */

#include <linux/debugfs.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>

#define MAX_LEN 4096

static u64 peek_addr;
static u32 peek_len = 256;
static u64 phys_addr;
static u32 phys_len = 256;
static u8 buf[MAX_LEN];

static int data_show(struct seq_file *m, void *unused)
{
	u32 len = peek_len;
	u32 i;
	int ret;

	if (!peek_addr) {
		seq_puts(m, "# no address set\n");
		return 0;
	}
	if (len > MAX_LEN)
		len = MAX_LEN;

	ret = copy_from_kernel_nofault(buf, (void *)(uintptr_t)peek_addr, len);
	if (ret) {
		seq_printf(m, "# cannot read %#llx: %d\n", peek_addr, ret);
		return 0;
	}

	seq_printf(m, "# %u bytes at %#llx\n", len, peek_addr);
	for (i = 0; i < len; i += 16) {
		u32 j, n = min_t(u32, 16, len - i);

		seq_printf(m, "%04x ", i);
		for (j = 0; j < n; j++)
			seq_printf(m, "%02x ", buf[i + j]);
		seq_puts(m, "\n");
	}
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(data);

/*
 * The same, for a physical address. Only for memory that is known to answer --
 * the RPM message RAM is in the device tree and other drivers already map it --
 * because an address that does not respond on this SoC takes the machine down
 * instead of faulting.
 */
static int phys_show(struct seq_file *m, void *unused)
{
	u32 len = phys_len;
	void __iomem *p;
	u32 i;

	if (!phys_addr) {
		seq_puts(m, "# no physical address set\n");
		return 0;
	}
	if (len > MAX_LEN)
		len = MAX_LEN;

	p = ioremap(phys_addr, len);
	if (!p) {
		seq_printf(m, "# cannot map %#llx\n", phys_addr);
		return 0;
	}
	memcpy_fromio(buf, p, len);
	iounmap(p);

	seq_printf(m, "# %u bytes at physical %#llx\n", len, phys_addr);
	for (i = 0; i < len; i += 16) {
		u32 j, n = min_t(u32, 16, len - i);

		seq_printf(m, "%04x ", i);
		for (j = 0; j < n; j++)
			seq_printf(m, "%02x ", buf[i + j]);
		seq_puts(m, "\n");
	}
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(phys);

static struct dentry *dir;

static int __init rhodep_kvpeek_init(void)
{
	dir = debugfs_create_dir("rhodep_kvpeek", NULL);
	if (IS_ERR(dir))
		return PTR_ERR(dir);

	debugfs_create_x64("addr", 0600, dir, &peek_addr);
	debugfs_create_u32("len", 0600, dir, &peek_len);
	debugfs_create_file("data", 0400, dir, NULL, &data_fops);
	debugfs_create_x64("paddr", 0600, dir, &phys_addr);
	debugfs_create_u32("plen", 0600, dir, &phys_len);
	debugfs_create_file("phys", 0400, dir, NULL, &phys_fops);

	pr_info("rhodep_kvpeek: ready\n");
	return 0;
}

static void __exit rhodep_kvpeek_exit(void)
{
	debugfs_remove_recursive(dir);
}

module_init(rhodep_kvpeek_init);
module_exit(rhodep_kvpeek_exit);

MODULE_DESCRIPTION("Read a kernel address, to check the running kernel itself");
MODULE_LICENSE("GPL");
