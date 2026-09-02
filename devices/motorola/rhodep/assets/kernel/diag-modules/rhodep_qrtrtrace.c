// SPDX-License-Identifier: GPL-2.0
/*
 * Record every QRTR message the modem sends, to find out what it asks for
 * before the machine is switched off.
 *
 * What is known so far about the LTE reset: it needs ipa.ko loaded, it needs
 * LTE rather than GSM, the PMIC records PS_HOLD -- the SoC asking to be powered
 * down -- and none of the four participants reports a fault. Linux prints
 * nothing, makes no secure monitor call and keeps answering. TrustZone's
 * interrupt counters do not move. The modem answers AT commands, and its own
 * DIAG log shows routine periodic measurement logging at an even cadence right
 * up to the last instant, with no log code in the final second that was not
 * already there in the first.
 *
 * So nothing fails, and something asks. The remaining way for the modem to ask
 * the AP for anything is QMI over QRTR, and there is a specific reason to look
 * there: mainline's IPA implements a much smaller QMI surface than the
 * downstream driver. A request the AP never answers, followed by the modem
 * giving up, would look exactly like this from outside -- graceful, unlogged,
 * and fatal.
 *
 * Everything arriving from a remote node goes through qrtr_endpoint_post(), so
 * one kprobe sees all of it. The header is decoded here rather than in
 * userspace because the buffer is not ours to keep.
 *
 * The reset is a power cycle and RAM does not survive it, so entries carry a
 * sequence number and userspace drains them to flash continuously; a gap in the
 * sequence means the poll was too slow, which is worth knowing rather than
 * silently missing.
 *
 *   make && sudo insmod rhodep_qrtrtrace.ko
 *   sudo rhodep-qrtrtrace-watch.py --log /var/log/qrtr.log &
 */

#include <linux/debugfs.h>
#include <linux/kprobes.h>
#include <linux/module.h>
#include <linux/seq_file.h>
#include <linux/spinlock.h>

#define QRTR_TYPE_DATA 1

/* Mirrors struct qrtr_hdr_v1/v2 in net/qrtr/af_qrtr.c, which are private. */
struct hdr_v1 {
	__le32 version;
	__le32 type;
	__le32 src_node_id;
	__le32 src_port_id;
	__le32 confirm_rx;
	__le32 size;
	__le32 dst_node_id;
	__le32 dst_port_id;
} __packed;

struct hdr_v2 {
	u8 version;
	u8 type;
	u8 flags;
	u8 optlen;
	__le32 size;
	__le16 src_node_id;
	__le16 src_port_id;
	__le16 dst_node_id;
	__le16 dst_port_id;
} __packed;

/* The QMI service header that follows, for data packets. */
struct qmi_hdr {
	u8 type;
	__le16 txn;
	__le16 msg_id;
	__le16 msg_len;
} __packed;

/*
 * Control packets carry the service map. Decoding them here means the trace
 * resolves its own port numbers to services, instead of needing a second
 * instrument that has to agree with this one.
 */
struct ctrl_pkt {
	__le32 cmd;
	__le32 service;
	__le32 instance;
	__le32 node;
	__le32 port;
} __packed;

#define RING_SIZE 2048
struct entry {
	u64 seq;
	u64 ns;
	u32 type;
	u32 src_node;
	u32 src_port;
	u32 dst_node;
	u32 dst_port;
	u32 size;
	/* Only meaningful for data packets; qmi_type is 0xff when absent. */
	u8 qmi_type;
	u16 qmi_txn;
	u16 qmi_msg_id;
	/* Only meaningful for control packets; ctrl_service is ~0 when absent. */
	u32 ctrl_service;
	u32 ctrl_instance;
	u32 ctrl_node;
	u32 ctrl_port;
};

static struct entry ring[RING_SIZE];
static unsigned int head;
static u64 total;
static DEFINE_SPINLOCK(ring_lock);

static int rx_entry(struct kprobe *p, struct pt_regs *regs)
{
	/* arm64: x0 endpoint, x1 data, x2 len. */
	const u8 *data = (const u8 *)regs->regs[1];
	size_t len = (size_t)regs->regs[2];
	unsigned int hdrlen = 0, optlen = 0;
	unsigned long flags;
	struct entry *e;
	u32 type = 0, sn = 0, sp = 0, dn = 0, dp = 0, size = 0;

	if (!data || len < 4)
		return 0;

	/*
	 * Reading through a raw kernel pointer in a kprobe handler: the caller
	 * owns a buffer of at least len bytes, so staying inside it is enough.
	 */
	if (data[0] == 1) {
		const struct hdr_v1 *h = (const struct hdr_v1 *)data;

		if (len < sizeof(*h))
			return 0;
		hdrlen = sizeof(*h);
		type = le32_to_cpu(h->type);
		sn = le32_to_cpu(h->src_node_id);
		sp = le32_to_cpu(h->src_port_id);
		dn = le32_to_cpu(h->dst_node_id);
		dp = le32_to_cpu(h->dst_port_id);
		size = le32_to_cpu(h->size);
	} else if (data[0] == 2) {
		const struct hdr_v2 *h = (const struct hdr_v2 *)data;

		if (len < sizeof(*h))
			return 0;
		hdrlen = sizeof(*h);
		optlen = h->optlen;
		type = h->type;
		sn = le16_to_cpu(h->src_node_id);
		sp = le16_to_cpu(h->src_port_id);
		dn = le16_to_cpu(h->dst_node_id);
		dp = le16_to_cpu(h->dst_port_id);
		size = le32_to_cpu(h->size);
	} else {
		return 0;
	}

	spin_lock_irqsave(&ring_lock, flags);
	e = &ring[head % RING_SIZE];
	e->seq = total;
	e->ns = ktime_get_ns();
	e->type = type;
	e->src_node = sn;
	e->src_port = sp;
	e->dst_node = dn;
	e->dst_port = dp;
	e->size = size;
	e->qmi_type = 0xff;
	e->qmi_txn = 0;
	e->qmi_msg_id = 0;
	e->ctrl_service = 0xffffffff;
	e->ctrl_instance = 0;
	e->ctrl_node = 0;
	e->ctrl_port = 0;

	if (type == QRTR_TYPE_DATA &&
	    len >= hdrlen + optlen + sizeof(struct qmi_hdr)) {
		const struct qmi_hdr *q =
			(const struct qmi_hdr *)(data + hdrlen + optlen);

		e->qmi_type = q->type;
		e->qmi_txn = le16_to_cpu(q->txn);
		e->qmi_msg_id = le16_to_cpu(q->msg_id);
	} else if (type != QRTR_TYPE_DATA &&
		   len >= hdrlen + optlen + sizeof(struct ctrl_pkt)) {
		const struct ctrl_pkt *c =
			(const struct ctrl_pkt *)(data + hdrlen + optlen);

		e->ctrl_service = le32_to_cpu(c->service);
		e->ctrl_instance = le32_to_cpu(c->instance);
		e->ctrl_node = le32_to_cpu(c->node);
		e->ctrl_port = le32_to_cpu(c->port);
	}

	head++;
	total++;
	spin_unlock_irqrestore(&ring_lock, flags);
	return 0;
}

static struct kprobe kp = {
	.symbol_name = "qrtr_endpoint_post",
	.pre_handler = rx_entry,
};

static int msgs_show(struct seq_file *m, void *unused)
{
	static struct entry copy[RING_SIZE];
	unsigned long flags;
	unsigned int i, start, count;
	u64 seen;

	spin_lock_irqsave(&ring_lock, flags);
	seen = total;
	count = head < RING_SIZE ? head : RING_SIZE;
	start = head < RING_SIZE ? 0 : head % RING_SIZE;
	memcpy(copy, ring, sizeof(copy));
	spin_unlock_irqrestore(&ring_lock, flags);

	seq_printf(m, "# %llu messages seen, showing the last %u\n", seen, count);
	seq_puts(m, "# seq ns type src_node:src_port dst_node:dst_port size qmi_type qmi_txn qmi_msg_id ctrl_service ctrl_instance ctrl_node ctrl_port\n");
	for (i = 0; i < count; i++) {
		struct entry *e = &copy[(start + i) % RING_SIZE];

		seq_printf(m, "%llu %llu %u %u:%u %u:%u %u %#x %u %#x %u %u %u %u\n",
			   e->seq, e->ns, e->type, e->src_node, e->src_port,
			   e->dst_node, e->dst_port, e->size, e->qmi_type,
			   e->qmi_txn, e->qmi_msg_id, e->ctrl_service,
			   e->ctrl_instance, e->ctrl_node, e->ctrl_port);
	}
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(msgs);

static struct dentry *dir;

static int __init rhodep_qrtrtrace_init(void)
{
	int ret;

	ret = register_kprobe(&kp);
	if (ret) {
		pr_err("rhodep_qrtrtrace: could not probe qrtr_endpoint_post: %d\n",
		       ret);
		return ret;
	}

	dir = debugfs_create_dir("rhodep_qrtrtrace", NULL);
	if (IS_ERR(dir)) {
		unregister_kprobe(&kp);
		return PTR_ERR(dir);
	}
	debugfs_create_file("msgs", 0400, dir, NULL, &msgs_fops);

	pr_info("rhodep_qrtrtrace: watching qrtr_endpoint_post at %p\n", kp.addr);
	return 0;
}

static void __exit rhodep_qrtrtrace_exit(void)
{
	unregister_kprobe(&kp);
	debugfs_remove_recursive(dir);
}

module_init(rhodep_qrtrtrace_init);
module_exit(rhodep_qrtrtrace_exit);

MODULE_DESCRIPTION("Record QRTR messages from the modem, to find what it asks for");
MODULE_LICENSE("GPL");
