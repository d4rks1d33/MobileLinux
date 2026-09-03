// rhodep: hold the rear camera sensor powered so it can be probed by hand.
//
// The s5kjn1 driver powers the part, reads 0 where 0x38e1 is expected, fails
// probe and powers back down -- which leaves nothing to talk to. This binds to
// the same node, brings the rails, MCLK and reset up, and then just stays
// loaded, so /dev/i2c-4 can be used interactively with i2ctransfer -f.
//
// Build on the phone (linux-headers-7.2.0-rc5 is installed and held):
//   make -C /lib/modules/$(uname -r)/build M=$PWD modules
//
// Order and delays follow the vendor's own power sequence, the one spelled out
// in DT form in yupik-camera-sensor-rb3.dtsi:
//   assert reset, 1 ms -> rails -> MCLK 24 MHz, 1 ms -> release reset, 18 ms
// All four steps are module parameters so they can be varied without a rebuild.
//
// SPDX-License-Identifier: GPL-2.0

#include <linux/clk.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/regulator/consumer.h>

static unsigned int pre_reset_ms = 1;
static unsigned int post_rail_ms = 1;
static unsigned int post_mclk_ms = 1;
static unsigned int post_reset_ms = 18;
static unsigned int mclk_hz = 24000000;
static bool assert_reset_first = true;
static bool mclk_before_reset = true;
static bool release_reset = true;
static bool enable_mclk = true;

module_param(pre_reset_ms, uint, 0644);
module_param(post_rail_ms, uint, 0644);
module_param(post_mclk_ms, uint, 0644);
module_param(post_reset_ms, uint, 0644);
module_param(mclk_hz, uint, 0644);
module_param(assert_reset_first, bool, 0644);
module_param(mclk_before_reset, bool, 0644);
module_param(release_reset, bool, 0644);
module_param(enable_mclk, bool, 0644);

struct camdiag {
	struct i2c_client *client;
	struct regulator *vdda, *vddd, *vddio;
	struct clk *mclk;
	struct gpio_desc *reset;
	bool mclk_on;
};

static int camdiag_rd16(struct i2c_client *c, u16 reg, u16 *val)
{
	u8 tx[2] = { reg >> 8, reg & 0xff };
	u8 rx[2] = { 0, 0 };
	struct i2c_msg m[2] = {
		{ .addr = c->addr, .flags = 0, .len = 2, .buf = tx },
		{ .addr = c->addr, .flags = I2C_M_RD, .len = 2, .buf = rx },
	};
	int ret = i2c_transfer(c->adapter, m, 2);

	if (ret != 2)
		return ret < 0 ? ret : -EIO;
	*val = (rx[0] << 8) | rx[1];
	return 0;
}

static int camdiag_probe(struct i2c_client *client)
{
	struct device *dev = &client->dev;
	struct camdiag *cd;
	u16 id = 0;
	int ret, i;

	cd = devm_kzalloc(dev, sizeof(*cd), GFP_KERNEL);
	if (!cd)
		return -ENOMEM;
	cd->client = client;

	cd->vdda  = devm_regulator_get(dev, "vdda");
	cd->vddd  = devm_regulator_get(dev, "vddd");
	cd->vddio = devm_regulator_get(dev, "vddio");
	if (IS_ERR(cd->vdda) || IS_ERR(cd->vddd) || IS_ERR(cd->vddio)) {
		dev_err(dev, "camdiag: regulators missing\n");
		return -EPROBE_DEFER;
	}

	cd->mclk = devm_clk_get(dev, NULL);
	if (IS_ERR(cd->mclk)) {
		dev_err(dev, "camdiag: no mclk\n");
		return PTR_ERR(cd->mclk);
	}

	/* GPIOD_OUT_HIGH on an active-low line means asserted, i.e. held in
	 * reset from the moment we own the pin.
	 */
	cd->reset = devm_gpiod_get_optional(dev, "reset",
			assert_reset_first ? GPIOD_OUT_HIGH : GPIOD_OUT_LOW);
	if (IS_ERR(cd->reset)) {
		dev_err(dev, "camdiag: no reset gpio\n");
		return PTR_ERR(cd->reset);
	}

	if (assert_reset_first && cd->reset) {
		gpiod_set_value_cansleep(cd->reset, 1);	/* asserted */
		msleep(pre_reset_ms);
	}

	/* Vendor's regulator-names order: cam_vio, cam_vana, cam_vdig. */
	ret = regulator_enable(cd->vddio);
	if (ret)
		return ret;
	ret = regulator_enable(cd->vdda);
	if (ret)
		return ret;
	ret = regulator_enable(cd->vddd);
	if (ret)
		return ret;
	msleep(post_rail_ms);

	if (mclk_before_reset && enable_mclk) {
		clk_set_rate(cd->mclk, mclk_hz);
		ret = clk_prepare_enable(cd->mclk);
		if (ret)
			return ret;
		cd->mclk_on = true;
		msleep(post_mclk_ms);
	}

	if (cd->reset && release_reset) {
		gpiod_set_value_cansleep(cd->reset, 0);	/* released */
		msleep(post_reset_ms);
	} else if (cd->reset) {
		dev_info(dev, "camdiag: leaving reset ASSERTED\n");
		msleep(post_reset_ms);
	}

	if (!mclk_before_reset && enable_mclk) {
		clk_set_rate(cd->mclk, mclk_hz);
		ret = clk_prepare_enable(cd->mclk);
		if (ret)
			return ret;
		cd->mclk_on = true;
		msleep(post_mclk_ms);
	}

	dev_info(dev, "camdiag: powered (mclk %u Hz, rate now %lu, delays %u/%u/%u/%u, reset_first=%d, mclk_before_reset=%d)\n",
		 mclk_hz, clk_get_rate(cd->mclk),
		 pre_reset_ms, post_rail_ms, post_mclk_ms, post_reset_ms,
		 assert_reset_first, mclk_before_reset);

	for (i = 0; i < 3; i++) {
		ret = camdiag_rd16(client, 0x0000, &id);
		dev_info(dev, "camdiag: read 0x0000 -> 0x%04x (ret %d)\n",
			 id, ret);
	}

	dev_info(dev, "camdiag: staying powered; poke it with:\n");
	dev_info(dev, "camdiag:   i2ctransfer -f -y 4 w2@0x56 0x00 0x00 r2\n");

	i2c_set_clientdata(client, cd);
	return 0;
}

static void camdiag_remove(struct i2c_client *client)
{
	struct camdiag *cd = i2c_get_clientdata(client);

	if (cd->reset)
		gpiod_set_value_cansleep(cd->reset, 1);
	/* Only what this instance turned on: keying the teardown off the module
	 * parameter leaks the clock the moment the parameter is changed between
	 * bind and unbind, and then an "MCLK off" test silently runs with the
	 * clock still on.
	 */
	if (cd->mclk_on)
		clk_disable_unprepare(cd->mclk);
	regulator_disable(cd->vddd);
	regulator_disable(cd->vdda);
	regulator_disable(cd->vddio);
}

static const struct of_device_id camdiag_of[] = {
	{ .compatible = "samsung,s5kjn1" },
	{ }
};
MODULE_DEVICE_TABLE(of, camdiag_of);

static struct i2c_driver camdiag_driver = {
	.driver = { .name = "camdiag", .of_match_table = camdiag_of },
	.probe = camdiag_probe,
	.remove = camdiag_remove,
};
module_i2c_driver(camdiag_driver);

MODULE_DESCRIPTION("rhodep camera sensor power-hold diagnostic");
MODULE_LICENSE("GPL");
