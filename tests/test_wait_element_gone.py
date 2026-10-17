"""element_gone（等元素消失）单测：命中 / 超时 / 参数不足 / 三种寻址方式。

为什么这个条件要单独存在：cua-driver 的 `verify_state` 明确拒绝 `element.exists=false`
（只接受 true），所以"元素没了"这件事**没法交给驱动判**，只能在核心侧轮询元素树。
本文件钉住的就是这条轮询的判定口径。

不接触真实桌面与真实时钟（与 tests/test_wait_for.py 同套路）：
- 局部 ScriptedDriver 按脚本回 `list_windows` / `get_window_state`（不动 tests/fake_driver.py，
  那是 CLI 层的假驱动）
- 注入 FakeClock，sleep 即时返回 → 整份用例真实耗时远小于一秒
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core import wait_for as wait_mod  # noqa: E402
from winuse.core.wait_for import (  # noqa: E402
    KINDS,
    WAIT_TIMEOUT,
    WaitSpec,
    wait_for,
)
from winuse.driver.errors import WinUseError  # noqa: E402


# ------------------------------------------------------------------ 构造器（口径同 test_wait_for.py）


def win(pid=100, wid=200, title="Demo", app="Demo.exe", z: int | None = 1, minimized=False, on_screen=True):
    return {
        "app_name": app,
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        "is_on_screen": on_screen,
        "layer": 0,
        "minimized": minimized,
        "pid": pid,
        "title": title,
        "window_id": wid,
        "z_index": z,
    }


def el(i, role="Button", label=None, value=None, actions=None, enabled=True, snapshot="s00000001"):
    """造一个 get_window_state 元素（令牌前缀 = 快照号，驱动每读一次就换一批）。"""
    out = {
        "actions": list(actions) if actions else [],
        "depth": 1,
        "element_index": i,
        "element_token": f"{snapshot}:{i}",
        "enabled": enabled,
        "frame": {"x": 10 * i, "y": 20, "h": 30, "w": 90},
        "role": role,
    }
    if label is not None:
        out["label"] = label
    if value is not None:
        out["value"] = value
    return out


def window_state(elements, pid=100, wid=200, snapshot_id="s00000001"):
    return {
        "elements": elements,
        "element_count": len(elements),
        "elements_complete": True,
        "pid": pid,
        "screenshot_png_b64": "iVBORw0KGgo" + "A" * 200,
        "snapshot_id": snapshot_id,
        "window_id": wid,
        "window_title": "Demo",
    }


class ScriptedDriver:
    """按脚本回话的局部假驱动：脚本是「每次调用的下一次返回」，末项重复。"""

    def __init__(self, windows_script=None, states_script=None):
        self.windows_script = list(windows_script or [[]])
        self.states_script = list(states_script or [window_state([])])
        self.calls: list[tuple[str, dict]] = []
        self._w = 0
        self._s = 0

    @staticmethod
    def _next(script, idx):
        item = script[min(idx, len(script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if tool == "list_windows":
            out = self._next(self.windows_script, self._w)
            self._w += 1
            return {"windows": list(out), "_legacy_windows": []}
        if tool == "get_window_state":
            out = self._next(self.states_script, self._s)
            self._s += 1
            return out
        raise AssertionError(f"ScriptedDriver 未预期的工具：{tool}")

    def called_with(self, tool):
        return [args for name, args in self.calls if name == tool]


class FakeClock:
    """假时钟：sleep 把假时间往前推，测试于是无需真等。"""

    def __init__(self, start=0.0):
        self.t = start
        self.slept: list[float] = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def run_wait(driver, spec, clock=None):
    clock = clock or FakeClock()
    return wait_for(driver, spec, sleep=clock.sleep, monotonic=clock.monotonic), clock


# ------------------------------------------------------------------ 只做加法：既有条件不动


class TestKindRegistration(unittest.TestCase):
    def test_element_gone_is_registered_without_disturbing_existing_kinds(self):
        """新增一个条件不能让既有条件消失或改名（等待族是公开契约）。"""
        self.assertIn("element_gone", KINDS)
        for kind in ("window_appear", "window_gone", "title_match", "element_appear", "text_appear"):
            self.assertIn(kind, KINDS)


# ------------------------------------------------------------------ 命中消失


class TestElementGoneMet(unittest.TestCase):
    def test_met_when_element_leaves_the_tree(self):
        """等元素消失：本轮读到的树里找不到它，条件即成立。"""
        before = window_state([el(0, "Document", "报表"), el(2, "ProgressBar", "正在保存")])
        after = window_state([el(0, "Document", "报表")])
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[before, after])

        out, clock = run_wait(
            driver,
            WaitSpec(
                kind="element_gone",
                pid=100,
                element_index=2,
                snapshot_id="s00000001",
                timeout_seconds=5,
                interval_seconds=0.5,
            ),
        )

        self.assertTrue(out["condition_met"])
        self.assertIsNone(out["code"])
        self.assertEqual(out["polls"], 2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], "element_gone")
        self.assertEqual(clock.slept, [0.5])
        self.assertFalse(out["final"]["found"])
        self.assertIsNone(out["final"]["element"])
        self.assertEqual(out["final"]["elements_count"], 1)
        self.assertEqual(out["final"]["address"]["by"], "element_index")

    def test_met_immediately_when_element_already_gone(self):
        driver = ScriptedDriver(
            windows_script=[[win()]], states_script=[window_state([el(0, "Document", "报表")])]
        )
        out, clock = run_wait(
            driver,
            WaitSpec(kind="element_gone", pid=100, element_index=2, snapshot_id="s00000001"),
        )

        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 1)
        self.assertEqual(clock.slept, [])
        self.assertEqual(
            driver.called_with("get_window_state")[0],
            {"pid": 100, "window_id": 200, "include_screenshot": False},
        )

    def test_met_on_window_disappearing_too(self):
        """窗口自己没了 → 里面的元素当然也没了，条件成立。

        与 `element_appear`（窗口没出现 = 元素还没出现）刻意相反：两个条件的目标状态
        互为反面。回执里给 reason，免得调用方以为"元素精确地消失于某个仍在的窗口里"。
        """
        driver = ScriptedDriver(
            windows_script=[[win()], []],
            states_script=[window_state([el(2, "ProgressBar", "正在保存")])],
        )
        out, _ = run_wait(
            driver,
            WaitSpec(
                kind="element_gone",
                pid=100,
                element_index=2,
                snapshot_id="s00000001",
                timeout_seconds=5,
                interval_seconds=0.5,
            ),
        )

        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 2)
        self.assertIsNone(out["final"]["target"])
        self.assertIn("窗口", out["final"]["reason"])
        # 窗口不存在的那一轮**不去读状态**：没有窗口可读，多一次驱动往返纯属浪费
        self.assertEqual(len(driver.called_with("get_window_state")), 1)


# ------------------------------------------------------------------ 超时是结果不是异常


class TestElementGoneTimeout(unittest.TestCase):
    def test_timeout_is_a_result_not_an_exception(self):
        """没等到是**结果**：ok=true + condition_met=false + code=WAIT_TIMEOUT（与既有条件同口径）。"""
        driver = ScriptedDriver(
            windows_script=[[win()]], states_script=[window_state([el(2, "ProgressBar", "正在保存")])]
        )
        out, clock = run_wait(
            driver,
            WaitSpec(
                kind="element_gone",
                pid=100,
                element_index=2,
                snapshot_id="s00000001",
                timeout_seconds=2,
                interval_seconds=0.5,
            ),
        )

        self.assertFalse(out["condition_met"])
        self.assertTrue(out["ok"])  # 等待这次调用本身是成功的
        self.assertEqual(out["code"], WAIT_TIMEOUT)
        self.assertEqual(out["polls"], 5)  # t=0,0.5,1,1.5,2.0 → deadline 收口
        self.assertEqual(len(clock.slept), 4)
        # 仍在的元素要带摘要回报，方便调用方看清"等的是谁、它现在什么样"
        self.assertTrue(out["final"]["found"])
        self.assertEqual(out["final"]["element"]["index"], 2)
        self.assertEqual(out["final"]["element"]["role"], "ProgressBar")

    def test_disabled_but_present_element_is_not_gone(self):
        """"还在但被禁用"不等于"消失"——消失看的是**在不在树里**，不是能不能点。"""
        driver = ScriptedDriver(
            windows_script=[[win()]],
            states_script=[window_state([el(2, "Button", "保存", enabled=False)])],
        )
        out, _ = run_wait(
            driver,
            WaitSpec(
                kind="element_gone",
                pid=100,
                element_index=2,
                snapshot_id="s00000001",
                timeout_seconds=1,
                interval_seconds=0.5,
            ),
        )
        self.assertFalse(out["condition_met"])
        self.assertEqual(out["code"], WAIT_TIMEOUT)

    def test_zero_timeout_still_probes_once(self):
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[window_state([])])
        out, _ = run_wait(
            driver,
            WaitSpec(kind="element_gone", pid=100, element_index=2, snapshot_id="s00000001", timeout_seconds=0),
        )
        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 1)


# ------------------------------------------------------------------ 参数不足：核心自己先报错


class TestElementGoneParamErrors(unittest.TestCase):
    def test_no_addressing_is_a_core_param_error_before_touching_driver(self):
        """没给任何寻址方式 → 当场报核心的参数错，且**一次驱动都不调**。"""
        driver = ScriptedDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_wait(driver, WaitSpec(kind="element_gone", pid=100))

        self.assertEqual(ctx.exception.code, "MISSING_TARGET")
        self.assertEqual(driver.calls, [])
        self.assertIn("element_index", ctx.exception.detail)
        self.assertIn("element_token", ctx.exception.detail)
        self.assertIn("pattern", ctx.exception.detail)

    def test_index_without_snapshot_is_rejected(self):
        """索引只在与它同源的快照内有效（与 act.py 同一条不变量）→ 缺快照直接拒。"""
        driver = ScriptedDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_wait(driver, WaitSpec(kind="element_gone", pid=100, element_index=2))

        self.assertEqual(ctx.exception.code, "SNAPSHOT_REQUIRED")
        self.assertEqual(driver.calls, [])

    def test_blank_pattern_is_not_addressing(self):
        driver = ScriptedDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_wait(driver, WaitSpec(kind="element_gone", pid=100, pattern="   "))
        self.assertEqual(ctx.exception.code, "MISSING_TARGET")
        self.assertEqual(driver.calls, [])

    def test_malformed_token_is_rejected_not_treated_as_gone(self):
        """认不出的令牌 ≠ 元素消失。fail-closed：宁可报参数错，也不回一个假的"没了"。"""
        driver = ScriptedDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_wait(driver, WaitSpec(kind="element_gone", pid=100, element_token="坏令牌"))

        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(driver.calls, [])

    def test_non_integer_index_is_rejected(self):
        driver = ScriptedDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_wait(
                driver,
                WaitSpec(kind="element_gone", pid=100, element_index="2", snapshot_id="s00000001"),
            )
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(driver.calls, [])

    def test_timeout_over_limit_is_rejected(self):
        with self.assertRaises(WinUseError) as ctx:
            run_wait(
                ScriptedDriver(),
                WaitSpec(
                    kind="element_gone",
                    pid=100,
                    element_index=1,
                    snapshot_id="s00000001",
                    timeout_seconds=wait_mod.limit_mod.MAX_WAIT_SECONDS + 1,
                ),
            )
        self.assertEqual(ctx.exception.code, "PARAM_OUT_OF_RANGE")


# ------------------------------------------------------------------ 三种寻址方式


class TestElementGoneAddressing(unittest.TestCase):
    def test_by_token_survives_snapshot_rotation(self):
        """令牌寻址：驱动换快照后前缀翻新，但元素**还在** → 绝不能误判成消失。

        这是本条件最容易出的假阳性：令牌前缀是快照号，两轮读必然不同。若只比令牌字面，
        每个元素都会在第一轮就"消失"，这个条件会变成永远立即命中——等于没等。
        """
        same_tree_new_snapshot = window_state(
            [el(0, "Document", "报表", snapshot="s00000039"), el(2, "ProgressBar", "正在保存", snapshot="s00000039")],
            snapshot_id="s00000039",
        )
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[same_tree_new_snapshot])

        out, _ = run_wait(
            driver,
            WaitSpec(
                kind="element_gone",
                pid=100,
                element_token="s00000001:2",  # 旧快照里的令牌
                timeout_seconds=1,
                interval_seconds=0.5,
            ),
        )
        self.assertFalse(out["condition_met"])
        self.assertEqual(out["code"], WAIT_TIMEOUT)
        self.assertTrue(out["final"]["found"])

    def test_by_token_detects_real_disappearance_across_snapshots(self):
        after = window_state([el(0, "Document", "报表", snapshot="s00000039")], snapshot_id="s00000039")
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[after])

        out, _ = run_wait(driver, WaitSpec(kind="element_gone", pid=100, element_token="s00000001:2"))

        self.assertTrue(out["condition_met"])
        self.assertEqual(out["final"]["address"]["by"], "element_token")

    def test_by_text_substring(self):
        """文本寻址：label 或 value 里都不再含这个子串 = 消失（不区分大小写）。"""
        before = window_state([el(0, "Document", "报表"), el(3, "StatusBar", value="正在保存…")])
        after = window_state([el(0, "Document", "报表"), el(3, "StatusBar", value="就绪")])
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[before, after])

        out, clock = run_wait(
            driver,
            WaitSpec(
                kind="element_gone",
                pid=100,
                pattern="正在保存",
                timeout_seconds=3,
                interval_seconds=0.5,
            ),
        )

        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 2)
        self.assertEqual(clock.slept, [0.5])
        self.assertEqual(out["final"]["address"]["by"], "text")

    def test_text_match_is_case_insensitive_and_reads_value_too(self):
        driver = ScriptedDriver(
            windows_script=[[win()]],
            states_script=[window_state([el(5, "StatusBar", label="SAVING", value="Saving 12%")])],
        )
        out, _ = run_wait(
            driver,
            WaitSpec(kind="element_gone", pid=100, pattern="saving", timeout_seconds=1, interval_seconds=0.5),
        )
        self.assertFalse(out["condition_met"])  # 命中 → 还在 → 没等到

    def test_text_addressing_does_not_match_other_elements(self):
        """别的元素还带着这个名字，就不算消失。"""
        driver = ScriptedDriver(
            windows_script=[[win()]],
            states_script=[window_state([el(0, "Button", "保存"), el(1, "MenuItem", "保存")])],
        )
        out, _ = run_wait(
            driver,
            WaitSpec(kind="element_gone", pid=100, pattern="保存", timeout_seconds=1, interval_seconds=0.5),
        )
        self.assertFalse(out["condition_met"])

    def test_token_wins_over_index_when_both_given(self):
        """同时给了令牌与索引时按令牌（与 act.py 的优先级一致），并在回执里说明。"""
        driver = ScriptedDriver(
            windows_script=[[win()]],
            states_script=[window_state([el(2, "ProgressBar", "正在保存")])],
        )
        out, _ = run_wait(
            driver,
            WaitSpec(
                kind="element_gone",
                pid=100,
                element_token="s00000001:2",
                element_index=9,  # 树里没有 9 → 若按索引判会立刻误报"消失"
                snapshot_id="s00000001",
                timeout_seconds=1,
                interval_seconds=0.5,
            ),
        )
        self.assertFalse(out["condition_met"])
        self.assertEqual(out["final"]["address"]["by"], "element_token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
