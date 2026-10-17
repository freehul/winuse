"""winuse_state 单测：裁剪策略、窗口选择、焦点判定、体量统计。

全部用 StubDriver（Python 对象），**不接触真实驱动与桌面**。
数据形状照抄 cua-driver 的真实返回（2026-09-26 实测采样）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.state import (  # noqa: E402
    ACTIVE_ACTIONS,
    StateOptions,
    build_state,
    focus_window,
    pick_window,
    trim_elements,
)
from winuse.driver.errors import WinUseError  # noqa: E402


def win(pid, wid, title, app="App.exe", x=0, y=0, w=800, h=600, z=1, minimized=False, on_screen=True):
    """造一个 list_windows 记录（字段与驱动实测一致）。"""
    return {
        "app_name": app,
        "bounds": {"x": x, "y": y, "width": w, "height": h},
        "is_on_screen": on_screen,
        "layer": 0,
        "minimized": minimized,
        "pid": pid,
        "title": title,
        "window_id": wid,
        "z_index": z,
    }


def el(i, role, label=None, actions=None, enabled=True, frame=None, value=None, depth=1):
    """造一个 get_window_state 元素（字段与驱动实测一致）。"""
    out = {
        "actions": list(actions) if actions else [],
        "depth": depth,
        "element_index": i,
        "element_token": f"s00000001:{i}",
        "enabled": enabled,
        "frame": frame or {"x": 10 * i, "y": 20, "h": 30, "w": 90},
        "role": role,
    }
    if label is not None:
        out["label"] = label
    if value is not None:
        out["value"] = value
    return out


def window_state(elements, pid=100, wid=200, title="Demo"):
    """造一份 get_window_state 原始返回（含必须被丢弃的截图与 markdown）。"""
    return {
        "_note": "element bounds are in native desktop coordinates",
        "app_name": "Demo.exe",
        "element_count": len(elements),
        "elements": elements,
        "elements_complete": True,
        "pid": pid,
        "returned_element_count": len(elements),
        "screenshot_height": 600,
        "screenshot_mime_type": "image/png",
        "screenshot_png_b64": "iVBORw0KGgoAAAANSUhEUg" + "A" * 5000,
        "screenshot_width": 800,
        "snapshot_id": "s00000001",
        "total_element_count": len(elements),
        "tree_markdown": "- Document\n  - Button '确定'\n" * 50,
        "window_bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        "window_id": wid,
        "window_title": title,
    }


class StubDriver:
    """记录调用参数的假驱动。"""

    def __init__(self, windows, state):
        self.windows = windows
        self.state = state
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if tool == "list_windows":
            return {"windows": self.windows, "_legacy_windows": [], "current_space_id": None}
        if tool == "get_window_state":
            return self.state
        raise AssertionError(f"StubDriver 未预期的工具：{tool}")

    def called_with(self, tool):
        return [args for name, args in self.calls if name == tool]


def interactive_plus_noise():
    """3 个可交互 + 2 个纯文本 + 1 个只可滚动 + 1 个禁用。"""
    return [
        el(0, "Document", "Demo", actions=["set_value", "text", "scroll"]),
        el(1, "Button", "确定", actions=["invoke"]),
        el(2, "Edit", "搜索", actions=["set_value"]),
        el(3, "Text", "纯说明文字", actions=["text"]),
        el(4, "Text", "另一段说明", actions=["text"]),
        el(5, "Pane", "滚动区", actions=["scroll"]),
        el(6, "Button", "灰按钮", actions=["invoke"], enabled=False),
    ]


class TestTrimElements(unittest.TestCase):
    def test_keeps_only_active_and_counts_the_rest(self):
        kept, stats = trim_elements(interactive_plus_noise(), StateOptions())
        roles = [e["role"] for e in kept]
        self.assertIn("Button", roles)
        self.assertIn("Edit", roles)
        # Document 带 set_value → 属可交互，保留
        self.assertIn("Document", roles)
        # 纯文本与只可滚动的不进输出
        self.assertNotIn("纯说明文字", [e.get("label") for e in kept])
        self.assertNotIn("滚动区", [e.get("label") for e in kept])
        # 禁用的不进输出，但计数
        self.assertNotIn("灰按钮", [e.get("label") for e in kept])

        self.assertEqual(stats["elements_total"], 7)
        self.assertEqual(stats["elements_returned"], 3)
        self.assertEqual(stats["dropped_text_only"], 2)
        self.assertEqual(stats["dropped_scroll_only"], 1)
        self.assertEqual(stats["dropped_disabled"], 1)
        # 数字自洽：保留 + 各类丢弃 = 总数
        self.assertEqual(
            stats["elements_returned"]
            + stats["dropped_text_only"]
            + stats["dropped_scroll_only"]
            + stats["dropped_disabled"],
            stats["elements_total"],
        )

    def test_document_with_only_set_value_is_kept(self):
        kept, _ = trim_elements([el(0, "Document", "页", actions=["set_value"])], StateOptions())
        self.assertEqual(len(kept), 1)

    def test_cap_applies_and_is_reported(self):
        elements = [el(i, "Button", f"按钮{i}", actions=["invoke"]) for i in range(50)]
        kept, stats = trim_elements(elements, StateOptions(max_elements=10))
        self.assertEqual(len(kept), 10)
        self.assertTrue(stats["truncated_by_cap"])
        self.assertEqual(stats["cap"], 10)
        self.assertEqual(stats["elements_returned"], 10)

    def test_cap_not_flagged_when_not_needed(self):
        kept, stats = trim_elements([el(0, "Button", "A", actions=["invoke"])], StateOptions(max_elements=10))
        self.assertEqual(len(kept), 1)
        self.assertFalse(stats["truncated_by_cap"])

    def test_frames_included_only_when_asked(self):
        els = [el(0, "Button", "A", actions=["invoke"])]
        lean, _ = trim_elements(els, StateOptions(include_frames=False))
        rich, _ = trim_elements(els, StateOptions(include_frames=True))
        self.assertNotIn("frame", lean[0])
        self.assertIn("frame", rich[0])

    def test_markdown_and_screenshot_and_noisy_keys_are_dropped(self):
        els = [el(0, "Button", "A", actions=["invoke"])]
        els[0]["in_web_content"] = True
        els[0]["parent_index"] = 9
        kept, _ = trim_elements(els, StateOptions())
        self.assertNotIn("parent_index", kept[0])
        self.assertNotIn("screenshot_png_b64", kept[0])
        self.assertEqual(kept[0]["token"], "s00000001:0")  # element_token 保留（改名 token 还是原名？）

    def test_long_label_and_value_are_truncated(self):
        long_text = "很长" * 200
        kept, _ = trim_elements(
            [el(0, "Edit", long_text, actions=["set_value"], value=long_text)],
            StateOptions(label_max_chars=20, value_max_chars=10),
        )
        self.assertLessEqual(len(kept[0]["label"]), 21)
        self.assertLessEqual(len(kept[0]["value"]), 11)

    def test_weak_only_elements_can_be_kept_for_debugging(self):
        kept, _ = trim_elements(interactive_plus_noise(), StateOptions(keep_weak_only=True))
        labels = [e.get("label") for e in kept]
        self.assertIn("滚动区", labels)


class TestWindowSelection(unittest.TestCase):
    def setUp(self):
        self.windows = [
            win(100, 200, "主窗口", app="Demo.exe", z=5),
            win(100, 201, "副窗口", app="Demo.exe", z=2, minimized=True),
            win(300, 400, "Hermes", app="Hermes.exe", z=9),
            win(500, 501, "只有最小化", app="Mini.exe", z=1, minimized=True),
        ]

    def test_focus_is_max_z_index(self):
        self.assertEqual(focus_window(self.windows)["window_id"], 400)

    def test_focus_is_none_when_z_index_unavailable(self):
        """驱动文档明示：z_index 全为 null 时不得推断顺序。"""
        nulls = [win(1, 1, "A", z=None), win(2, 2, "B", z=None)]
        self.assertIsNone(focus_window(nulls))

    def test_pick_by_window_id(self):
        self.assertEqual(pick_window(self.windows, StateOptions(window_id=200))["pid"], 100)

    def test_pick_by_pid_skips_minimized(self):
        got = pick_window(self.windows, StateOptions(pid=100))
        self.assertEqual(got["window_id"], 200)

    def test_pick_refuses_minimized_target_by_default(self):
        """最小化窗口报的坐标是哨兵值 → 默认明确报错，绝不静默返回。"""
        with self.assertRaises(WinUseError) as ctx:
            pick_window(self.windows, StateOptions(pid=500))
        self.assertEqual(ctx.exception.code, "WINDOW_MINIMIZED")

    def test_pick_allows_minimized_when_explicitly_asked(self):
        got = pick_window(self.windows, StateOptions(pid=500, allow_minimized=True))
        self.assertEqual(got["window_id"], 501)

    def test_explicit_minimized_window_id_is_also_guarded(self):
        with self.assertRaises(WinUseError) as ctx:
            pick_window(self.windows, StateOptions(window_id=201))
        self.assertEqual(ctx.exception.code, "WINDOW_MINIMIZED")

    def test_pick_by_app_name_is_case_insensitive(self):
        self.assertEqual(pick_window(self.windows, StateOptions(app="hermes"))["window_id"], 400)

    def test_pick_by_app_name_matches_title_too(self):
        self.assertEqual(pick_window(self.windows, StateOptions(app="主窗口"))["window_id"], 200)

    def test_pick_defaults_to_focus(self):
        self.assertEqual(pick_window(self.windows, StateOptions())["window_id"], 400)

    def test_default_prefers_os_foreground_over_z_index(self):
        """实测教训：驱动自带悬浮层也是顶层窗口且 z_index 最大，会骗走"取最前"。"""
        windows = [
            win(900, 901, "Cua.AgentCursorOverlay", app="cua-driver.exe", z=99),
            win(300, 400, "Hermes", app="Hermes.exe", z=9),
        ]
        opts = StateOptions(foreground_window_id=400)
        self.assertEqual(pick_window(windows, opts)["window_id"], 400)

    def test_foreground_hint_ignored_when_minimized(self):
        windows = [win(300, 400, "Hermes", z=9), win(300, 401, "副窗", z=1, minimized=True)]
        opts = StateOptions(foreground_window_id=401)
        self.assertEqual(pick_window(windows, opts)["window_id"], 400)

    def test_pick_raises_clear_error_when_not_found(self):
        with self.assertRaises(WinUseError) as ctx:
            pick_window(self.windows, StateOptions(app="不存在的应用"))
        self.assertEqual(ctx.exception.code, "WINDOW_NOT_FOUND")


class TestBuildState(unittest.TestCase):
    def setUp(self):
        self.windows = [win(100, 200, "Demo", app="Demo.exe", z=5)]
        self.driver = StubDriver(self.windows, window_state(interactive_plus_noise()))

    def test_asks_driver_for_tree_only_without_screenshot(self):
        build_state(self.driver, StateOptions())
        args = self.driver.called_with("get_window_state")
        self.assertEqual(len(args), 1)
        self.assertIs(args[0]["include_screenshot"], False)
        self.assertEqual(args[0]["pid"], 100)
        self.assertEqual(args[0]["window_id"], 200)

    def test_output_has_no_screenshot_or_markdown(self):
        out = build_state(self.driver, StateOptions())
        blob = repr(out)
        self.assertNotIn("iVBORw0KGgo", blob)
        self.assertNotIn("tree_markdown", blob)

    def test_output_shape(self):
        out = build_state(self.driver, StateOptions())
        self.assertTrue(out["ok"])
        self.assertEqual(out["snapshot_id"], "s00000001")
        self.assertEqual(out["target"]["pid"], 100)
        self.assertEqual(len(out["elements"]), 3)
        self.assertIn("stats", out)
        self.assertIn("windows", out)
        # R-2 之后两个口径分开：z_order_top = z_index 最大（原先挂在 focus 上的那个），
        # focus = 系统前台（测试机没有对应真实窗口时可以是 None，故只断言键存在）
        self.assertEqual(out["z_order_top"]["window_id"], 200)
        self.assertIn("focus", out)
        self.assertIn("window_order_note", out)
        self.assertIn("coordinate_note", out)

    def test_stats_report_driver_side_totals(self):
        out = build_state(self.driver, StateOptions())
        self.assertEqual(out["stats"]["elements_total"], 7)
        self.assertEqual(out["stats"]["elements_returned"], 3)
        self.assertEqual(out["stats"]["driver_element_count"], 7)

    def test_raises_when_no_window_matches(self):
        driver = StubDriver([], window_state([]))
        with self.assertRaises(WinUseError) as ctx:
            build_state(driver, StateOptions(app="x"))
        self.assertEqual(ctx.exception.code, "WINDOW_NOT_FOUND")


if __name__ == "__main__":
    unittest.main(verbosity=2)
