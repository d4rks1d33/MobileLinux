/*
 * Answer the modem's memshare (QMI service 52) requests.
 *
 * Established by scripts/modem/memshare-probe.c, on this device, at boot:
 *
 *     packet from node=0 port=7, 21 bytes
 *     00 0100 2400 0e00 | 01 0400 01000000 | 10 0400 00000000
 *     QMI REQUEST txn=1 msg_id=0x0024 (MEM_QUERY_SIZE) client_id=1 proc=0
 *
 * The modem asks the application processor how much shared memory it can have,
 * exactly once, during bring-up, and on mainline nothing answers because there
 * is no memshare implementation at all. The stock device tree declares
 * qcom,memshare with an 8 MB region and a client marked qcom,allocate-boot-time,
 * so this is a conversation the modem is entitled to expect.
 *
 * This daemon is deliberately the *minimal* answer: it reports success with a
 * size of zero rather than handing out memory. The point is to find out whether
 * the modem is blocked on receiving an answer at all, which is cheap to test,
 * before taking on the much larger job of allocating physical memory from a
 * reserved region and handing over addresses. If the modem completes
 * initialisation after this, the remaining work is well defined. If it does
 * not, memshare is not the blocker and the next lead is somewhere else.
 *
 * Build on the phone:
 *     sudo apt install libqrtr-dev
 *     gcc -O2 -Wall -o memshare-daemon memshare-daemon.c -lqrtr
 */

#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <libqrtr.h>

#define MEMSHARE_SERVICE   52
#define MEMSHARE_VERSION   1
#define MEMSHARE_INSTANCE  0

/* Downstream memshare message ids. */
#define MEM_ALLOC_REQ          0x0020
#define MEM_FREE_REQ           0x0021
#define MEM_ALLOC_GENERIC_REQ  0x0022
#define MEM_FREE_GENERIC_REQ   0x0023
#define MEM_QUERY_SIZE_REQ     0x0024

/* QMI_REQUEST and QMI_RESPONSE come from libqrtr.h. */

#define TLV_RESULT    0x02
#define TLV_SIZE      0x10
#define TLV_SEQUENCE  0x10
#define TLV_ADDR_INFO 0x11

/*
 * The region handed to the modem. It must be reserved in the device tree, or
 * Linux will happily use it as ordinary RAM and the modem will corrupt it:
 *
 *     memshare@8ab00000 {
 *             reg = <0x00 0x8ab00000 0x00 0x800000>;
 *             no-map;
 *     };
 *
 * 8 MB in the hole between pil-gpu-ucode and pil-mpss-wlan. A fixed address is
 * used instead of stock's dynamically placed shared-dma-pool so that this can
 * stay in userspace: there is nothing to allocate, only a number to hand over.
 */
#define REGION_BASE   0x8ab00000ULL
#define REGION_SIZE   0x800000U

static volatile sig_atomic_t stop;

static void on_signal(int signo)
{
	(void)signo;
	stop = 1;
}

static const char *msg_name(uint16_t id)
{
	switch (id) {
	case MEM_ALLOC_REQ:         return "MEM_ALLOC";
	case MEM_FREE_REQ:          return "MEM_FREE";
	case MEM_ALLOC_GENERIC_REQ: return "MEM_ALLOC_GENERIC";
	case MEM_FREE_GENERIC_REQ:  return "MEM_FREE_GENERIC";
	case MEM_QUERY_SIZE_REQ:    return "MEM_QUERY_SIZE";
	default:                    return "unknown";
	}
}

/*
 * Build a QMI response carrying a result TLV, and optionally a size TLV.
 * result 0 / error 0 is success.
 */
static unsigned int build_response(unsigned char *buf, uint16_t txn,
				   uint16_t msg_id, uint16_t result,
				   uint16_t error, bool with_size,
				   uint32_t size)
{
	unsigned int payload = 7 + (with_size ? 7 : 0);
	unsigned int i = 0;

	buf[i++] = QMI_RESPONSE;
	memcpy(buf + i, &txn, 2);      i += 2;
	memcpy(buf + i, &msg_id, 2);   i += 2;
	memcpy(buf + i, &payload, 2);  i += 2;

	buf[i++] = TLV_RESULT;
	buf[i++] = 0x04;
	buf[i++] = 0x00;
	memcpy(buf + i, &result, 2);   i += 2;
	memcpy(buf + i, &error, 2);    i += 2;

	if (with_size) {
		buf[i++] = TLV_SIZE;
		buf[i++] = 0x04;
		buf[i++] = 0x00;
		memcpy(buf + i, &size, 4); i += 4;
	}

	return i;
}

/* The modem echoes a sequence id back in the response; pull it out of TLV 4. */
static uint32_t parse_sequence_id(const unsigned char *buf, int len)
{
	int i = 7;

	while (i + 3 <= len) {
		unsigned char type = buf[i];
		uint16_t tlen;

		memcpy(&tlen, buf + i + 1, 2);
		if (type == 0x04 && tlen == 4 && i + 3 + 4 <= len) {
			uint32_t seq;

			memcpy(&seq, buf + i + 3, 4);
			return seq;
		}
		i += 3 + tlen;
	}

	return 0;
}

/*
 * mem_alloc_generic_resp_msg_v01: result, the echoed sequence id, and an array
 * of address/size pairs. The array is a QMI array, so it carries a one byte
 * count before the entries, and each entry is a 64 bit physical address
 * followed by a 32 bit length.
 */
static unsigned int build_alloc_response(unsigned char *buf, uint16_t txn,
					 uint16_t msg_id, uint32_t sequence_id,
					 uint64_t phys, uint32_t size)
{
	uint16_t payload = 7 + 7 + (3 + 1 + 12);
	uint16_t zero = 0;
	unsigned int i = 0;

	buf[i++] = QMI_RESPONSE;
	memcpy(buf + i, &txn, 2);      i += 2;
	memcpy(buf + i, &msg_id, 2);   i += 2;
	memcpy(buf + i, &payload, 2);  i += 2;

	buf[i++] = TLV_RESULT;
	buf[i++] = 0x04;
	buf[i++] = 0x00;
	memcpy(buf + i, &zero, 2);     i += 2;   /* result: success */
	memcpy(buf + i, &zero, 2);     i += 2;   /* error */

	buf[i++] = TLV_SEQUENCE;
	buf[i++] = 0x04;
	buf[i++] = 0x00;
	memcpy(buf + i, &sequence_id, 4); i += 4;

	buf[i++] = TLV_ADDR_INFO;
	buf[i++] = 0x0d;                         /* 1 + 12 */
	buf[i++] = 0x00;
	buf[i++] = 0x01;                         /* one entry */
	memcpy(buf + i, &phys, 8);     i += 8;
	memcpy(buf + i, &size, 4);     i += 4;

	return i;
}

int main(int argc, char **argv)
{
	/* Size to advertise for MEM_QUERY_SIZE. Zero means "nothing available",
	 * which is the safe default; pass a size to find out whether the modem
	 * follows up with an allocation request when offered memory. */
	uint32_t offer = argc > 1 ? (uint32_t)strtoul(argv[1], NULL, 0) : 0;
	unsigned char req[4096];
	unsigned char resp[64];
	uint32_t node;
	uint32_t port;
	int sock;
	int ret;

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	setvbuf(stdout, NULL, _IOLBF, 0);

	sock = qrtr_open(0);
	if (sock < 0) {
		fprintf(stderr, "failed to open qrtr socket: %s\n", strerror(-sock));
		return 1;
	}

	ret = qrtr_publish(sock, MEMSHARE_SERVICE, MEMSHARE_VERSION, MEMSHARE_INSTANCE);
	if (ret < 0) {
		fprintf(stderr, "failed to publish service %d: %s\n",
			MEMSHARE_SERVICE, strerror(-ret));
		return 1;
	}
	printf("memshare: published QMI service %d\n", MEMSHARE_SERVICE);

	while (!stop) {
		uint16_t txn;
		uint16_t msg_id;
		unsigned int len;

		ret = qrtr_poll(sock, 1000);
		if (ret < 0) {
			if (errno == EINTR)
				continue;
			fprintf(stderr, "poll failed: %s\n", strerror(errno));
			break;
		}
		if (ret == 0)
			continue;

		ret = qrtr_recvfrom(sock, req, sizeof(req), &node, &port);
		if (ret < 0) {
			if (errno == EINTR)
				continue;
			fprintf(stderr, "recv failed: %s\n", strerror(errno));
			break;
		}

		/* Control traffic is not ours to answer. */
		if (port == QRTR_PORT_CTRL || ret < 7)
			continue;
		if (req[0] != QMI_REQUEST)
			continue;

		memcpy(&txn, req + 1, 2);
		memcpy(&msg_id, req + 3, 2);

		printf("memshare: request from node=%u port=%u %s (0x%04x) txn=%u\n",
		       node, port, msg_name(msg_id), msg_id, txn);
		{
			/* Dump the raw request: implementing a real allocator
			 * needs the exact TLVs the modem sends. */
			int i;

			printf("memshare: raw ");
			for (i = 0; i < ret && i < 96; i++)
				printf("%02x", req[i]);
			printf("\n");
		}

		switch (msg_id) {
		case MEM_QUERY_SIZE_REQ:
			printf("memshare: reporting size %u\n", offer);
			len = build_response(resp, txn, msg_id, 0, 0, true, offer);
			break;
		case MEM_ALLOC_REQ:
		case MEM_ALLOC_GENERIC_REQ:
			if (offer == 0) {
				/* Not advertising memory, so refuse cleanly
				 * rather than leaving the modem waiting. */
				len = build_response(resp, txn, msg_id, 1, 0, false, 0);
				break;
			}
			len = build_alloc_response(resp, txn, msg_id,
						   parse_sequence_id(req, ret),
						   REGION_BASE, offer);
			printf("memshare: handing out 0x%llx (%u bytes)\n",
			       (unsigned long long)REGION_BASE, offer);
			break;
		case MEM_FREE_REQ:
		case MEM_FREE_GENERIC_REQ:
			len = build_response(resp, txn, msg_id, 0, 0, false, 0);
			break;
		default:
			printf("memshare: no handler for 0x%04x, ignoring\n", msg_id);
			continue;
		}

		ret = qrtr_sendto(sock, node, port, resp, len);
		if (ret < 0)
			fprintf(stderr, "memshare: failed to answer: %s\n",
				strerror(-ret));
		else
			printf("memshare: answered %s\n", msg_name(msg_id));
	}

	qrtr_close(sock);
	return 0;
}
