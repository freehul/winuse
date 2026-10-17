"""winuse_act 单测：动作映射、参数组装、快照守门、干跑。

不接触真实驱动与桌面：核心是**纯函数** build_act_call（可穷举断言），
run_act 走 StubDriver。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.act import (  # noqa: E402
    ActSpec,
    build_act_call,
    run_act,
)
from winuse.driver.errors import WinUseError  # noqa: E402

TARGET = {"pid": 100, "window_id": 200, "app_name": "Demo.exe", "title": "Demo"}
SNAP = "s0000000a"


class TestBuildActCall(unittest.TestCase):
    def test_click_by_index_carries_snapshot(self):
        tool, args = build_act_call(ActSpec(action="click", element_index=7), TARGET, SNAP)
        self.assertEqual(tool, "click")
        self.assertEqual(args["pid"], 100)
        self.assertEqual(args["window_id"], 200)
        self.assertEqual(args["element_index"], 7)
        self.assertEqual(args["snapshot_id"], SNAP)
        # 不抢焦点：显式要求后台投递
        self.assertEqual(args["delivery_mode"], "background")

    def test_semantic_actions_map_to_click(self):
        """驱动没有分开的 toggle/select/expand 工具，统一走元素点击（由驱动选 UIA 模式）。"""
        for action in ("invoke", "toggle", "select", "expand"):
            tool, args = build_act_call(ActSpec(action=action, element_index=1), TARGET, SNAP)
            self.assertEqual(tool, "click", action)
            self.assertEqual(args["element_index"], 1, action)

    def test_index_without_snapshot_is_refused(self):
        """驱动不变量：元素索引必须绑定同一份快照，否则可能点到过期位置上。"""
        with self.assertRaises(WinUseError) as ctx:
            build_act_call(ActSpec(action="click", element_index=3), TARGET, None)
        self.assertEqual(ctx.exception.code, "SNAPSHOT_REQUIRED")

    def test_token_form_needs_no_snapshot(self):
        tool, args = build_act_call(ActSpec(action="click", element_token="s1:9"), TARGET, None)
        self.assertEqual(tool, "click")
        self.assertEqual(args["element_token"], "s1:9")
        self.assertNotIn("element_index", args)
        self.assertNotIn("snapshot_id", args)

    def test_no_element_target_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            build_act_call(ActSpec(action="click"), TARGET, SNAP)
        self.assertEqual(ctx.exception.code, "MISSING_TARGET")

    def test_unknown_action_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            build_act_call(ActSpec(action="fly"), TARGET, SNAP)
        self.assertEqual(ctx.exception.code, "UNKNOWN_ACTION")

    def test_set_value_requires_value(self):
        with self.assertRaises(WinUseError) as ctx:
            build_act_call(ActSpec(action="set_value", element_index=1), TARGET, SNAP)
        self.assertEqual(ctx.exception.code, "MISSING_PARAM")
        tool, args = build_act_call(
            ActSpec(action="set_value", element_index=1, value="你好"), TARGET, SNAP
        )
        self.assertEqual(tool, "set_value")
        self.assertEqual(args["value"], "你好")

    def test_type_requires_text_and_uses_type_text(self):
        tool, args = build_act_call(ActSpec(action="type", element_index=1, text="abc"), TARGET, SNAP)
        self.assertEqual(tool, "type_text")
        self.assertEqual(args["text"], "abc")
        self.assertEqual(args["delivery_mode"], "background")

    def test_key_requires_key_name(self):
        with self.assertRaises(WinUseError) as ctx:
            build_act_call(ActSpec(action="key", element_index=1), TARGET, SNAP)
        self.assertEqual(ctx.exception.code, "MISSING_PARAM")
        tool, args = build_act_call(
            ActSpec(action="key", key="return", element_index=1, modifiers=["ctrl"]), TARGET, SNAP
        )
        self.assertEqual(tool, "press_key")
        self.assertEqual(args["key"], "return")
        self.assertEqual(args["modifiers"], ["ctrl"])

    def test_scroll_is_whole_window_on_windows(self):
        """驱动文档：scroll 的 element_index 在 Windows 是 no-op —— 不要传，免得误导。"""
        tool, args = build_act_call(
            ActSpec(action="scroll", element_index=5, direction="down", amount=2), TARGET, SNAP
        )
        self.assertEqual(tool, "scroll")
        self.assertEqual(args["direction"], "down")
        self.assertEqual(args["amount"], 2)
        self.assertNotIn("element_index", args)
        self.assertNotIn("snapshot_id", args)

    def test_scroll_requires_direction(self):
        with self.assertRaises(WinUseError) as ctx:
            build_act_call(ActSpec(action="scroll"), TARGET, SNAP)
        self.assertEqual(ctx.exception.code, "MISSING_PARAM")

    def test_activate_targets_window_only(self):
        tool, args = build_act_call(ActSpec(action="activate"), TARGET, SNAP)
        self.assertEqual(tool, "bring_to_front")
        self.assertEqual(args, {"pid": 100, "window_id": 200})


class StubDriver:
    def __init__(self, result=None, boom=None):
        self.calls = []
        self.result = result if result is not None else {"ok": True}
        self.boom = boom

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if self.boom:
            raise self.boom
        return self.result


class TestRunAct(unittest.TestCase):
    def test_dry_run_does_not_call_driver(self):
        driver = StubDriver()
        out = run_act(driver, ActSpec(action="click", element_index=1), TARGET, SNAP, execute=False)
        self.assertEqual(driver.calls, [])
        self.assertTrue(out["dry_run"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["would_call"]["tool"], "click")

    def test_execute_calls_driver_once(self):
        driver = StubDriver({"_note": "clicked"})
        out = run_act(driver, ActSpec(action="click", element_index=1), TARGET, SNAP, execute=True)
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(driver.calls[0][0], "click")
        self.assertFalse(out["dry_run"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], {"_note": "clicked"})

    def test_driver_error_is_reported_not_raised(self):
        driver = StubDriver(boom=WinUseError("boom", "detail"))
        out = run_act(driver, ActSpec(action="click", element_index=1), TARGET, SNAP, execute=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["message"], "boom")

    def test_guard_errors_still_raise(self):
        """参数拼错是编程错误，应当直接抛，不该被吞成 ok=false。"""
        with self.assertRaises(WinUseError):
            run_act(StubDriver(), ActSpec(action="click"), TARGET, SNAP, execute=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
