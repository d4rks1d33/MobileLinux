/*
 * Publish every QMI service id nobody else provides, and log which ones the
 * modem talks to.
 *
 * The point is to stop guessing. The modem does not look services up: the
 * kernel name service *announces* them as they appear, and the modem connects
 * to the ones it wants. That is exactly how memshare was found, by publishing
 * service 52 on a hunch and watching the modem immediately talk to it. This
 * does the same thing systematically: offer everything that is missing and see
 * what it bites on.
 *
 * Nothing is answered, deliberately. The question here is only "does the modem
 * want to talk to this", and a request arriving is the whole answer. Answering
 * properly needs to know the protocol of whichever service turns up.
 *
 * Service ids already provided by somebody, on any node, are skipped. Offering
 * a duplicate of something the modem itself provides would be worse than
 * useless: clients such as ModemManager could bind to the wrong one.
 *
 * Build on the phone:
 *     sudo apt install libqrtr-dev
 *     gcc -O2 -Wall -o service-probe service-probe.c -lqrtr
 *
 * Run it with the list of ids to offer, then reboot with it started before
 * rmtfs so it is up before the modem is:
 *     service-probe 6 13 18 19 20 25 ...
 */

#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <libqrtr.h>

#define MAX_SERVICES 128

/*
 * Optionally answer every request with a bare success. The QMI result TLV is
 * the one part of the protocol that is the same for every service, so this can
 * be sent without knowing anything about the service in question. It is not a
 * real implementation, it is a way to ask "does this modem only need to be
 * acknowledged", which is precisely the question memshare turned out to answer
 * with no.
 */
static int answer_mode;

static unsigned int build_ok(unsigned char *buf, uint16_t txn, uint16_t msg_id)
{
	uint16_t payload = 7;
	uint16_t zero = 0;
	unsigned int i = 0;

	buf[i++] = QMI_RESPONSE;
	memcpy(buf + i, &txn, 2);     i += 2;
	memcpy(buf + i, &msg_id, 2);  i += 2;
	memcpy(buf + i, &payload, 2); i += 2;
	buf[i++] = 0x02;              /* result TLV */
	buf[i++] = 0x04;
	buf[i++] = 0x00;
	memcpy(buf + i, &zero, 2);    i += 2;  /* success */
	memcpy(buf + i, &zero, 2);    i += 2;  /* no error */

	return i;
}

static volatile sig_atomic_t stop;

static void on_signal(int signo)
{
	(void)signo;
	stop = 1;
}

int main(int argc, char **argv)
{
	struct pollfd fds[MAX_SERVICES];
	unsigned int services[MAX_SERVICES];
	unsigned char buf[4096];
	unsigned int count = 0;
	unsigned int hits = 0;
	int i;

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	setvbuf(stdout, NULL, _IOLBF, 0);

	if (argc < 2) {
		fprintf(stderr, "usage: %s <service-id> [<service-id> ...]\n", argv[0]);
		return 2;
	}

	for (i = 1; i < argc && count < MAX_SERVICES; i++) {
		unsigned int svc;

		if (!strcmp(argv[i], "--answer")) {
			answer_mode = 1;
			continue;
		}
		svc = (unsigned int)strtoul(argv[i], NULL, 0);
		int sock;
		int ret;

		sock = qrtr_open(0);
		if (sock < 0) {
			fprintf(stderr, "service %u: open failed: %s\n",
				svc, strerror(-sock));
			continue;
		}

		ret = qrtr_publish(sock, svc, 1, 0);
		if (ret < 0) {
			fprintf(stderr, "service %u: publish failed: %s\n",
				svc, strerror(-ret));
			qrtr_close(sock);
			continue;
		}

		services[count] = svc;
		fds[count].fd = sock;
		fds[count].events = POLLIN;
		count++;
	}

	printf("published %u services, waiting\n", count);

	while (!stop) {
		int ret = poll(fds, count, 1000);

		if (ret < 0) {
			if (errno == EINTR)
				continue;
			fprintf(stderr, "poll failed: %s\n", strerror(errno));
			break;
		}
		if (ret == 0)
			continue;

		for (i = 0; i < (int)count; i++) {
			uint32_t node;
			uint32_t port;
			int len;
			int j;

			if (!(fds[i].revents & POLLIN))
				continue;

			len = qrtr_recvfrom(fds[i].fd, buf, sizeof(buf), &node, &port);
			if (len < 0)
				continue;

			/* Control traffic is bookkeeping, not the modem asking
			 * for anything. */
			if (port == QRTR_PORT_CTRL)
				continue;

			hits++;
			printf("\n*** service %u: %d bytes from node=%u port=%u\n",
			       services[i], len, node, port);
			if (len >= 7) {
				uint16_t txn;
				uint16_t msg_id;

				memcpy(&txn, buf + 1, 2);
				memcpy(&msg_id, buf + 3, 2);
				printf("    QMI type=0x%02x txn=%u msg_id=0x%04x\n",
				       buf[0], txn, msg_id);
			}
			printf("    raw ");
			for (j = 0; j < len && j < 96; j++)
				printf("%02x", buf[j]);
			printf("\n");

			if (answer_mode && len >= 7 && buf[0] == QMI_REQUEST) {
				unsigned char resp[32];
				uint16_t txn;
				uint16_t msg_id;
				unsigned int rlen;

				memcpy(&txn, buf + 1, 2);
				memcpy(&msg_id, buf + 3, 2);
				rlen = build_ok(resp, txn, msg_id);
				if (qrtr_sendto(fds[i].fd, node, port, resp, rlen) < 0)
					printf("    (failed to answer)\n");
				else
					printf("    answered with success\n");
			}
		}
	}

	printf("\ndone: %u requests across %u published services\n", hits, count);
	for (i = 0; i < (int)count; i++)
		qrtr_close(fds[i].fd);

	return 0;
}
