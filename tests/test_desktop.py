"""winuse_desktop 单测：窗口动作的映射/干跑/执行，以及虚拟桌面探测的分支。

铁律：**单测不碰真实桌面**。动作类用例全部注入桩：
- `StubDriver`：只回 `list_windows` 的假窗口清单（字段照抄驱动实测形态）
- `RecordingNative`：记录每一次"原生调用"并逐字段断言参数
- `ExplodingNative`：干跑用例专用，任何一次原生调用都会让用例失败
真机只读用例单独放在文件末尾，用 `pytest.skip` / `unittest.SkipTest` 双重保护，
且只读句柄与标题（不动任何窗口状态）。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.desktop import (  # noqa: E402
    GWL_EXSTYLE,
    HWND_NOTOPMOST,
    HWND_TOPMOST,
    SW_MAXIMIZE,
    SW_MINIMIZE,
    SW_RESTORE,
    SWP_NOACTIVATE,
    SWP_NOZORDER,
    WM_CLOSE,
    BadParam,
    NativeUser32,
    VirtualDesktopBackend,
    WindowActionSpec,
    build_window_calls,
    interpret_desktop_query,
    virtual_desktop_info,
    window_action,
)
from winuse.driver.errors import (  # noqa: E402
    DriverError,
    MissingParam,
    UnknownAction,
    WindowMinimized,
    WindowNotFound,
    WinUseError,
)
from winuse.core import desktop as desktop_mod  # noqa: E402


def win(pid, wid, title, app="App.exe", minimized=False, z=1, on_screen=True):
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


WINDOWS = [
    win(100, 2001, "记事本 - 无标题", app="notepad.exe", z=3),
    win(101, 2002, "Demo - 浏览器", app="browser.exe", z=2),
    win(102, 2003, "已最小化的窗口", app="mail.exe", minimized=True, z=1),
]


class StubDriver:
    """假的驱动：只答 list_windows；可注入错误以验证错误归一化。"""

    def __init__(self, windows=None, error=None):
        self.windows = WINDOWS if windows is None else windows
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        if self.error is not None:
            raise self.error
        assert tool == "list_windows", f"本模块只应调 list_windows，收到 {tool}"
        return {"windows": self.windows}


class RecordingNative:
    """桩：记录每一次原生调用，并可模拟"系统拒绝"的返回值。"""

    def __init__(self, topmost=False, result=True):
        self.calls: list[tuple[str, list]] = []
        self.topmost = topmost
        self.result = result

    def _log(self, name, args):
        self.calls.append((name, list(args)))

    def last_error(self):
        return 1400  # ERROR_INVALID_WINDOW_HANDLE（模拟）

    def show_window(self, hwnd, cmd):
        self._log("show_window", [hwnd, cmd])
        return self.result

    def set_window_pos(self, hwnd, insert_after, x, y, cx, cy, flags):
        self._log("set_window_pos", [hwnd, insert_after, x, y, cx, cy, flags])
        if insert_after == HWND_TOPMOST:
            self.topmost = True
        elif insert_after == HWND_NOTOPMOST:
            self.topmost = False
        return self.result

    def set_foreground_window(self, hwnd):
        self._log("set_foreground_window", [hwnd])
        return self.result

    def post_message(self, hwnd, msg, wparam, lparam):
        self._log("post_message", [hwnd, msg, wparam, lparam])
        return self.result

    def get_window_long_ptr(self, hwnd, index):
        self._log("get_window_long_ptr", [hwnd, index])
        return desktop_mod.WS_EX_TOPMOST if self.topmost else 0

    def set_window_long_ptr(self, hwnd, index, value):
        self._log("set_window_long_ptr", [hwnd, index, value])
        return self.result

    def is_topmost(self, hwnd):
        self._log("is_topmost", [hwnd])
        return self.topmost


class ExplodingNative:
    """干跑用例专用：任何一次属性访问/调用都直接失败。"""

    def __getattr__(self, name):
        raise AssertionError(f"干跑不应触碰原生接口：{name}")


# --------------------------------------------------------------------------
# 纯函数：动作 → user32 调用清单
# --------------------------------------------------------------------------


class TestBuildWindowCalls(unittest.TestCase):
    def target(self, wid=2001, minimized=False):
        return {"pid": 100, "window_id": wid, "title": "T", "minimized": minimized}

    def test_show_window_actions_map_to_their_sw_command(self):
        cases = {"minimize": SW_MINIMIZE, "maximize": SW_MAXIMIZE, "restore": SW_RESTORE}
        for action, cmd in cases.items():
            with self.subTest(action=action):
                calls = build_window_calls(WindowActionSpec(action=action), self.target())
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["func"], "ShowWindow")
                self.assertEqual(calls[0]["args"], [2001, cmd])

    def test_close_posts_wm_close_not_destroy(self):
        calls = build_window_calls(WindowActionSpec(action="close"), self.target())
        self.assertEqual(calls[0]["func"], "PostMessageW")
        self.assertEqual(calls[0]["args"], [2001, WM_CLOSE, 0, 0])

    def test_activate_normal_window_does_not_restore(self):
        calls = build_window_calls(WindowActionSpec(action="activate"), self.target())
        self.assertEqual([c["func"] for c in calls], ["SetForegroundWindow"])

    def test_activate_minimized_window_restores_first(self):
        # 顺序反了这一步会静默失效：最小化窗口拿不到前台
        calls = build_window_calls(
            WindowActionSpec(action="activate"), self.target(minimized=True)
        )
        self.assertEqual([c["func"] for c in calls], ["ShowWindow", "SetForegroundWindow"])
        self.assertEqual(calls[0]["args"], [2001, SW_RESTORE])

    def test_always_on_top_true_uses_hwnd_topmost(self):
        calls = build_window_calls(
            WindowActionSpec(action="always_on_top", always_on_top=True), self.target()
        )
        args = calls[0]["args"]
        self.assertEqual(calls[0]["func"], "SetWindowPos")
        self.assertEqual(args[1], HWND_TOPMOST)
        self.assertEqual(args[0], 2001)

    def test_always_on_top_false_uses_hwnd_notopmost(self):
        calls = build_window_calls(
            WindowActionSpec(action="always_on_top", always_on_top=False), self.target()
        )
        self.assertEqual(calls[0]["args"][1], HWND_NOTOPMOST)

    def test_move_resize_keeps_zorder_and_focus_untouched(self):
        spec = WindowActionSpec(action="move_resize", x=10, y=20, width=800, height=600)
        calls = build_window_calls(spec, self.target())
        args = calls[0]["args"]
        self.assertEqual(calls[0]["func"], "SetWindowPos")
        self.assertEqual(args[0], 2001)
        self.assertEqual(args[2:6], [10, 20, 800, 600])
        # 不许顺带改 z 序或抢前台
        self.assertTrue(args[6] & SWP_NOZORDER)
        self.assertTrue(args[6] & SWP_NOACTIVATE)

    def test_negative_coordinates_are_legal(self):
        # 左竖屏原点是负值（真机 -2160,-890），不是非法参数
        spec = WindowActionSpec(action="move_resize", x=-2160, y=-890, width=1440, height=2560)
        args = build_window_calls(spec, self.target())[0]["args"]
        self.assertEqual(args[2:6], [-2160, -890, 1440, 2560])

    def test_every_call_has_one_label_per_positional_arg(self):
        specs = [
            WindowActionSpec(action="minimize"),
            WindowActionSpec(action="maximize"),
            WindowActionSpec(action="restore"),
            WindowActionSpec(action="close"),
            WindowActionSpec(action="activate"),
            WindowActionSpec(action="always_on_top", always_on_top=True),
            WindowActionSpec(action="move_resize", x=1, y=2, width=3, height=4),
        ]
        for spec in specs:
            with self.subTest(action=spec.action):
                for call in build_window_calls(spec, self.target()):
                    self.assertEqual(len(call["args"]), len(call["labels"]))
                    self.assertTrue(all(isinstance(x, str) for x in call["labels"]))


# --------------------------------------------------------------------------
# 纯函数：参数校验的错误码契约
# --------------------------------------------------------------------------


class TestValidationErrors(unittest.TestCase):
    def target(self, minimized=False, wid=2001):
        return {"pid": 100, "window_id": wid, "title": "T", "minimized": minimized}

    def assert_code(self, spec, code, exc_type=WinUseError, **kw):
        with self.assertRaises(exc_type) as cm:
            build_window_calls(spec, self.target(**kw))
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    def test_unknown_action(self):
        self.assert_code(WindowActionSpec(action="teleport"), "UNKNOWN_ACTION", UnknownAction)

    def test_move_resize_missing_all_coords(self):
        exc = self.assert_code(
            WindowActionSpec(action="move_resize"), "MISSING_PARAM", MissingParam
        )
        for name in ("x", "y", "width", "height"):
            self.assertIn(name, exc.message)

    def test_move_resize_missing_some_coords_names_them(self):
        spec = WindowActionSpec(action="move_resize", x=1, y=2, width=3)
        exc = self.assert_code(spec, "MISSING_PARAM", MissingParam)
        self.assertIn("height", exc.message)
        self.assertNotIn("width", exc.message.split("：")[-1])

    def test_zero_width_is_bad_param(self):
        spec = WindowActionSpec(action="move_resize", x=0, y=0, width=0, height=600)
        self.assert_code(spec, "BAD_PARAM", BadParam)

    def test_negative_height_is_bad_param(self):
        spec = WindowActionSpec(action="move_resize", x=0, y=0, width=800, height=-5)
        self.assert_code(spec, "BAD_PARAM", BadParam)

    def test_sentinel_coordinate_is_rejected(self):
        # -32000 是最小化哨兵值，不是"挪到很远的地方"
        spec = WindowActionSpec(action="move_resize", x=-32000, y=0, width=800, height=600)
        exc = self.assert_code(spec, "BAD_PARAM", BadParam)
        self.assertIn("哨兵", exc.message)

    def test_bool_is_not_accepted_as_coordinate(self):
        spec = WindowActionSpec(action="move_resize", x=True, y=0, width=800, height=600)
        self.assert_code(spec, "BAD_PARAM", BadParam)

    def test_non_numeric_coordinate_is_bad_param(self):
        spec = WindowActionSpec(action="move_resize", x="100", y=0, width=800, height=600)
        self.assert_code(spec, "BAD_PARAM", BadParam)

    def test_integral_float_is_accepted_and_truncated(self):
        spec = WindowActionSpec(action="move_resize", x=10.0, y=20.0, width=800.0, height=600.0)
        args = build_window_calls(spec, self.target())[0]["args"]
        self.assertEqual(args[2:6], [10, 20, 800, 600])

    def test_non_integral_float_is_rejected(self):
        spec = WindowActionSpec(action="move_resize", x=10.5, y=0, width=800, height=600)
        self.assert_code(spec, "BAD_PARAM", BadParam)

    def test_always_on_top_without_value(self):
        self.assert_code(
            WindowActionSpec(action="always_on_top"), "MISSING_PARAM", MissingParam
        )

    def test_always_on_top_with_non_bool_is_bad_param(self):
        for bad in (1, 0, "true", "yes"):
            with self.subTest(value=bad):
                self.assert_code(
                    WindowActionSpec(action="always_on_top", always_on_top=bad),
                    "BAD_PARAM",
                    BadParam,
                )

    def test_move_resize_on_minimized_window_is_refused(self):
        # 最小化窗口报的是哨兵坐标，拿它当依据会挪到屏幕外
        spec = WindowActionSpec(action="move_resize", x=0, y=0, width=800, height=600)
        self.assert_code(spec, "WINDOW_MINIMIZED", WindowMinimized, minimized=True)

    def test_minimize_is_allowed_on_minimized_window(self):
        calls = build_window_calls(
            WindowActionSpec(action="minimize"), self.target(minimized=True)
        )
        self.assertEqual(calls[0]["args"][1], SW_MINIMIZE)

    def test_missing_window_id_is_bad_param(self):
        with self.assertRaises(BadParam) as cm:
            build_window_calls(WindowActionSpec(action="minimize"), {"pid": 1})
        self.assertEqual(cm.exception.code, "BAD_PARAM")


# --------------------------------------------------------------------------
# 干跑：一行都不许真发
# --------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):
    def test_default_is_dry_run_and_touches_nothing(self):
        out = window_action(StubDriver(), WindowActionSpec(action="minimize"), native=ExplodingNative())
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out["code"])
        self.assertEqual(out["action"], "minimize")
        self.assertEqual(out["would_call"]["calls"][0]["func"], "ShowWindow")
        self.assertIn("干跑", out["would_call"]["note"])

    def test_target_shape_is_slim(self):
        out = window_action(StubDriver(), WindowActionSpec(action="restore", pid=100))
        self.assertEqual(out["target"], {"pid": 100, "window_id": 2001, "title": "记事本 - 无标题"})

    def test_pick_by_window_id_wins(self):
        out = window_action(StubDriver(), WindowActionSpec(action="restore", window_id=2002, pid=100))
        self.assertEqual(out["target"]["window_id"], 2002)

    def test_pick_by_app_substring_matches_title_too(self):
        out = window_action(StubDriver(), WindowActionSpec(action="restore", app="浏览器"))
        self.assertEqual(out["target"]["window_id"], 2002)

    def test_pick_minimized_window_is_allowed_for_restore(self):
        out = window_action(StubDriver(), WindowActionSpec(action="restore", pid=102))
        self.assertTrue(out["ok"])
        self.assertEqual(out["target"]["window_id"], 2003)

    def test_window_not_found(self):
        out = window_action(StubDriver(), WindowActionSpec(action="restore", window_id=999))
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "WINDOW_NOT_FOUND")
        self.assertIsNone(out["target"])
        self.assertIsNone(out["would_call"])

    def test_unknown_action_is_structured_not_raised(self):
        out = window_action(StubDriver(), WindowActionSpec(action="teleport"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "UNKNOWN_ACTION")
        self.assertEqual(out["error"]["code"], "UNKNOWN_ACTION")

    def test_driver_error_is_normalized(self):
        driver = StubDriver(error=DriverError("驱动炸了", "exit=2"))
        out = window_action(driver, WindowActionSpec(action="close"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "DRIVER_ERROR")
        self.assertEqual(out["error"]["detail"], "exit=2")
        self.assertIsNone(out["would_call"])

    def test_only_list_windows_is_called(self):
        driver = StubDriver()
        window_action(driver, WindowActionSpec(action="minimize"))
        self.assertEqual([c[0] for c in driver.calls], ["list_windows"])
        self.assertEqual(driver.calls[0][1], {})


# --------------------------------------------------------------------------
# 执行：桩断言参数逐字段对上
# --------------------------------------------------------------------------


class TestExecute(unittest.TestCase):
    def setUp(self):
        # 隔离进程级副作用：真实 DPI 声明不在这里跑
        patcher = mock.patch.object(desktop_mod, "_DPI_STATE", "stub-declared")
        self.addCleanup(patcher.stop)
        patcher.start()

    def run_action(self, spec, native=None, driver=None, confirmed=False):
        native = native or RecordingNative()
        out = window_action(
            driver or StubDriver(), spec, execute=True, native=native, confirmed=confirmed
        )
        return out, native

    def test_minimize_executes_exactly_one_showwindow(self):
        out, native = self.run_action(WindowActionSpec(action="minimize", pid=100))
        self.assertFalse(out["dry_run"])
        self.assertIsNone(out["code"])
        self.assertEqual(native.calls, [("show_window", [2001, SW_MINIMIZE])])
        self.assertIn("已真实下发", out["would_call"]["note"])
        self.assertEqual(out["dpi_awareness"], "stub-declared")

    def test_maximize_and_restore_params(self):
        for action, cmd in (("maximize", SW_MAXIMIZE), ("restore", SW_RESTORE)):
            with self.subTest(action=action):
                _, native = self.run_action(WindowActionSpec(action=action, window_id=2003))
                self.assertEqual(native.calls, [("show_window", [2003, cmd])])

    def test_close_posts_wm_close_and_does_not_claim_delivery(self):
        # PostMessageW 返回值不可信（真机 hwnd=0 也返回 1），所以结果里不许出现"已关闭"
        # 注：R-12 之后 close 属危险动作，核心也要求确认——这里显式确认，测的是下发语义
        out, native = self.run_action(
            WindowActionSpec(action="close", window_id=2002), confirmed=True
        )
        self.assertEqual(native.calls, [("post_message", [2002, WM_CLOSE, 0, 0])])
        self.assertTrue(out["executed"][0]["called"])
        self.assertNotIn("closed", str(out))

    def test_activate_minimized_window_calls_restore_then_foreground(self):
        _, native = self.run_action(WindowActionSpec(action="activate", pid=102))
        self.assertEqual(
            native.calls, [("show_window", [2003, SW_RESTORE]), ("set_foreground_window", [2003])]
        )

    def test_move_resize_passes_physical_box_and_flags(self):
        spec = WindowActionSpec(
            action="move_resize", window_id=2001, x=-2160, y=-890, width=1440, height=2560
        )
        _, native = self.run_action(spec)
        name, args = native.calls[0]
        self.assertEqual(name, "set_window_pos")
        self.assertEqual(args, [2001, 0, -2160, -890, 1440, 2560, SWP_NOZORDER | SWP_NOACTIVATE])

    def test_always_on_top_sends_when_state_differs(self):
        native = RecordingNative(topmost=False)
        out, native = self.run_action(
            WindowActionSpec(action="always_on_top", window_id=2001, always_on_top=True), native
        )
        self.assertIn(("set_window_pos", [2001, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010]), native.calls)
        self.assertTrue(out["on_top_verified"])

    def test_always_on_top_skips_redundant_write(self):
        native = RecordingNative(topmost=True)
        out, native = self.run_action(
            WindowActionSpec(action="always_on_top", window_id=2001, always_on_top=True), native
        )
        self.assertEqual([name for name, _ in native.calls], ["is_topmost", "is_topmost"])
        self.assertFalse(out["executed"][0]["called"])
        self.assertIn("跳过", out["executed"][0]["reason"])
        self.assertTrue(out["on_top_verified"])

    def test_failed_native_call_carries_last_error(self):
        # R-8 之后 ShowWindow 的返回值语义是"调用前可见性"，false 不算失败
        # → 这里改用有成败语义的 SetForegroundWindow（activate 路径）
        native = RecordingNative(result=False)
        out, _ = self.run_action(WindowActionSpec(action="activate", window_id=2001), native)
        failed = [item for item in out["executed"] if item.get("succeeded") is False]
        self.assertTrue(failed, "activate 的 SetForegroundWindow 返回 0 应记为失败")
        self.assertEqual(failed[0]["last_error"], 1400)
        self.assertFalse(out["ok"])

    def test_dpi_awareness_is_declared_once_before_first_real_action(self):
        with mock.patch.object(desktop_mod, "_DPI_STATE", None), mock.patch.object(
            desktop_mod, "_declare_dpi_awareness", return_value="per-monitor-v2"
        ) as declare:
            out = window_action(
                StubDriver(),
                WindowActionSpec(action="minimize", window_id=2001),
                execute=True,
                native=RecordingNative(),
            )
        declare.assert_called_once()
        self.assertEqual(out["dpi_awareness"], "per-monitor-v2")

    def test_native_boundary_is_not_reached_on_validation_failure(self):
        native = ExplodingNative()
        out = window_action(
            StubDriver(),
            WindowActionSpec(action="always_on_top", window_id=2001),  # 缺 always_on_top
            execute=True,
            native=native,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "MISSING_PARAM")


# --------------------------------------------------------------------------
# 虚拟桌面：纯函数映射规则（不依赖 COM）
# --------------------------------------------------------------------------


class TestInterpretDesktopQuery(unittest.TestCase):
    # 占位符 GUID：真实桌面 id 属机器/会话标识，按 AGENTS.md 数据卫生不入库
    GUID_A = "{11111111-2222-3333-4444-555555555555}"
    GUID_B = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"

    def test_success(self):
        got = interpret_desktop_query(0, self.GUID_A, 0, 1)
        self.assertTrue(got["ok"])
        self.assertEqual(got["desktop_id"], self.GUID_A)
        self.assertTrue(got["on_current_desktop"])

    def test_on_current_false_is_kept(self):
        got = interpret_desktop_query(0, self.GUID_B, 0, 0)
        self.assertFalse(got["on_current_desktop"])

    def test_elementnotfound_is_per_window_not_unsupported(self):
        got = interpret_desktop_query(0x8002802B, None)
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "WINDOW_NOT_TRACKED")
        self.assertEqual(got["hresult"], 0x8002802B)

    def test_invalid_handle_reason(self):
        got = interpret_desktop_query(0x80070057, None)
        self.assertEqual(got["code"], "WINDOW_NOT_TRACKED")
        self.assertIn("无效", got["reason"])

    def test_zero_guid_is_not_success(self):
        got = interpret_desktop_query(0, "{00000000-0000-0000-0000-000000000000}")
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "NO_DESKTOP_ID")

    def test_unknown_hresult_still_structured(self):
        got = interpret_desktop_query(0x80004005, None)
        self.assertFalse(got["ok"])
        self.assertIn("0x80004005", got["reason"])

    def test_current_query_failure_does_not_break_main_result(self):
        got = interpret_desktop_query(0, "{11111111-2222-3333-4444-555555555555}", 0x80004005, 1)
        self.assertTrue(got["ok"])
        self.assertIsNone(got["on_current_desktop"])


class FakeBackend:
    def __init__(self, result):
        self.result = result
        self.queries: list[int] = []

    def query(self, hwnd):
        self.queries.append(hwnd)
        return self.result


class TestVirtualDesktopInfoWithFakeBackend(unittest.TestCase):
    def test_success_shape_carries_probe_metadata(self):
        backend = FakeBackend(
            {"ok": True, "code": None, "desktop_id": "{AABB}", "on_current_desktop": False}
        )
        out = virtual_desktop_info(2001, backend=backend)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["code"])
        self.assertEqual(out["window_id"], 2001)
        self.assertEqual(out["desktop_id"], "{AABB}")
        self.assertFalse(out["on_current_desktop"])
        self.assertEqual(out["probe"]["clsid"], "{AA509086-5CA9-4C25-8F95-589D3C07B48A}")
        self.assertFalse(out["probe"]["move_window_to_desktop_exposed"])
        self.assertEqual(backend.queries, [2001])

    def test_unsupported_passthrough(self):
        backend = FakeBackend(
            {"ok": False, "code": "UNSUPPORTED", "hresult": 0x80040154, "reason": "未注册"}
        )
        out = virtual_desktop_info(2001, backend=backend)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "UNSUPPORTED")
        self.assertEqual(out["hresult"], 0x80040154)

    def test_window_not_tracked_passthrough(self):
        backend = FakeBackend(
            {"ok": False, "code": "WINDOW_NOT_TRACKED", "hresult": 0x8002802B, "reason": "x"}
        )
        out = virtual_desktop_info(2001, backend=backend)
        self.assertEqual(out["code"], "WINDOW_NOT_TRACKED")
        self.assertNotIn("desktop_id", out)

    def test_no_foreground_window_is_reported_not_faked(self):
        with mock.patch.object(desktop_mod, "_foreground_window_id", return_value=0):
            out = virtual_desktop_info(None, backend=FakeBackend({}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "NO_TARGET")

    def test_none_uses_foreground_window_handle(self):
        backend = FakeBackend({"ok": True, "code": None, "desktop_id": "{X}", "on_current_desktop": True})
        with mock.patch.object(desktop_mod, "_foreground_window_id", return_value=4242):
            out = virtual_desktop_info(None, backend=backend)
        self.assertEqual(backend.queries, [4242])
        self.assertEqual(out["window_id"], 4242)


# --------------------------------------------------------------------------
# 真机只读用例（依赖环境 → 双重 skip 保护；不改任何窗口状态）
# --------------------------------------------------------------------------


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:  # pragma: no cover - 无 pytest 时退回 unittest 机制
        raise unittest.SkipTest(reason)


GUID_RE = re.compile(r"^\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}$")


class TestRealReadOnly(unittest.TestCase):
    """只读探测：读句柄/样式/桌面 id，绝不调用任何写接口。"""

    def test_native_boundary_is_wired_to_real_user32(self):
        if sys.platform != "win32":
            _skip("非 Windows 平台")
        try:
            native = NativeUser32()
        except WinUseError as exc:
            _skip(f"user32 不可用：{exc.message}")
        # hwnd=0 是"没有窗口"，读它只会失败，不会改变任何状态
        self.assertEqual(native.get_window_long_ptr(0, GWL_EXSTYLE), 0)
        self.assertEqual(native.last_error(), 1400)  # ERROR_INVALID_WINDOW_HANDLE

    def test_real_virtual_desktop_probe_is_read_only_and_self_consistent(self):
        if sys.platform != "win32":
            _skip("非 Windows 平台")
        backend = VirtualDesktopBackend()
        probe = backend.probe()
        if not probe["ok"]:
            _skip(f"本机 IVirtualDesktopManager 不可用：{probe.get('reason')}")
        out = virtual_desktop_info()
        if not out["ok"]:
            # 前台窗口恰好是"不被桌面跟踪"的窗口（实测外壳/悬浮层常见）→ 跳过而不是失败
            _skip(f"前台窗口读不到桌面归属：{out['code']}")
        self.assertTrue(GUID_RE.match(out["desktop_id"]))
        self.assertNotEqual(out["desktop_id"], "{00000000-0000-0000-0000-000000000000}")
        self.assertIn(out["on_current_desktop"], (True, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
