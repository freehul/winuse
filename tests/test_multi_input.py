"""run_multi 单测：干跑 / 全成功 / 首失败即停 / 继续执行 四条路径。

不接触真实桌面：局部 StubDriver 只记录调用，并按需在指定次序上抛 WinUseError。
调用参数的正确性直接断言 `build_act_call` 的产物（翻译只有一份，测试不另立口径）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.act import ActSpec, build_act_call  # noqa: E402
from winuse.core.multi_input import (  # noqa: E402
    STEP_FIELDS,
    StepNotSupported,
    run_multi,
)
from winuse.driver.errors import DriverError, WinUseError  # noqa: E402

TARGET = {"pid": 100, "window_id": 200, "app_name": "Demo.exe", "title": "Demo"}
SNAP = "s00000001"


class StubDriver:
    """记录调用参数的局部假驱动；fail_on 是 0 基的「第几次调用失败」。"""

    def __init__(self, fail_on: int | None = None, error: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_on = fail_on
        self.error = error or DriverError("驱动调用失败：模拟", "退出码 2")

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if self.fail_on is not None and len(self.calls) - 1 == self.fail_on:
            raise self.error
        return {"tool": tool, "ok": True}

    @property
    def tools(self):
        return [tool for tool, _ in self.calls]


class RawDriver:
    """返回值不是 dict 的假驱动（验证结果被包成 dict）。"""

    def __init__(self):
        self.calls = []

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        return [1, 2, 3]


def steps_click_type_key():
    return [
        {"action": "click", "element_index": 1},
        {"action": "type", "element_index": 2, "text": "你好"},
        {"action": "key", "key": "return", "element_index": 2},
    ]


class TestDryRun(unittest.TestCase):
    def test_dry_run_sends_nothing(self):
        driver = StubDriver()
        out = run_multi(driver, steps_click_type_key(), TARGET, SNAP, execute=False, confirmed=True)

        self.assertEqual(driver.calls, [])
        self.assertTrue(out["dry_run"])
        self.assertTrue(out["ok"])
        self.assertIsNone(out["stopped_at"])
        self.assertEqual([s["index"] for s in out["steps"]], [0, 1, 2])
        self.assertEqual([s["action"] for s in out["steps"]], ["click", "type", "key"])
        self.assertEqual([s["code"] for s in out["steps"]], [None, None, None])

    def test_dry_run_args_match_build_act_call(self):
        """干跑回的就是构建函数将要下发的调用——两处口径必须一致。"""
        driver = StubDriver()
        out = run_multi(driver, steps_click_type_key(), TARGET, SNAP, execute=False, confirmed=True)

        for step, spec in zip(out["steps"], (s for s in steps_click_type_key())):
            expected_tool, expected_args = build_act_call(ActSpec(**spec), TARGET, SNAP)
            self.assertEqual(step["result"]["tool"], expected_tool)
            self.assertEqual(step["result"]["args"], expected_args)

    def test_click_args_carry_snapshot_and_background_delivery(self):
        driver = StubDriver()
        out = run_multi(driver, [{"action": "click", "element_index": 7}], TARGET, SNAP, confirmed=True)

        args = out["steps"][0]["result"]["args"]
        self.assertEqual(out["steps"][0]["result"]["tool"], "click")
        self.assertEqual(args, {
            "pid": 100,
            "window_id": 200,
            "element_index": 7,
            "snapshot_id": SNAP,
            "delivery_mode": "background",
        })

    def test_type_and_key_args(self):
        driver = StubDriver()
        out = run_multi(
            driver,
            [
                {"action": "type", "element_index": 3, "text": "关键词"},
                {"action": "key", "key": "escape", "modifiers": ["ctrl"], "element_index": 2},
                {"action": "scroll", "direction": "down", "amount": 2},
                {"action": "set_value", "element_token": "s1:9", "value": "新内容"},
                {"action": "activate"},
            ],
            TARGET,
            SNAP,
            confirmed=True,
        )
        by_action = {s["action"]: s["result"] for s in out["steps"]}

        self.assertEqual(by_action["type"]["tool"], "type_text")
        self.assertEqual(by_action["type"]["args"]["text"], "关键词")

        self.assertEqual(by_action["key"]["tool"], "press_key")
        self.assertEqual(by_action["key"]["args"]["key"], "escape")
        self.assertEqual(by_action["key"]["args"]["modifiers"], ["ctrl"])

        # 滚动是窗口级：驱动文档说 element_index 在 Windows 是 no-op
        self.assertEqual(by_action["scroll"]["tool"], "scroll")
        self.assertNotIn("element_index", by_action["scroll"]["args"])
        self.assertEqual(by_action["scroll"]["args"]["direction"], "down")
        self.assertEqual(by_action["scroll"]["args"]["amount"], 2)

        # 元素令牌形式不需要快照
        self.assertEqual(by_action["set_value"]["tool"], "set_value")
        self.assertEqual(by_action["set_value"]["args"]["element_token"], "s1:9")
        self.assertNotIn("snapshot_id", by_action["set_value"]["args"])
        self.assertEqual(by_action["set_value"]["args"]["value"], "新内容")

        self.assertEqual(by_action["activate"]["tool"], "bring_to_front")
        self.assertEqual(by_action["activate"]["args"], {"pid": 100, "window_id": 200})

    def test_empty_batch_is_ok(self):
        driver = StubDriver()
        out = run_multi(driver, [], TARGET, SNAP, confirmed=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["steps"], [])
        self.assertIsNone(out["stopped_at"])
        self.assertEqual(driver.calls, [])

    def test_act_spec_instances_are_accepted(self):
        driver = StubDriver()
        out = run_multi(
            driver, [ActSpec(action="click", element_index=1), {"action": "activate"}], TARGET, SNAP,
            confirmed=True,
        )
        self.assertEqual([s["action"] for s in out["steps"]], ["click", "activate"])
        self.assertTrue(out["ok"])


class TestExecute(unittest.TestCase):
    def test_all_success_in_order(self):
        driver = StubDriver()
        out = run_multi(driver, steps_click_type_key(), TARGET, SNAP, execute=True, confirmed=True)

        self.assertFalse(out["dry_run"])
        self.assertTrue(out["ok"])
        self.assertIsNone(out["stopped_at"])
        self.assertEqual(driver.tools, ["click", "type_text", "press_key"])
        self.assertEqual([s["ok"] for s in out["steps"]], [True, True, True])
        # 真发时带上驱动返回
        self.assertEqual(out["steps"][0]["result"]["driver_result"]["tool"], "click")
        self.assertNotIn("error", out["steps"][0]["result"])

    def test_first_failure_stops_the_batch(self):
        driver = StubDriver(fail_on=1)  # 第 2 次调用（type_text）失败
        out = run_multi(driver, steps_click_type_key(), TARGET, SNAP, execute=True, confirmed=True)

        self.assertFalse(out["ok"])
        self.assertEqual(out["stopped_at"], 1)
        self.assertEqual(len(out["steps"]), 2)
        self.assertEqual(driver.tools, ["click", "type_text"])  # 第 3 步没发出去
        self.assertFalse(out["steps"][1]["ok"])
        self.assertEqual(out["steps"][1]["code"], "DRIVER_ERROR")
        self.assertEqual(out["steps"][1]["result"]["error"]["code"], "DRIVER_ERROR")
        self.assertTrue(out["steps"][0]["ok"])

    def test_continue_on_error_records_every_step(self):
        driver = StubDriver(fail_on=1)
        out = run_multi(driver, steps_click_type_key(), TARGET, SNAP, execute=True, stop_on_error=False, confirmed=True)

        self.assertFalse(out["ok"])
        self.assertIsNone(out["stopped_at"])  # 继续模式没「停」，失败位置看各步 ok
        self.assertEqual(len(out["steps"]), 3)
        self.assertEqual([s["ok"] for s in out["steps"]], [True, False, True])
        self.assertEqual(driver.tools, ["click", "type_text", "press_key"])
        self.assertEqual([s["code"] for s in out["steps"]], [None, "DRIVER_ERROR", None])

    def test_custom_error_code_propagates(self):
        class RateLimited(WinUseError):
            code = "RATE_LIMITED"

        driver = StubDriver(fail_on=0, error=RateLimited("太快了"))
        out = run_multi(driver, steps_click_type_key(), TARGET, SNAP, execute=True, confirmed=True)
        self.assertEqual(out["steps"][0]["code"], "RATE_LIMITED")
        self.assertEqual(out["stopped_at"], 0)

    def test_non_dict_driver_result_is_wrapped(self):
        out = run_multi(RawDriver(), [{"action": "click", "element_index": 1}], TARGET, SNAP, execute=True, confirmed=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["steps"][0]["result"]["driver_result"], {"value": [1, 2, 3]})


class TestGuardErrors(unittest.TestCase):
    def test_unknown_action_raises_even_in_dry_run(self):
        driver = StubDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_multi(driver, [{"action": "fly", "element_index": 1}], TARGET, SNAP, confirmed=True)
        self.assertEqual(ctx.exception.code, "UNKNOWN_ACTION")
        self.assertEqual(driver.calls, [])

    def test_missing_snapshot_raises(self):
        with self.assertRaises(WinUseError) as ctx:
            run_multi(StubDriver(), [{"action": "click", "element_index": 1}], TARGET, None, execute=True)
        self.assertEqual(ctx.exception.code, "SNAPSHOT_REQUIRED")

    def test_steps_are_validated_before_any_step_is_sent(self):
        """R-13 附带：整批先校验再下发——第 2 步非法时第 1 步也不许已经发出去。

        旧行为是"边跑边校验，前一步已发、不回头撤销"；改成整批先校验后，
        agent 不会因为一个拼错的步名而在桌面上留下半批已执行的动作。
        """
        driver = StubDriver()
        with self.assertRaises(WinUseError):
            run_multi(driver, [{"action": "click", "element_index": 1}, {"action": "fly"}], TARGET, SNAP, True, confirmed=True)
        self.assertEqual(driver.tools, [], "参数错必须在下发任何一步之前就被发现")

    def test_unknown_step_field_is_refused(self):
        with self.assertRaises(StepNotSupported) as ctx:
            run_multi(StubDriver(), [{"action": "click", "index": 1}], TARGET, SNAP, confirmed=True)
        self.assertEqual(ctx.exception.code, "STEP_NOT_SUPPORTED")
        self.assertIn("index", ctx.exception.message)

    def test_step_without_action_is_refused(self):
        with self.assertRaises(StepNotSupported) as ctx:
            run_multi(StubDriver(), [{"element_index": 1}], TARGET, SNAP, confirmed=True)
        self.assertEqual(ctx.exception.code, "STEP_NOT_SUPPORTED")

    def test_non_dict_step_is_refused(self):
        with self.assertRaises(StepNotSupported):
            run_multi(StubDriver(), ["click"], TARGET, SNAP, confirmed=True)

    def test_step_fields_match_act_spec_domain(self):
        """步骤字段集合必须与 ActSpec 的定义域一致，否则 _to_spec 会在 kwargs 上炸。"""
        fields = set(ActSpec.__dataclass_fields__)
        self.assertEqual(set(STEP_FIELDS), fields)

    def test_key_step_still_needs_an_element_target(self):
        """run_multi 不放松 act.py 的守门：key/type 也要有作用对象（元素索引或令牌）。"""
        with self.assertRaises(WinUseError) as ctx:
            run_multi(StubDriver(), [{"action": "key", "key": "return"}], TARGET, SNAP, confirmed=True)
        self.assertEqual(ctx.exception.code, "MISSING_TARGET")


class TestBatchConfirmation(unittest.TestCase):
    """R-13/R-5：批量确认必须在核心，且不能顺手放宽别的授权。"""

    def test_unconfirmed_batch_sends_nothing(self):
        """未确认 → 整批拒绝，干跑也一样（不给"干跑通过、真跑被拒"的陷阱）。"""
        for execute in (False, True):
            with self.subTest(execute=execute):
                driver = StubDriver()
                out = run_multi(
                    driver, steps_click_type_key(), TARGET, SNAP, execute=execute, confirmed=False
                )
                self.assertFalse(out["ok"])
                self.assertEqual(out["code"], "CONFIRM_REQUIRED")
                self.assertTrue(out["requires_confirm"])
                self.assertEqual(out["steps"], [])
                self.assertIsNone(out["stopped_at"])
                self.assertEqual(driver.calls, []), "未确认时一步都不许下发"

    def test_unconfirmed_single_step_batch_is_also_blocked(self):
        """assess_batch 的口径：1 步也算批处理通道。"""
        driver = StubDriver()
        out = run_multi(driver, [{"action": "click", "element_index": 7}], TARGET, SNAP)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "CONFIRM_REQUIRED")
        self.assertEqual(driver.calls, [])

    def test_empty_batch_needs_no_confirmation(self):
        driver = StubDriver()
        out = run_multi(driver, [], TARGET, SNAP)
        self.assertTrue(out["ok"])
        self.assertEqual(out["steps"], [])
        self.assertEqual(driver.calls, [])

    def test_confirmed_batch_runs_and_forwards_confirm(self):
        driver = StubDriver()
        out = run_multi(
            driver, steps_click_type_key(), TARGET, SNAP, execute=True, confirmed=True
        )
        self.assertTrue(out["ok"], "显式确认后必须能真的跑（不是谁都跑不了）")
        self.assertEqual(driver.tools, ["click", "type_text", "press_key"])
        self.assertEqual([s["ok"] for s in out["steps"]], [True, True, True])

    def test_confirmed_batch_does_not_grant_overwrite(self):
        """批量确认 ≠ 覆盖授权：非空目标仍然被写值门禁拦。"""
        from winuse.core.act import EXISTING_CONTENT_CODE

        draft = "用户草稿-不可静默覆盖"

        class ContentDriver:
            def __init__(self):
                self.calls: list[tuple[str, dict]] = []

            def call(self, tool: str, args: dict) -> dict:
                self.calls.append((tool, args))
                if tool == "get_window_state":
                    return {"elements": [{"token": "t1", "index": 0, "value": draft}]}
                return {"ok": True, "tool": tool}

        driver = ContentDriver()
        out = run_multi(
            driver,
            [{"action": "set_value", "element_token": "t1", "value": "新值"}],
            TARGET,
            SNAP,
            execute=True,
            confirmed=True,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["steps"][0]["code"], EXISTING_CONTENT_CODE)
        self.assertFalse(
            any(tool not in {"get_window_state", "list_windows"} for tool, _ in driver.calls),
            "批量确认不能顺手放行覆盖既有内容",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
