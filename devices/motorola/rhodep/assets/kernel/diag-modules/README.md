# Diagnostic modules built on the phone

Two small out-of-tree modules that exist to test "does the modem need a clock or
a rail corner the application processor never asks for?" **without** a device
tree change, a kernel build or a flash.

That constraint is the point. Both questions are otherwise unaskable: no
mainline node on sm6375 references the QDSS clock, and nothing at all
references `SM6375_VDDMX` after the modem's proxy vote is released, so there is
no consumer to hang a phandle on. Adding one means a new DTB, which means a new
boot image, which means the experiment costs twenty minutes instead of two.

They build on the device, against the installed headers package:

```sh
scp -r kernel/diag-modules kali@192.168.1.53:/tmp/
ssh kali@192.168.1.53 'cd /tmp/diag-modules && make'
```

`gcc`, `make` and `/lib/modules/7.2.0-rc5/build` are all present on the phone
(the headers `.deb` is there for DKMS). The "compiler differs" and "pahole
version differs" warnings are expected: the config carries
`CONFIG_MODULE_ALLOW_BTF_MISMATCH=y` exactly so that this works.

## `rhodep_clkhold` — hold RPM clocks enabled by index

```sh
sudo insmod rhodep_clkhold.ko idx=8,9      # RPM_SMD_QDSS_CLK, QDSS_A
sudo insmod rhodep_clkhold.ko compat="qcom,rpmcc-sm6375" idx=4
```

Indices are the provider's own, from
`include/dt-bindings/clock/qcom,rpmcc.h`.

**Read this before drawing a conclusion from it.** On this platform the module
is very often a no-op, and that cost a wasted test run. `clk_smd_rpm_handoff()`
in `drivers/clk/qcom/clk-smd-rpm.c` already writes an enable to RPM for
*every* clock in the SoC's table, in both the active and the sleep set, at
probe:

```c
struct clk_smd_rpm_req req = {
	.key = cpu_to_le32(r->rpm_key),
	.value = cpu_to_le32(r->branch ? 1 : INT_MAX),
};
qcom_rpm_smd_write(..., QCOM_SMD_RPM_ACTIVE_STATE, ...);
qcom_rpm_smd_write(..., QCOM_SMD_RPM_SLEEP_STATE, ...);
```

So `qdss_clk` already reads `enable Y` at 19.2 MHz in `clk_summary` with a
prepare count of zero, before this module is loaded. Voting it again changes
nothing. **Check `clk_summary` first**; if the clock is already `Y`, a null
result from this module says nothing about the hypothesis.

## `rhodep_pdhold` — pin rpmpd domains at a performance state

```sh
sudo insmod rhodep_pdhold.ko idx=0,3            # SM6375_VDDCX, SM6375_VDDMX
sudo insmod rhodep_pdhold.ko idx=0,3 state=384  # TURBO instead of the maximum
```

Indices are from `include/dt-bindings/power/qcom-rpmpd.h`. `state` defaults to
`INT_MAX`, which `rpmpd_set_performance()` clamps to the provider's
`max_state` (`RPM_SMD_LEVEL_TURBO_NO_CPR` for sm6375).

It works by registering a bare platform device per domain and attaching it with
`of_genpd_add_device()`, then `pm_runtime_resume_and_get()` and
`dev_pm_genpd_set_performance_state()`. The runtime-PM get matters: rpmpd only
sends the corner to RPM if the domain is enabled --

```c
if (!pd->enabled && pd->key != KEY_FLOOR_CORNER && pd->key != KEY_FLOOR_LEVEL)
	return 0;
```

Verify it took with

```sh
cat /sys/kernel/debug/pm_genpd/cx/perf_state
cat /sys/kernel/debug/pm_genpd/mx/perf_state
```

Unlike `clkhold`, this one really does change something: CX and MX both sit at
`256` (NOMINAL) in normal operation, because mainline drops the modem's proxy
vote at handover and the only remaining votes are the AP's own consumers.

## What they have found so far

Both were written for the silent whole-SoC reset (`docs/watchdog-ipa-lte-wip/`,
`docs/interconnect-sm6375-wip/GNSS-SM6375.md`) and both came back negative:

* CX **and** MX pinned at `TURBO_NO_CPR` for the whole run: the GNSS reproducer
  still resets the SoC. Rail corners that the AP can vote for are excluded.
* QDSS clock: no change, and for the reason above the run proves nothing. QDSS
  is **not** excluded.

Negative results, but each is an elimination with an instrument rather than an
argument, which is the only kind this bug has responded to.
