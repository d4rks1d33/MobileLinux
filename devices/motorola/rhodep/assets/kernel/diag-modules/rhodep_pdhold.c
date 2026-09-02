// SPDX-License-Identifier: GPL-2.0
/*
 * rhodep_pdhold - pin rpmpd power domains at a performance state.
 *
 * Diagnostic instrument. mainline releases the modem's CX proxy vote at
 * handover (qcom_pas_handover -> qcom_pas_pds_disable), after which the AP's
 * only CX/MX votes come from its own consumers -- CX and MX both sit at 256
 * (NOMINAL) on this device. This pins them at the provider's maximum so that
 * "the modem's hardware engines are starved of a rail corner" can be tested
 * without a device tree change or a new boot image.
 *
 *   insmod rhodep_pdhold.ko idx=0,3          # SM6375_VDDCX, SM6375_VDDMX
 *   insmod rhodep_pdhold.ko idx=0,3 state=384
 *
 * Indices are the provider's own (dt-bindings/power/qcom-rpmpd.h):
 *   SM6375_VDDCX 0  _AO 1  _VFL 2   SM6375_VDDMX 3  _AO 4  _VFL 5
 *   SM6375_VDDGX 6            SM6375_VDD_LPI_CX 8   SM6375_VDD_LPI_MX 9
 */
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/pm_domain.h>
#include <linux/pm_runtime.h>

#define MAX_PDS 6

static char *compat = "qcom,sm6375-rpmpd";
module_param(compat, charp, 0444);
MODULE_PARM_DESC(compat, "compatible of the power domain provider");

static int idx[MAX_PDS];
static int nidx;
module_param_array(idx, int, &nidx, 0444);
MODULE_PARM_DESC(idx, "power domain indices to pin");

static int state = INT_MAX;
module_param(state, int, 0444);
MODULE_PARM_DESC(state, "performance state to request (clamped by provider)");

static struct platform_device *pdev[MAX_PDS];
static int npinned;

static int __init pdhold_init(void)
{
	struct device_node *np;
	int i, ret;

	if (!nidx)
		return -EINVAL;

	np = of_find_compatible_node(NULL, NULL, compat);
	if (!np) {
		pr_err("rhodep_pdhold: no node with compatible '%s'\n", compat);
		return -ENODEV;
	}

	for (i = 0; i < nidx; i++) {
		struct of_phandle_args args = {
			.np = np,
			.args_count = 1,
			.args[0] = idx[i],
		};
		struct platform_device *p;

		p = platform_device_register_simple("rhodep-pdhold", idx[i],
						    NULL, 0);
		if (IS_ERR(p)) {
			pr_err("rhodep_pdhold: idx %d: pdev %ld\n",
			       idx[i], PTR_ERR(p));
			continue;
		}

		ret = of_genpd_add_device(&args, &p->dev);
		if (ret) {
			pr_err("rhodep_pdhold: idx %d: add_device %d\n",
			       idx[i], ret);
			platform_device_unregister(p);
			continue;
		}

		pm_runtime_enable(&p->dev);
		ret = pm_runtime_resume_and_get(&p->dev);
		if (ret)
			pr_err("rhodep_pdhold: idx %d: resume %d\n", idx[i], ret);

		ret = dev_pm_genpd_set_performance_state(&p->dev, state);
		if (ret)
			pr_err("rhodep_pdhold: idx %d: perf %d\n", idx[i], ret);
		else
			pr_info("rhodep_pdhold: idx %d pinned at %d\n",
				idx[i], state);

		pdev[npinned++] = p;
	}
	of_node_put(np);

	pr_info("rhodep_pdhold: %d of %d domains pinned\n", npinned, nidx);
	return npinned ? 0 : -ENODEV;
}

static void __exit pdhold_exit(void)
{
	while (npinned--) {
		dev_pm_genpd_set_performance_state(&pdev[npinned]->dev, 0);
		pm_runtime_put_sync(&pdev[npinned]->dev);
		pm_runtime_disable(&pdev[npinned]->dev);
		pm_genpd_remove_device(&pdev[npinned]->dev);
		platform_device_unregister(pdev[npinned]);
	}
	pr_info("rhodep_pdhold: released\n");
}

module_init(pdhold_init);
module_exit(pdhold_exit);
MODULE_DESCRIPTION("Pin rpmpd power domains at a performance state (rhodep diagnostic)");
MODULE_LICENSE("GPL");
