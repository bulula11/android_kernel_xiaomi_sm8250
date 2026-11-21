#!/usr/bin/env python3
"""让 msm_fb_ea_enable（ROM 的 DC 调光开关）真正打开面板硬件 DC 模式。

背景
----
- 这台机器实际面板是 qcom,mdss_dsi_j1s_42_02_0a_mp_dsc_cmd（不是 j2 42 02 0b）。
- MIUI 的 dtbo 里 j1s 节点只有 mi,mdss-dsi-panel-dc-type = 0 和
  mi,mdss-dsi-panel-dc-update-flag，**没有** mi,mdss-dsi-dc-on-command。
  所以 dsi_panel_tx_cmd_set(DSI_CMD_SET_MI_DC_ON) 的 cmd_set.count == 0，
  驱动什么都不发，面板一直停在 4-pulse PWM，低亮度必然闪。
- 内核源码 arch/arm64/boot/dts/vendor/qcom/dsi-panel-j1s-42-02-0a-mp-dsc-cmd.dtsi
  里其实写好了这套 DC 命令（B2 C8 / 6F 09 / B2 00 / B3 / page4 D2 LUT），
  但内核用的是 dtbo 里的面板节点，源码里这份 dtsi 不生效。

改法
----
1. dsi_panel.c：当 cmd_set.count == 0 且 panel->type 含 "j1s" 时，用内置的
   j1s DC ON / DC OFF / AOD-to-DC 命令集兜底，命令字节与源码 dtsi 完全一致。
2. exposure_adjustment.c：ea_panel_mode_ctrl() 同步 dc_type/dc_enable 并
   下发 DC ON/OFF，同时清掉残留 PCC，亮度全部交给面板 DBV。
3. ea_panel_calc_backlight() 不再做 PCC 钳位。
"""

import os
import sys

REL_EA = "techpack/display/msm/dsi/exposure_adjustment.c"
REL_DP = "techpack/display/msm/dsi/dsi_panel.c"
MARK = "[dc] full-range dc dimming"
MARK_DP = "[dc] j1s dc fallback"

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
	 * DDIC 切进硬件 DC 模式（1 pulse，与 MIUI 防闪烁模式同一条命令集）。
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

# ---- j1s 面板 DC 命令（取自 dsi-panel-j1s-42-02-0a-mp-dsc-cmd.dtsi） ----
DC_PAGE0 = [0xF0, 0x55, 0xAA, 0x52, 0x08, 0x00]
DC_PAGE4 = [0xF0, 0x55, 0xAA, 0x52, 0x08, 0x04]

DC_ON = [
    ("long", DC_PAGE0),
    ("short", [0xB2, 0xC8]),          # disable EM_PULSE_OPT and dimming on
    ("short", [0x6F, 0x09]),
    ("short", [0xB2, 0x00]),          # 1 pulse -> DC
    ("long", [0xB3] + [0x00] * 32 + [0x07, 0xFF, 0x0F, 0xFF]),
    ("long", DC_PAGE4),
    ("long", [0xD2,
              0x08, 0x08, 0x08, 0x08, 0x06, 0x0E, 0x0E, 0x0E, 0x0E, 0x0A,
              0x12, 0x12, 0x12, 0x12, 0x0E, 0x18, 0x18, 0x18, 0x18, 0x15,
              0x12, 0x12, 0x12, 0x12, 0x12,
              0x11, 0x11, 0x11, 0x11, 0x09, 0x12, 0x12, 0x12, 0x12, 0x0C,
              0x16, 0x16, 0x16, 0x16, 0x12, 0x1C, 0x1C, 0x1C, 0x1C, 0x18,
              0x1A, 0x1A, 0x1A, 0x1A, 0x1A,
              0x08, 0x08, 0x08, 0x08, 0x06, 0x0B, 0x0B, 0x0B, 0x0B, 0x0A,
              0x10, 0x10, 0x10, 0x10, 0x0C, 0x16, 0x16, 0x16, 0x16, 0x16,
              0x12, 0x12, 0x12, 0x12, 0x12]),
]

DC_OFF = [
    ("long", DC_PAGE0),
    ("short", [0xB2, 0x58]),
    ("short", [0x6F, 0x09]),
    ("short", [0xB2, 0x20]),          # 4 pulse -> PWM
    ("long", [0xB3,
              0x00, 0x08, 0x00, 0x21, 0x00, 0x21, 0x00, 0x52, 0x00, 0x52,
              0x00, 0xA4, 0x00, 0xA4, 0x00, 0xF6, 0x00, 0xF6, 0x01, 0x48,
              0x01, 0x48, 0x01, 0x99, 0x01, 0x99, 0x03, 0x33, 0x03, 0x33,
              0x07, 0xFF, 0x07, 0xFF, 0x0F, 0xFF]),
    ("long", DC_PAGE4),
    ("long", [0xD2,
              0x12, 0x0C, 0x0C, 0x08, 0x06, 0x30, 0x18, 0x16, 0x0E, 0x0A,
              0x48, 0x20, 0x16, 0x12, 0x0E, 0x44, 0x20, 0x16, 0x18, 0x15,
              0x44, 0x20, 0x16, 0x12, 0x12,
              0x20, 0x20, 0x20, 0x11, 0x09, 0x80, 0x30, 0x20, 0x12, 0x0C,
              0x90, 0x30, 0x20, 0x16, 0x12, 0x80, 0x2A, 0x1E, 0x1C, 0x18,
              0x70, 0x20, 0x1E, 0x1A, 0x1A,
              0x12, 0x08, 0x08, 0x08, 0x06, 0x30, 0x12, 0x10, 0x0B, 0x0A,
              0x46, 0x1A, 0x10, 0x10, 0x0C, 0x30, 0x1A, 0x10, 0x16, 0x16,
              0x30, 0x1A, 0x10, 0x12, 0x12]),
]

AOD_DC_ON = [
    ("long", DC_PAGE0),
    ("short", [0xB2, 0xC8]),
    ("short", [0x6F, 0x09]),
    ("short", [0xB2, 0x00]),
]


def c_bytes(name, data):
    lines = []
    for i in range(0, len(data), 12):
        lines.append("\t" + ", ".join("0x%02X" % b for b in data[i:i + 12]) + ",")
    return "static u8 %s[] = {\n%s\n};\n" % (name, "\n".join(lines))


def c_cmds(name, cmds):
    out = ["static struct dsi_cmd_desc %s[] = {" % name]
    for idx, (kind, data) in enumerate(cmds):
        last = "true" if idx == len(cmds) - 1 else "false"
        dtype = "MIPI_DSI_DCS_LONG_WRITE" if kind == "long" else "MIPI_DSI_DCS_SHORT_WRITE_PARAM"
        out.append("\t{ .msg = { .type = %s, .tx_len = %d, .tx_buf = %s_%d },"
                   " .last_command = %s }," % (dtype, len(data), name, idx, last))
    out.append("};\n")
    return "\n".join(out)


def build_fallback():
    parts = ["/* %s: MIUI dtbo 没带 dc-on-command，这里内置 j1s 的 DC 命令集 */" % MARK_DP]
    blocks = [("j1s_dc_on", DC_ON), ("j1s_dc_off", DC_OFF), ("j1s_aod_dc_on", AOD_DC_ON)]
    for cname, cmds in blocks:
        for idx, (_, data) in enumerate(cmds):
            parts.append(c_bytes("%s_%d" % (cname, idx), data))
        parts.append(c_cmds(cname, cmds))
    return "\n".join(parts)


HOOK = '''	SDE_EVT32(type, state, count);

	if (!count && panel->name && strstr(panel->name, "42 02 0a")) {
		if (type == DSI_CMD_SET_MI_DC_ON) {
			cmds = j1s_dc_on;
			count = ARRAY_SIZE(j1s_dc_on);
			state = DSI_CMD_SET_STATE_LP;
		} else if (type == DSI_CMD_SET_MI_DC_OFF) {
			cmds = j1s_dc_off;
			count = ARRAY_SIZE(j1s_dc_off);
			state = DSI_CMD_SET_STATE_LP;
		} else if (type == DSI_CMD_SET_MI_AOD_TO_DC_ON) {
			cmds = j1s_aod_dc_on;
			count = ARRAY_SIZE(j1s_aod_dc_on);
			state = DSI_CMD_SET_STATE_LP;
		}
	}
'''


def fail(msg):
    print("::error::" + msg)
    sys.exit(1)


def replace_func(src, sig, body):
    start = src.index(sig)
    end = src.index("\n}\n", start) + 3
    return src[:start] + body + "\n" + src[end:]


def patch_ea(root):
    path = os.path.join(root, REL_EA)
    if not os.path.isfile(path):
        fail("找不到 " + REL_EA)
    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("[*] exposure_adjustment.c 已打过补丁，跳过")
        return
    try:
        src = replace_func(src, "void ea_panel_mode_ctrl(struct dsi_panel *panel, bool enable)", MODE_FUNC)
        src = replace_func(src, "u32 ea_panel_calc_backlight(u32 bl_lvl)", CALC_FUNC)
    except ValueError:
        fail("exposure_adjustment.c 结构不符合预期，需要人工确认")
    for needle in ("DSI_CMD_SET_MI_DC_ON", "panel->mi_cfg.dc_enable = enable;",
                   "panel->mi_cfg.dc_type = 0;", "return bl_lvl;"):
        if needle not in src:
            fail("exposure_adjustment.c 补丁自检失败: " + needle)
    if "return ELVSS_OFF_THRESHOLD;" in src:
        fail("exposure_adjustment.c 补丁自检失败: PCC 钳位没有去掉")
    open(path, "w", encoding="utf-8").write(src)
    print("[+] exposure_adjustment.c: 全程 DC 开关已注入")


def patch_dp(root):
    path = os.path.join(root, REL_DP)
    if not os.path.isfile(path):
        fail("找不到 " + REL_DP)
    src = open(path, encoding="utf-8").read()
    if MARK_DP in src:
        print("[*] dsi_panel.c 已打过补丁，跳过")
        return

    anchor = "\tSDE_EVT32(type, state, count);\n"
    if src.count(anchor) != 1:
        fail("dsi_panel.c 里 SDE_EVT32(type, state, count) 不唯一，无法安全插入")
    src = src.replace(anchor, HOOK, 1)

    sig = "int dsi_panel_tx_cmd_set(struct dsi_panel *panel,\n\t\t\t\tenum dsi_cmd_set_type type)\n{"
    if src.count(sig) != 1:
        fail("dsi_panel.c 里 dsi_panel_tx_cmd_set 定义不唯一")
    src = src.replace(sig, build_fallback() + "\n" + sig, 1)

    for needle in ("j1s_dc_on", "j1s_dc_off", "j1s_aod_dc_on",
                   'strstr(panel->name, "42 02 0a")', "ARRAY_SIZE(j1s_dc_on)"):
        if needle not in src:
            fail("dsi_panel.c 补丁自检失败: " + needle)
    open(path, "w", encoding="utf-8").write(src)
    print("[+] dsi_panel.c: j1s 内置 DC 命令集已注入")


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."

    headers = {
        "techpack/display/msm/dsi/dsi_panel.h": [
            "dsi_panel_tx_cmd_set",
            "struct dsi_panel_mi_cfg mi_cfg;",
        ],
        "techpack/display/msm/dsi/dsi_defs.h": [
            "DSI_CMD_SET_MI_DC_ON",
            "DSI_CMD_SET_MI_DC_OFF",
            "DSI_CMD_SET_MI_AOD_TO_DC_ON",
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

    patch_ea(root)
    patch_dp(root)
    print("[dc] 全部补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
