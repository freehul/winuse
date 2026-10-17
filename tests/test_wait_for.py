"""wait_for 单测：条件立即满足 / 第 N 次轮询满足 / 超时三条主路径。

不接触真实桌面与真实时钟：
- 局部 ScriptedDriver 按脚本回 `list_windows` / `get_window_state`
  （不动 tests/fake_driver.py，那是 CLI 层的假驱动）
- 注入 FakeClock，`sleep` 即时返回 → 整份用例真实耗时远小于一秒
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.wait_for import (  # noqa: E402
    WAIT_TIMEOUT,
    UnknownWaitKind,
    WaitSpec,
    wait_for,
)
from winuse.driver.errors import DriverError, WinUseError  # noqa: E402


# ------------------------------------------------------------------ 构造器


def win(pid=100, wid=200, title="Demo", app="Demo.exe", z: int | None = 1, minimized=False, on_screen=True):
    """造一个 list_windows 记录（字段与驱动实测一致）。"""
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


def el(i, role, label=None, value=None, actions=None, enabled=True):
    """造一个 get_window_state 元素（字段与驱动实测一致）。"""
    out = {
        "actions": list(actions) if actions else [],
        "depth": 1,
        "element_index": i,
        "element_token": f"s00000001:{i}",
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


# ------------------------------------------------------------------ 窗口类条件


class TestWindowKinds(unittest.TestCase):
    def test_window_appear_met_on_first_poll(self):
        driver = ScriptedDriver(windows_script=[[win(title="记事本")]])
        out, clock = run_wait(driver, WaitSpec(kind="window_appear", app="Demo.exe"))

        self.assertTrue(out["condition_met"])
        self.assertIsNone(out["code"])
        self.assertEqual(out["polls"], 1)
        self.assertEqual(out["waited_ms"], 0)
        self.assertEqual(out["kind"], "window_appear")
        self.assertTrue(out["ok"])
        self.assertEqual(out["final"]["matched"], 1)
        self.assertEqual(out["final"]["window"]["title"], "记事本")
        # 一次都没睡：条件首轮就满足
        self.assertEqual(clock.slept, [])

    def test_window_appear_met_on_third_poll(self):
        driver = ScriptedDriver(windows_script=[[], [], [win()]])
        out, clock = run_wait(
            driver, WaitSpec(kind="window_appear", pid=100, timeout_seconds=5, interval_seconds=0.5)
        )

        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 3)
        self.assertEqual(out["waited_ms"], 1000)  # 两次 0.5s 轮询间隔
        self.assertEqual(clock.slept, [0.5, 0.5])
        self.assertIsNone(out["code"])

    def test_timeout_is_a_result_not_an_exception(self):
        """等不到是「结果」：condition_met=False + WAIT_TIMEOUT，绝不抛。"""
        driver = ScriptedDriver(windows_script=[[]])
        out, clock = run_wait(
            driver, WaitSpec(kind="window_appear", app="Demo.exe", timeout_seconds=2, interval_seconds=0.5)
        )

        self.assertFalse(out["condition_met"])
        self.assertEqual(out["code"], WAIT_TIMEOUT)
        self.assertTrue(out["ok"])  # 调用本身成功，没等到是条件问题
        self.assertEqual(out["polls"], 5)  # t=0,0.5,1,1.5,2.0 → deadline 收口
        self.assertEqual(len(clock.slept), 4)
        self.assertEqual(out["final"]["matched"], 0)

    def test_zero_timeout_still_probes_once(self):
        driver = ScriptedDriver(windows_script=[[win()]])
        out, _ = run_wait(driver, WaitSpec(kind="window_appear", app="Demo.exe", timeout_seconds=0))
        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 1)

    def test_window_gone(self):
        driver = ScriptedDriver(windows_script=[[win()], []])
        out, _ = run_wait(
            driver, WaitSpec(kind="window_gone", pid=100, timeout_seconds=5, interval_seconds=0.5)
        )
        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 2)
        self.assertEqual(out["final"]["selected"], 0)

    def test_window_gone_requires_selector(self):
        with self.assertRaises(WinUseError) as ctx:
            run_wait(ScriptedDriver(), WaitSpec(kind="window_gone"))
        self.assertEqual(ctx.exception.code, "MISSING_TARGET")

    def test_title_match_is_case_insensitive_substring(self):
        driver = ScriptedDriver(windows_script=[[win(title="Untitled - Notepad")]])
        out, _ = run_wait(driver, WaitSpec(kind="title_match", pattern="notepad"))
        self.assertTrue(out["condition_met"])
        self.assertEqual(out["final"]["window"]["window_id"], 200)

    def test_title_match_ignores_non_matching_titles(self):
        driver = ScriptedDriver(
            windows_script=[[win(wid=1, title="别的窗口")], [win(wid=2, title="保存成功")]],
        )
        out, _ = run_wait(
            driver,
            WaitSpec(kind="title_match", pattern="保存成功", timeout_seconds=3, interval_seconds=0.5),
        )
        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 2)
        self.assertEqual(out["final"]["window"]["window_id"], 2)

    def test_title_match_requires_pattern(self):
        with self.assertRaises(WinUseError) as ctx:
            run_wait(ScriptedDriver(), WaitSpec(kind="title_match", app="Demo.exe"))
        self.assertEqual(ctx.exception.code, "MISSING_PARAM")

    def test_window_appear_requires_selector_or_pattern(self):
        with self.assertRaises(WinUseError) as ctx:
            run_wait(ScriptedDriver(), WaitSpec(kind="window_appear"))
        self.assertEqual(ctx.exception.code, "MISSING_TARGET")

    def test_selector_narrows_candidates(self):
        """给了 pid 就只在这个进程的窗口里找，别的窗口带同样标题也不算。"""
        other = win(pid=999, wid=301, title="保存成功")
        target = win(pid=100, wid=200, title="别的标题")
        driver = ScriptedDriver(windows_script=[[other, target], [other, win(pid=100, wid=200, title="保存成功")]])
        out, _ = run_wait(
            driver, WaitSpec(kind="title_match", pid=100, pattern="保存成功", interval_seconds=0.5)
        )
        self.assertEqual(out["polls"], 2)
        self.assertEqual(out["final"]["selected"], 1)
        self.assertTrue(out["condition_met"])

    def test_unknown_kind_raises(self):
        with self.assertRaises(UnknownWaitKind) as ctx:
            run_wait(ScriptedDriver(), WaitSpec(kind="window_dance"))
        self.assertEqual(ctx.exception.code, "UNKNOWN_WAIT_KIND")


# ------------------------------------------------------------------ 元素/文本类条件


class TestElementKinds(unittest.TestCase):
    def test_element_appear_met_immediately(self):
        state = window_state([el(1, "Button", "确定", actions=["invoke"])])
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[state])
        out, _ = run_wait(driver, WaitSpec(kind="element_appear", pid=100, pattern="确定"))

        self.assertTrue(out["condition_met"])
        self.assertEqual(out["final"]["element"]["index"], 1)
        self.assertEqual(out["final"]["element"]["role"], "Button")
        self.assertEqual(out["final"]["snapshot_id"], "s00000001")
        # 取状态一律纯文本：不带截图
        self.assertEqual(
            driver.called_with("get_window_state")[0],
            {"pid": 100, "window_id": 200, "include_screenshot": False},
        )

    def test_element_appear_ignores_disabled_elements(self):
        state = window_state([el(7, "Button", "确定", actions=["invoke"], enabled=False)])
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[state])
        out, _ = run_wait(
            driver,
            WaitSpec(kind="element_appear", pid=100, pattern="确定", timeout_seconds=1, interval_seconds=0.5),
        )
        self.assertFalse(out["condition_met"])
        self.assertEqual(out["code"], WAIT_TIMEOUT)

    def test_element_appear_matches_role(self):
        state = window_state([el(3, "ListItem", "第二项", actions=["select"])])
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[state])
        out, _ = run_wait(driver, WaitSpec(kind="element_appear", pid=100, pattern="listitem"))
        self.assertTrue(out["condition_met"])

    def test_text_appear_met_on_later_poll(self):
        before = window_state([el(0, "Document", "空文档")])
        after = window_state([el(0, "Document", "已保存：报表.csv")])
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[before, after])
        out, clock = run_wait(
            driver, WaitSpec(kind="text_appear", pid=100, pattern="已保存", interval_seconds=0.5)
        )

        self.assertTrue(out["condition_met"])
        self.assertEqual(out["polls"], 2)
        self.assertEqual(clock.slept, [0.5])
        self.assertEqual(out["final"]["element"]["label"], "已保存：报表.csv")

    def test_text_appear_reads_value_field_too(self):
        state = window_state([el(2, "Edit", "搜索框", value="关键词已填入", actions=["set_value"])])
        driver = ScriptedDriver(windows_script=[[win()]], states_script=[state])
        out, _ = run_wait(driver, WaitSpec(kind="text_appear", pid=100, pattern="关键词已填入"))
        self.assertTrue(out["condition_met"])

    def test_without_selector_falls_back_to_default_pick(self):
        """不给选取条件时沿用 pick_window 的默认判定（这里 z_index 可用，取最前）。"""
        windows = [win(pid=1, wid=11, title="A", z=1), win(pid=2, wid=22, title="B", z=9)]
        driver = ScriptedDriver(
            windows_script=[windows], states_script=[window_state([el(5, "Button", "完成")], pid=2, wid=22)]
        )
        out, _ = run_wait(driver, WaitSpec(kind="text_appear", pattern="完成"))

        self.assertTrue(out["condition_met"])
        self.assertEqual(out["final"]["target"]["window_id"], 22)
        self.assertEqual(driver.called_with("get_window_state")[0]["window_id"], 22)

    def test_window_not_found_keeps_polling_then_times_out(self):
        driver = ScriptedDriver(windows_script=[[]], states_script=[window_state([])])
        out, _ = run_wait(
            driver, WaitSpec(kind="element_appear", pid=100, pattern="确定", timeout_seconds=1, interval_seconds=0.5)
        )
        self.assertFalse(out["condition_met"])
        self.assertEqual(out["code"], WAIT_TIMEOUT)
        self.assertIsNone(out["final"]["target"])
        self.assertEqual(driver.called_with("get_window_state"), [])  # 没有窗口就不去取状态

    def test_ambiguous_selector_is_raised_not_swallowed(self):
        """z_index 不可用且可见窗口不唯一：这是参数问题，等下去也不会变清楚，必须抛。"""
        windows = [
            win(pid=1, wid=11, title="A", z=None),
            win(pid=2, wid=22, title="B", z=None),
        ]
        driver = ScriptedDriver(windows_script=[windows])
        with self.assertRaises(WinUseError) as ctx:
            run_wait(driver, WaitSpec(kind="text_appear", pattern="完成"))
        self.assertEqual(ctx.exception.code, "WINDOW_AMBIGUOUS")


# ------------------------------------------------------------------ 驱动侧异常与时钟注入


class TestDriverErrorsAndClock(unittest.TestCase):
    def test_driver_error_recorded_in_final(self):
        driver = ScriptedDriver(windows_script=[DriverError("驱动调用失败：模拟", "退出码 2")])
        out, _ = run_wait(
            driver, WaitSpec(kind="window_appear", app="Demo.exe", timeout_seconds=1, interval_seconds=0.5)
        )

        self.assertFalse(out["condition_met"])
        self.assertEqual(out["code"], WAIT_TIMEOUT)
        self.assertEqual(out["final"]["last_error"]["code"], "DRIVER_ERROR")
        self.assertEqual(out["polls"], 3)

    def test_injected_clock_means_no_real_waiting(self):
        """5 秒超时 + 0.5 秒间隔，真实耗时必须远小于 1 秒（sleep 被注入替换）。"""
        driver = ScriptedDriver(windows_script=[[]])
        clock = FakeClock()
        started = time.monotonic()
        out = wait_for(
            driver,
            WaitSpec(kind="window_appear", app="Demo.exe", timeout_seconds=5, interval_seconds=0.5),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        real_elapsed = time.monotonic() - started

        self.assertFalse(out["condition_met"])
        self.assertEqual(out["waited_ms"], 5000)  # 假时钟里确实等了 5 秒
        self.assertEqual(len(clock.slept), 10)
        self.assertLess(real_elapsed, 1.0)  # 真实世界几乎不花时间

    def test_default_clock_is_real(self):
        """不注入时必须用真实现——否则生产路径会退化成忙等。"""
        driver = ScriptedDriver(windows_script=[[win()]])
        started = time.monotonic()
        out = wait_for(driver, WaitSpec(kind="window_appear", app="Demo.exe"))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(out["condition_met"])
        self.assertEqual(out["waited_ms"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
