"""R-8：每个 Win32 函数的返回值**语义不同**，不能统一按"真/假=成功/失败"处理。

报告 R-8：`_run_calls()` 对所有调用统一做 `if not ret: last_error()`，但
`ShowWindow` 返回的是**调用前窗口是否可见**——首次最小化一个可见窗口时它返回
`True`，恢复时可能返回 `False`，两种都不是"调用失败"。同时顶层 `ok` 仍是 `true`，
真正的失败混在里面读不出来。

本文件钉住四类语义：
| 函数 | 返回值含义 |
|---|---|
| ShowWindow | 调用前的可见性（**不是**成败） |
| SetWindowPos / SetForegroundWindow | BOOL 成败 |
| PostMessageW | 是否投递成功（**不代表窗口已关闭**） |
| GetWindowLongPtrW | 返回值本身就是数据 |

不碰真实桌面：全部走桩原生对象。
"""

from __future__ import annotations

import unittest

from winuse.core.desktop import (
    WM_CLOSE,
    WindowActionSpec,
    window_action,
)


WINDOW = {
    "pid": 100,
    "window_id": 2001,
    "app_name": "demo.exe",
    "title": "占位窗口",
    "minimized": False,
    "is_on_screen": True,
    "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
    "z_index": 1,
}


class StubDriver:
    def call(self, tool, args=None):
        if tool == "list_windows":
            return {"windows": [WINDOW]}
        return {"ok": True}


class StubNative:
    """按需给每个函数设定返回值；记录调用。"""

    def __init__(self, show_window=True, set_window_pos=True, post_message=True,
                 set_foreground_window=True):
        self.returns = {
            "show_window": show_window,
            "set_window_pos": set_window_pos,
            "post_message": post_message,
            "set_foreground_window": set_foreground_window,
        }
        self.calls = []

    def __getattr__(self, name):
        if name in ("returns", "calls"):
            raise AttributeError(name)

        def recorder(*args):
            self.calls.append((name, list(args)))
            return self.returns.get(name, True)

        return recorder

    def last_error(self):
        return 1400

    def is_topmost(self, _hwnd):
        return None


def _run(spec, native):
    return window_action(StubDriver(), spec, execute=True, native=native, confirmed=True)


class TestShowWindowSemantics(unittest.TestCase):
    def test_false_is_reported_as_previously_invisible_not_failure(self):
        """ShowWindow 返回 False = 调用前不可见，属**正常**（不是失败）。"""
        native = StubNative(show_window=False)
        out = _run(WindowActionSpec(action="minimize", window_id=2001), native)
        item = out["executed"][0]
        self.assertTrue(out["ok"], "ShowWindow 返回 False 不该被判成调用失败")
        self.assertEqual(item["semantics"], "previously_visible")
        self.assertIs(item["previously_visible"], False)
        self.assertNotIn("last_error", item, "合法返回值不该带 last_error")

    def test_true_also_normal(self):
        native = StubNative(show_window=True)
        out = _run(WindowActionSpec(action="minimize", window_id=2001), native)
        self.assertIs(out["executed"][0]["previously_visible"], True)
        self.assertTrue(out["ok"])


class TestBoolSuccessSemantics(unittest.TestCase):
    def test_set_window_pos_zero_is_a_failure(self):
        native = StubNative(set_window_pos=0)
        out = _run(
            WindowActionSpec(action="move_resize", window_id=2001, x=10, y=10, width=100, height=100),
            native,
        )
        self.assertFalse(out["ok"], "BOOL 类调用返回 0 必须让顶层 ok 变 false")
        self.assertEqual(out["code"], "WINDOW_CALL_FAILED")
        item = out["executed"][0]
        self.assertEqual(item["semantics"], "bool_success")
        self.assertIs(item["succeeded"], False)
        self.assertEqual(item["last_error"], 1400)

    def test_set_window_pos_nonzero_is_success(self):
        native = StubNative(set_window_pos=1)
        out = _run(
            WindowActionSpec(action="move_resize", window_id=2001, x=10, y=10, width=100, height=100),
            native,
        )
        self.assertTrue(out["ok"])
        self.assertIs(out["executed"][0]["succeeded"], True)


class TestPostMessageSemantics(unittest.TestCase):
    def test_post_is_delivery_not_closure(self):
        """PostMessageW 成功只表示消息投递成功——回执不许暗示窗口已关。"""
        native = StubNative(post_message=1)
        out = _run(WindowActionSpec(action="close", window_id=2001), native)
        item = out["executed"][0]
        self.assertTrue(out["ok"])
        self.assertEqual(item["semantics"], "posted")
        self.assertIs(item["posted"], True)
        self.assertNotIn("closed", str(out))

    def test_post_zero_is_failure(self):
        native = StubNative(post_message=0)
        out = _run(WindowActionSpec(action="close", window_id=2001), native)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "WINDOW_CALL_FAILED")


class TestMixedCalls(unittest.TestCase):
    """最小化窗口的 activate 会下发两条（先 restore 再置前台），逐条语义各判各的。"""

    class MinimizedDriver:
        def call(self, tool, args=None):
            if tool == "list_windows":
                return {"windows": [dict(WINDOW, minimized=True)]}
            return {"ok": True}

    def _run_minimized(self, native):
        return window_action(
            self.MinimizedDriver(),
            WindowActionSpec(action="activate", window_id=2001),
            execute=True,
            native=native,
            confirmed=True,
        )

    def test_one_failed_call_makes_the_whole_action_not_ok(self):
        native = StubNative(show_window=True, set_foreground_window=0)
        out = self._run_minimized(native)
        self.assertEqual([i["func"] for i in out["executed"]],
                         ["ShowWindow", "SetForegroundWindow"])
        self.assertFalse(out["ok"], "其中一条 BOOL 调用失败 → 整体不能报成功")
        self.assertEqual(out["code"], "WINDOW_CALL_FAILED")
        failed = [i for i in out["executed"] if i.get("succeeded") is False]
        self.assertEqual([i["func"] for i in failed], ["SetForegroundWindow"])

    def test_show_window_false_does_not_drag_the_action_down(self):
        native = StubNative(show_window=False, set_foreground_window=1)
        out = self._run_minimized(native)
        self.assertTrue(out["ok"], "ShowWindow 的 false 是「调用前不可见」，不该算失败")
        self.assertIs(out["executed"][0]["previously_visible"], False)

    def test_error_carries_the_failed_calls(self):
        native = StubNative(set_foreground_window=0)
        out = self._run_minimized(native)
        self.assertIn("SetForegroundWindow", str(out["error"]["message"]))
        self.assertEqual(out["executed"][0]["func"], "ShowWindow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
