# Out-of-tree diagnostic modules for motorola-rhodep.
#
# These build ON THE PHONE against the installed headers package -- gcc, make
# and /lib/modules/7.2.0-rc5/build are all present there -- which makes them a
# seconds-per-iteration instrument instead of a pmbootstrap build and a flash.
#
#   scp -r kernel/diag-modules kali@phone:/tmp/
#   ssh kali@phone 'cd /tmp/diag-modules && make'
#   sudo insmod rhodep_clkhold.ko idx=8
#
# The compiler-differs and pahole warnings are expected and harmless: the
# kernel config carries CONFIG_MODULE_ALLOW_BTF_MISMATCH=y precisely so that
# modules built with a different toolchain still load.
obj-m += rhodep_clkhold.o
obj-m += rhodep_pdhold.o
obj-m += rhodep_dbgmem.o
obj-m += rhodep_scmtrace.o
obj-m += rhodep_qrtrtrace.o
obj-m += rhodep_kvpeek.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
