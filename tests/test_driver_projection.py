"""Optional-6：`max_elements` 必须**下推给驱动**，否则内存峰值已经发生。

报告原文：「`CuaDriver` 和 `state` 在 MCP 门面裁剪前仍可能先完整物化很大的驱动回包；
考虑驱动侧 query/max 或流式/硬上限，避免内存峰值。」

问题的形状：核心侧裁剪发生在**解析完整个回包之后**——872 个元素的窗口会先把整份
JSON 读进内存再裁到 50 条。防峰值只能靠"让驱动少回"。

真机实测（`get_window_state`，某窗口）：不带 `max_elements` → **872** 个元素；
带 `max_elements=5` → **4** 个。驱动侧确实支持这个参数。

实现取舍：下推时给一个**放大系数**（`DOWNSTREAM_HEADROOM`），因为驱动的截断口径
（前 N 个）与核心的裁剪口径（只留可操作元素）不同——只推 `max_elements` 可能在
驱动侧就把可操作元素截掉，改变最终结果。
"""

from __future__ import annotations

import unittest

from winuse.core import state as state_mod
from winuse.core.state import StateOptions, build_state


class FakeDriver:
    def __init__(self, elements=None):
        self.elements = elements or []
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args=None):
        self.calls.append((tool, dict(args or {})))
        if tool == "list_windows":
            return {
                "windows": [
                    {"pid": 100, "window_id": 2001, "app_name": "demo.exe", "title": "占位",
                     "minimized": False, "is_on_screen": True, "z_index": 1,
                     "bounds": {"x": 0, "y": 0, "width": 800, "height": 600}}
                ]
            }
        return {
            "elements": self.elements,
            "snapshot_id": "s1",
            "element_count": len(self.elements),
            "elements_complete": True,
        }

    @property
    def state_args(self) -> dict:
        for tool, args in self.calls:
            if tool == "get_window_state":
                return args
        return {}


def _elements(count: int) -> list[dict]:
    return [
        {"index": i, "token": f"t{i}", "actions": ["invoke"], "label": "占位", "value": ""}
        for i in range(count)
    ]


class TestMaxElementsIsPushedDown(unittest.TestCase):
    def test_driver_receives_a_max_elements_cap(self):
        driver = FakeDriver(_elements(300))
        build_state(driver, StateOptions(window_id=2001, max_elements=50))
        args = driver.state_args
        self.assertIn("max_elements", args, "不下推的话驱动仍会把整棵树物化出来")
        self.assertGreaterEqual(args["max_elements"], 50, "下推值不能小于核心侧要求的上限")

    def test_headroom_keeps_core_semantics(self):
        """下推值要留出余量：驱动是"前 N 个"，核心是"可操作元素"，口径不同。"""
        driver = FakeDriver(_elements(300))
        build_state(driver, StateOptions(window_id=2001, max_elements=50))
        pushed = driver.state_args["max_elements"]
        self.assertEqual(pushed, 50 * state_mod.DOWNSTREAM_HEADROOM)
        self.assertGreater(pushed, 50)

    def test_pushdown_is_itself_capped(self):
        """下推值也受上限约束（否则"防峰值"变成"放大峰值"）。"""
        driver = FakeDriver(_elements(10))
        build_state(driver, StateOptions(window_id=2001, max_elements=10**9))
        pushed = driver.state_args["max_elements"]
        self.assertLessEqual(pushed, state_mod.MAX_PUSHED_ELEMENTS)

    def test_core_still_trims_to_the_requested_cap(self):
        driver = FakeDriver(_elements(300))
        out = build_state(driver, StateOptions(window_id=2001, max_elements=50))
        self.assertEqual(len(out["elements"]), 50, "最终结果仍以核心侧上限为准（下推只是防峰值）")
        self.assertTrue(out["stats"]["truncated_by_cap"])

    def test_query_is_still_passed_through(self):
        driver = FakeDriver(_elements(5))
        build_state(driver, StateOptions(window_id=2001, max_elements=50, query="文件"))
        self.assertEqual(driver.state_args.get("query"), "文件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
