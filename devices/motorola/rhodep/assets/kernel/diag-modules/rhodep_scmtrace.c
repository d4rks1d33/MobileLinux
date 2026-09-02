// SPDX-License-Identifier: GPL-2.0
/*
 * Record every secure monitor call, to find out who asks for the shutdown.
 *
 * Three participants have now been measured through an LTE reset and none of
 * them reports a fault: Linux prints nothing and keeps answering, the modem
 * answers AT commands and is healthy, and TrustZone's own interrupt counters
 * show no VMIDMT error, no watchdog bite, nothing. Meanwhile the PMIC records
 * PS_HOLD, which is the SoC asking to be powered down -- and which is also how
 * an ordinary shutdown is performed.
 *
 * So the question is no longer what fails but what asks. A secure monitor call
 * requesting a reset would look exactly like this from outside, and would leave
 * TZ's fault accounting untouched because nothing faulted.
 *
 * Every SMC in mainline goes through __scm_smc_call(), so a kprobe there sees
 * all of them. CONFIG_KPROBES is set on this kernel and the symbol is in
 * kallsyms, so this needs no flashing.
 *
 * The reset is a power cycle, so nothing in RAM survives it. The calls are kept
 * in a small ring that userspace polls and flushes to flash; the last entries
 * before the machine stops are the ones that matter.
 *
 *   make && sudo insmod rhodep_scmtrace.ko
 *   sudo cat /sys/kernel/debug/rhodep_scmtrace/calls
 *   sudo rhodep-scmtrace-watch.py --log /var/log/scm.log &
 */

#include <linux/debugfs.h>
#include <linux/kprobes.h>
#include <linux/module.h>
#include <linux/seq_file.h>
#include <linux/spinlock.h>

/*
 * Mirrors struct qcom_scm_desc from drivers/firmware/qcom/qcom_scm.h, which is
 * private to that directory. If it ever changes shape this reads rubbish, so
 * the svc/cmd values are worth sanity-checking against known ones.
 */
#define MAX_SCM_ARGS 10
struct scm_desc_mirror {
	u32 svc;
	u32 cmd;
	u32 arginfo;
	u64 args[MAX_SCM_ARGS];
	u32 owner;
};

#define RING_SIZE 256
struct entry {
	u64 ns;
	u32 svc;
	u32 cmd;
	u32 owner;
	u64 arg0;
	u64 arg1;
	int cpu;
	char comm[TASK_COMM_LEN];
};

static struct entry ring[RING_SIZE];
static unsigned int head;
static u64 total;
static DEFINE_SPINLOCK(ring_lock);

static int scm_entry(struct kprobe *p, struct pt_regs *regs)
{
	/* arm64: x0 is dev, x1 is the descriptor. */
	struct scm_desc_mirror *desc = (struct scm_desc_mirror *)regs->regs[1];
	unsigned long flags;
	struct entry *e;

	if (!desc)
		return 0;

	spin_lock_irqsave(&ring_lock, flags);
	e = &ring[head % RING_SIZE];
	e->ns = ktime_get_ns();
	e->svc = desc->svc;
	e->cmd = desc->cmd;
	e->owner = desc->owner;
	e->arg0 = desc->args[0];
	e->arg1 = desc->args[1];
	e->cpu = raw_smp_processor_id();
	memcpy(e->comm, current->comm, TASK_COMM_LEN);
	head++;
	total++;
	spin_unlock_irqrestore(&ring_lock, flags);
	return 0;
}

static struct kprobe kp = {
	.symbol_name = "__scm_smc_call",
	.pre_handler = scm_entry,
};

static int calls_show(struct seq_file *m, void *unused)
{
	unsigned long flags;
	unsigned int i, start, count;
	static struct entry copy[RING_SIZE];
	u64 seen;

	spin_lock_irqsave(&ring_lock, flags);
	seen = total;
	count = head < RING_SIZE ? head : RING_SIZE;
	start = head < RING_SIZE ? 0 : head % RING_SIZE;
	memcpy(copy, ring, sizeof(copy));
	spin_unlock_irqrestore(&ring_lock, flags);

	seq_printf(m, "# %llu calls seen, showing the last %u\n", seen, count);
	seq_puts(m, "# ns svc cmd owner arg0 arg1 cpu comm\n");
	for (i = 0; i < count; i++) {
		struct entry *e = &copy[(start + i) % RING_SIZE];

		seq_printf(m, "%llu %#x %#x %#x %#llx %#llx %d %s\n",
			   e->ns, e->svc, e->cmd, e->owner, e->arg0, e->arg1,
			   e->cpu, e->comm);
	}
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(calls);

static struct dentry *dir;

static int __init rhodep_scmtrace_init(void)
{
	int ret;

	ret = register_kprobe(&kp);
	if (ret) {
		pr_err("rhodep_scmtrace: could not probe __scm_smc_call: %d\n",
		       ret);
		return ret;
	}

	dir = debugfs_create_dir("rhodep_scmtrace", NULL);
	if (IS_ERR(dir)) {
		unregister_kprobe(&kp);
		return PTR_ERR(dir);
	}
	debugfs_create_file("calls", 0400, dir, NULL, &calls_fops);

	pr_info("rhodep_scmtrace: watching __scm_smc_call at %p\n", kp.addr);
	return 0;
}

static void __exit rhodep_scmtrace_exit(void)
{
	unregister_kprobe(&kp);
	debugfs_remove_recursive(dir);
}

module_init(rhodep_scmtrace_init);
module_exit(rhodep_scmtrace_exit);

MODULE_DESCRIPTION("Record secure monitor calls, to find who asks for a reset");
MODULE_LICENSE("GPL");
