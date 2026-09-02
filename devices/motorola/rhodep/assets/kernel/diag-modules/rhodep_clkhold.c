// SPDX-License-Identifier: GPL-2.0
/*
 * rhodep_clkhold - hold RPM (or any provider's) clocks enabled, by index.
 *
 * Diagnostic instrument, not a fix. It exists to answer "does the modem need
 * a clock the application processor never votes for?" without a device tree
 * change or a new boot image: no mainline node references the QDSS clock on
 * sm6375, and mainline describes none of the 110 CoreSight blocks the vendor
 * tree does, so there is no consumer to add a phandle to.
 *
 *   insmod rhodep_clkhold.ko idx=8          # RPM_SMD_QDSS_CLK
 *   insmod rhodep_clkhold.ko idx=8,9        # QDSS + QDSS_A
 *   insmod rhodep_clkhold.ko compat="qcom,rpmcc-sm6375" idx=8
 */
#include <linux/module.h>
#include <linux/clk.h>
#include <linux/clk-provider.h>
#include <linux/of.h>

#define MAX_CLKS 8

static char *compat = "qcom,rpmcc-sm6375";
module_param(compat, charp, 0444);
MODULE_PARM_DESC(compat, "compatible of the clock provider node");

static int idx[MAX_CLKS];
static int nidx;
module_param_array(idx, int, &nidx, 0444);
MODULE_PARM_DESC(idx, "clock indices to prepare_enable and hold");

static struct clk *held[MAX_CLKS];
static int nheld;

static int __init clkhold_init(void)
{
	struct device_node *np;
	int i, ret;

	if (!nidx) {
		pr_err("rhodep_clkhold: no idx= given\n");
		return -EINVAL;
	}

	np = of_find_compatible_node(NULL, NULL, compat);
	if (!np) {
		pr_err("rhodep_clkhold: no node with compatible '%s'\n", compat);
		return -ENODEV;
	}

	for (i = 0; i < nidx; i++) {
		struct of_phandle_args spec = {
			.np = np,
			.args_count = 1,
			.args[0] = idx[i],
		};
		struct clk *c = of_clk_get_from_provider(&spec);

		if (IS_ERR(c)) {
			pr_err("rhodep_clkhold: idx %d: get failed %ld\n",
			       idx[i], PTR_ERR(c));
			continue;
		}
		ret = clk_prepare_enable(c);
		if (ret) {
			pr_err("rhodep_clkhold: idx %d (%s): enable failed %d\n",
			       idx[i], __clk_get_name(c), ret);
			clk_put(c);
			continue;
		}
		held[nheld++] = c;
		pr_info("rhodep_clkhold: holding idx %d (%s) rate %lu\n",
			idx[i], __clk_get_name(c), clk_get_rate(c));
	}
	of_node_put(np);

	pr_info("rhodep_clkhold: %d of %d clocks held\n", nheld, nidx);
	return nheld ? 0 : -ENODEV;
}

static void __exit clkhold_exit(void)
{
	while (nheld--) {
		clk_disable_unprepare(held[nheld]);
		clk_put(held[nheld]);
	}
	pr_info("rhodep_clkhold: released\n");
}

module_init(clkhold_init);
module_exit(clkhold_exit);
MODULE_DESCRIPTION("Hold RPM clocks enabled by index (rhodep diagnostic)");
MODULE_LICENSE("GPL");
