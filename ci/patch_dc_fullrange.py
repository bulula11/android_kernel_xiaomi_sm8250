#!/usr/bin/env python3
"""让 msm_fb_ea_enable（ROM 的 DC 调光开关）真正打开面板硬件 DC 模式。

背景
----
- MIUI 官方内核（4.19.157-perf, cas, MIUI14）里没有 exposure_adjustment 这一套，
  dtbo 里 42 02 0b 面板是 mi,mdss-dsi-panel-dc-type = 0，防闪烁模式只做一件事：
  下发 mi,mdss-dsi-dc-on-command（F0 55 AA 52 08 00 / B2 C8 / 6F 09 / B2 00 /
  B3 全零），面板 DDIC 进入硬件 DC 模式，整个亮度区间都是直流调光。
  实测字节与内核树 dsi-panel-j2-42-02-0b-dsc-cmd.dtsi 完全一致。
- Lineage/Clover 的 DC 开关只写 msm_fb_ea_enable -> ea_panel_mode_ctrl()，
  那段代码只做低亮度 PCC(DSPP) 缩放，从来没把面板切进 DC 模式；被钳到
  ELVSS_OFF_THRESHOLD 的 DBV 依旧是面板自己的 PWM，所以亮度一低就闪。
  而且原实现里亮度升回阈值以上时不会把 PCC 复位，画面会一直偏暗。

改法（与 MIUI 对齐）
----
1. ea_panel_mode_ctrl() 直接下发 DSI_CMD_SET_MI_DC_ON / OFF（就是
   DISPPARAM_DC_ON 走的那条命令集），并同步 dc_type/dc_enable，
   使每次亮屏后 dsi_panel_enable() 自动补发，不用再点一次开关。
2. ea_panel_calc_backlight() 去掉 PCC 钳位，亮度完整交给面板 DBV，
   避免低亮度发灰、亮度回不去。
"""

import os
import sys

REL = "techpack/display/msm/dsi/exposure_adjustment.c"
MARK = "[dc] full-range dc dimming"

MODE_FUNC = '''void ea_panel_mode_ctrl(struct dsi_panel *panel, bool enable)
{
	if (!panel)
		return;

	if (pcc_backlight_enable == enable)
		return;

	pcc_backlight_enable = enable;

	/*
	 * 全程 DC：msm_fb_ea_enable 在 ROM 里就是「DC 调光」开关。
	 * 只做低亮度 PCC 缩放是不够的——面板本身仍在 PWM，所以这里直接把
	 * DDIC 切进硬件 DC 模式（与 MIUI 防闪烁模式同一条命令集）。
	 */
	panel->mi_cfg.dc_type = 0;
	panel->mi_cfg.dc_enable = enable;

	pr_info("[dc] full-range dc dimming %s, bl %d\\n",
			enable ? "on" : "off", last_level);

	mutex_lock(&panel->panel_lock);
	if (panel->panel_initialized)
		dsi_panel_tx_cmd_set(panel, enable ?
				DSI_CMD_SET_MI_DC_ON :
				DSI_CMD_SET_MI_DC_OFF);
	mutex_unlock(&panel->panel_lock);

	/* 清掉可能残留的 PCC 缩放，亮度完全由面板 DBV 决定 */
	if (panel->panel_initialized)
		ea_panel_send_pcc(ELVSS_OFF_THRESHOLD);
}'''

CALC_FUNC = '''u32 ea_panel_calc_backlight(u32 bl_lvl)
{
	/* 全程 DC：亮度完整交给面板 DBV，不再用 PCC 缩放兜底 */
	last_level = bl_lvl;

	return bl_lvl;
}'''


def fail(msg):
    print("::error::" + msg)
    sys.exit(1)


def replace_func(src, sig, body):
    start = src.index(sig)
    end = src.index("\n}\n", start) + 3
    return src[:start] + body + "\n" + src[end:]


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(root, REL)
    if not os.path.isfile(path):
        fail("找不到 " + REL)

    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("[*] 已经打过全程 DC 补丁，跳过")
        return 0

    try:
        src = replace_func(src, "void ea_panel_mode_ctrl(struct dsi_panel *panel, bool enable)", MODE_FUNC)
        src = replace_func(src, "u32 ea_panel_calc_backlight(u32 bl_lvl)", CALC_FUNC)
    except ValueError:
        fail("exposure_adjustment.c 结构不符合预期，需要人工确认")

    checks = {
        "DC ON 命令": "DSI_CMD_SET_MI_DC_ON",
        "dc_enable 同步": "panel->mi_cfg.dc_enable = enable;",
        "dc_type 归零": "panel->mi_cfg.dc_type = 0;",
        "PCC 已解除钳位": "return bl_lvl;",
    }
    for name, needle in checks.items():
        if needle not in src:
            fail("补丁自检失败: %s" % name)
    if "return ELVSS_OFF_THRESHOLD;" in src:
        fail("补丁自检失败: PCC 钳位没有去掉")

    headers = {
        "techpack/display/msm/dsi/dsi_panel.h": [
            "dsi_panel_tx_cmd_set",
            "struct dsi_panel_mi_cfg mi_cfg;",
        ],
        "techpack/display/msm/dsi/dsi_defs.h": [
            "DSI_CMD_SET_MI_DC_ON",
            "DSI_CMD_SET_MI_DC_OFF",
        ],
    }
    for rel, needles in headers.items():
        hp = os.path.join(root, rel)
        if not os.path.isfile(hp):
            fail("缺少头文件 " + rel)
        text = open(hp, encoding="utf-8", errors="ignore").read()
        for needle in needles:
            if needle not in text:
                fail("%s 里没有 %s，补丁前提不成立" % (rel, needle))

    open(path, "w", encoding="utf-8").write(src)
    print("[+] 已注入全程 DC：%s" % REL)

    out = open(path, encoding="utf-8").read()
    i = out.index("void ea_panel_mode_ctrl")
    print("\n".join(out[i:i + 1600].splitlines()[:46]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
