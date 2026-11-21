#!/usr/bin/env python3
"""让 msm_fb_ea_enable（ROM 的 DC 调光开关）真正打开面板硬件 DC 模式。

背景
----
- MIUI 官方内核里没有 exposure_adjustment（EA）这套东西，它的「防闪烁模式」
  是靠面板 DDIC 的 DC 模式实现的（dsi_panel_mi.c 的 DISPPARAM_DC_ON，
  对应面板 DTS 的 mi,mdss-dsi-dc-on-command，B2=0xC8），所以整段亮度都是
  直流调光，不存在「低亮度回落 PWM」。
- Lineage/Clover 的 DC 开关只写 msm_fb_ea_enable -> ea_panel_mode_ctrl()，
  而那段代码只做低亮度 PCC（DSPP）缩放，从来没有把面板切到 DC 模式；
  被钳到 ELVSS_OFF_THRESHOLD 的 DBV 依旧是面板自己的 PWM，所以亮度一低就闪。

改法
----
1. ea_panel_mode_ctrl() 额外把面板切到硬件 DC 模式（DC ON/OFF 命令集），
   并同步 dc_enable/dc_type，使每次亮屏后 dsi_panel_enable() 会自动补发，
   不需要用户再点一次开关。
2. 保留原有的低亮度 PCC 缩放作为兜底（DC 的低亮端再叠一层像素缩放）。
"""

import os
import sys

REL = "techpack/display/msm/dsi/exposure_adjustment.c"
MARK = "[dc] full-range dc dimming"

NEW_FUNC = '''void ea_panel_mode_ctrl(struct dsi_panel *panel, bool enable)
{
	if (!panel)
		return;

	if (pcc_backlight_enable != enable) {
		pcc_backlight_enable = enable;

		/*
		 * 全程 DC：msm_fb_ea_enable 在 ROM 里就是「DC 调光」开关。
		 * 只做低亮度 PCC 缩放是不够的——钳到 ELVSS_OFF_THRESHOLD 的
		 * DBV 依旧是面板 PWM，所以这里同时把 DDIC 切进硬件 DC 模式。
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

		dsi_panel_set_backlight(panel, last_level);
		if (!enable)
			ea_panel_send_pcc(ELVSS_OFF_THRESHOLD);
	} else if (last_level == 0 && !pcc_backlight_enable) {
		ea_panel_send_pcc(ELVSS_OFF_THRESHOLD);
	}
}'''


def fail(msg):
    print("::error::" + msg)
    sys.exit(1)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(root, REL)
    if not os.path.isfile(path):
        fail("找不到 " + REL)

    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("[*] 已经打过 DC 全程补丁，跳过")
        return 0

    sig = "void ea_panel_mode_ctrl(struct dsi_panel *panel, bool enable)"
    try:
        start = src.index(sig)
        end = src.index("\n}\n", start) + 3
    except ValueError:
        fail("ea_panel_mode_ctrl() 结构不符合预期，需要人工确认")

    src = src[:start] + NEW_FUNC + "\n" + src[end:]

    # ---- 落地前自检：依赖的符号/字段必须存在 ----
    checks = {
        "dsi_panel_tx_cmd_set 调用": "dsi_panel_tx_cmd_set(panel, enable ?",
        "dc_enable 同步": "panel->mi_cfg.dc_enable = enable;",
        "dc_type 归零": "panel->mi_cfg.dc_type = 0;",
        "PCC 兜底保留": "ea_panel_send_pcc(ELVSS_OFF_THRESHOLD);",
    }
    for name, needle in checks.items():
        if needle not in src:
            fail("补丁自检失败: %s" % name)

    headers = {
        "techpack/display/msm/dsi/dsi_panel.h": [
            "dsi_panel_tx_cmd_set",
            "dsi_panel_update_backlight",
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

    # 打印改动后的关键片段，便于在 CI 日志里核对
    out = open(path, encoding="utf-8").read()
    i = out.index(sig)
    print("\n".join(out[i:i + 1400].splitlines()[:42]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
