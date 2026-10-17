"""R-2：默认窗口目标必须吃**系统前台**，不是"叠得最靠前"。

背景（报告 R-2）：`_default_target()` 本来就支持 `foreground_window_id` 优先，但
没有人默认传它——窗口动作（`desktop._resolve_target`）构造 `StateOptions` 时没设，
于是回落到 `focus_window()`（z_index 最大）。项目 Backlog 已记录驱动自带的
`Cua.AgentCursorOverlay` 悬浮层也是顶层窗口且 z_index 最大，会骗走这个判定。
同时 `state` / `display` 输出的 `focus` 字段装的也是 z_index 最大的那个，
名字与语义不符。

本测试用"悬浮层 z_index 最大 + 真实前台是另一个窗口"这个场景，钉住三件事：

1. 默认目标解析吃系统前台（不是 z_index 最大的悬浮层）
2. 没有可用前台时才回落到 z_index（保留原来的兜底语义）
3. 输出里 `focus` = 真前台、`z_order_top` = z_index 最大，两者可以不同

铁律：**不碰真实桌面**。系统前台探测被 monkeypatch 到 `core.foreground` 一个出处。
"""

from __future__ import annotations

import unittest
from unittest import mock

from winuse.core import display as display_mod
from winuse.core import foreground as fg_mod
from winuse.core import guard as guard_mod
from winuse.core import state as state_mod
from winuse.core.desktop import WindowActionSpec, window_action

#: 悬浮层：z_index 最大（会骗走"取最前"的判定）
OVERLAY = {
    "app_name": "overlay.exe",
    "pid": 900,
    "window_id": 9001,
    "title": "占位悬浮层",
    "minimized": False,
    "is_on_screen": True,
    "bounds": {"x": 0, "y": 0, "width": 100, "height": 100},
    "z_index": 999,
}

#: 用户真正在看的窗口：z_index 很小
REAL = {
    "app_name": "notepad.exe",
    "pid": 901,
    "window_id": 9002,
    "title": "占位前台窗口",
    "minimized": False,
    "is_on_screen": True,
    "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
    "z_index": 1,
}


def _opts(**kw):
    return state_mod.StateOptions(**kw)


class TestDefaultTargetUsesSystemForeground(unittest.TestCase):
    def test_foreground_wins_over_z_order_top(self):
        with mock.patch.object(fg_mod, "foreground_window_id", return_value=REAL["window_id"]):
            target = state_mod.pick_window([OVERLAY, REAL], _opts())
        self.assertEqual(target["window_id"], REAL["window_id"], "默认必须选系统前台，不是 z_index 最大")

    def test_without_foreground_falls_back_to_z_order_top(self):
        with mock.patch.object(fg_mod, "foreground_window_id", return_value=0):
            target = state_mod.pick_window([OVERLAY, REAL], _opts())
        self.assertEqual(target["window_id"], OVERLAY["window_id"], "没有前台信息时才按 z_index 兜底")

    def test_explicit_option_still_wins(self):
        """显式指定优先级不变：显式 > 系统前台 > z_index。"""
        with mock.patch.object(fg_mod, "foreground_window_id", return_value=REAL["window_id"]):
            target = state_mod.pick_window([OVERLAY, REAL], _opts(window_id=OVERLAY["window_id"]))
        self.assertEqual(target["window_id"], OVERLAY["window_id"])

    def test_minimized_foreground_is_not_used(self):
        """前台窗口若已最小化，不能当默认目标（沿用原来的判定）。"""
        minimized = dict(REAL, minimized=True)
        with mock.patch.object(fg_mod, "foreground_window_id", return_value=minimized["window_id"]):
            target = state_mod.pick_window([OVERLAY, minimized], _opts())
        self.assertNotEqual(target["window_id"], minimized["window_id"])


class TestWindowActionTarget(unittest.TestCase):
    """窗口动作的核心路径也必须吃系统前台（原先这里完全没有传前台提示）。"""

    class _Driver:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        def call(self, tool: str, args: dict | None = None):
            self.calls.append((tool, dict(args or {})))
            if tool == "list_windows":
                return {"windows": [OVERLAY, REAL]}
            return {"ok": True}

    class _Native:
        def __init__(self):
            self.calls: list[tuple[str, list]] = []

        def show_window(self, hwnd, cmd):
            self.calls.append(("show_window", [hwnd, cmd]))
            return True

    def test_action_targets_foreground_not_overlay(self):
        driver, native = self._Driver(), self._Native()
        with mock.patch.object(fg_mod, "foreground_window_id", return_value=REAL["window_id"]):
            with mock.patch.object(guard_mod, "assess_action", return_value=guard_mod.RiskVerdict.safe()):
                out = window_action(
                    driver, WindowActionSpec(action="minimize"), execute=True, native=native
                )
        self.assertTrue(out["ok"])
        self.assertEqual(out["target"]["window_id"], REAL["window_id"])
        self.assertEqual(native.calls[0][1][0], REAL["window_id"], "动作必须下发给真前台窗口")


class TestOutputSeparatesFocusAndZOrder(unittest.TestCase):
    def test_state_output_distinguishes_foreground_from_z_order_top(self):
        windows = [OVERLAY, REAL]
        with mock.patch.object(fg_mod, "foreground_window_id", return_value=REAL["window_id"]):
            focus, z_top = state_mod.describe_focus(windows)
        self.assertEqual(focus["window_id"], REAL["window_id"], "focus 必须是系统前台")
        self.assertEqual(z_top["window_id"], OVERLAY["window_id"], "z_order_top 才是 z_index 最大")
        self.assertNotEqual(focus["window_id"], z_top["window_id"])

    def test_display_output_distinguishes_both(self):
        driver = TestWindowActionTarget._Driver()
        with mock.patch.object(fg_mod, "foreground_window_id", return_value=REAL["window_id"]):
            with mock.patch.object(display_mod, "enum_monitors", return_value=[]):
                out = display_mod.describe(driver)
        self.assertEqual(out["focus"]["window_id"], REAL["window_id"])
        self.assertEqual(out["z_order_top"]["window_id"], OVERLAY["window_id"])


class TestForegroundProbeSingleSource(unittest.TestCase):
    """系统前台只有一个出处：`core.foreground`。display / desktop 都只是转调。"""

    def test_display_delegates(self):
        with mock.patch.object(fg_mod, "foreground_window", return_value={"window_id": 1234, "pid": 5}) as called:
            self.assertEqual(display_mod.foreground_window()["window_id"], 1234)
        self.assertTrue(called.called, "display.foreground_window 必须委托给 core.foreground")

    def test_desktop_delegates(self):
        from winuse.core import desktop as desktop_mod

        with mock.patch.object(fg_mod, "foreground_window_id", return_value=4321) as called:
            self.assertEqual(desktop_mod._foreground_window_id(), 4321)
        self.assertTrue(called.called, "desktop._foreground_window_id 必须委托给 core.foreground")


if __name__ == "__main__":
    unittest.main(verbosity=2)
