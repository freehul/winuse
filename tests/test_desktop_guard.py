"""T-23：窗口动作的**下发前安全判定**（安全判定下沉到 core/desktop.py）。

要守住的三件事：

1. **拦得住**：目标是系统级窗口（任务栏 / 资源管理器 / 设置页）时，`window_action`
   在**任何调用下发之前**就拒绝——即使动作只是 minimize（T-23 的原话口径）。
2. **判据单一出处**：危险清单与系统窗口清单只存在于 `winuse.core.guard`。本文件不
   复制那份清单，而是**从 guard 的常量驱动**穷举断言（清单将来加了名字，这里自动覆盖）；
   另外用 monkeypatch 证明 core 的判定真的走 `guard.assess_action`。
3. **两个维度都在核心判（R-12 之后变更）**：动作名维度（close/kill/电源）**也**下沉
   到了 core（`assess_window_action`）。原先只判目标维度，理由是"门面没有通道把确认
   状态传进核心，core 再拦一次会让 `--confirm` 过不去"——该前提已作废：`window_action`
   现在有 `confirmed` 参数且两个门面都透传（见 `tests/test_window_confirm_wiring.py`），
   所以确认过得去，动作名维度可以安全下沉。只判目标维度会让**任何直接调核心**的路径
   绕过关闭确认（核心安全边界不自包含），这不是可接受的取舍。

铁律：**单测不碰真实桌面**。全部走桩驱动 + 桩原生边界（含 ExplodingNative，
任何一次原生调用都让用例失败）。窗口标题一律占位符，不带本机实例信息。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core import desktop as desktop_mod  # noqa: E402
from winuse.core import guard as guard_mod  # noqa: E402
from winuse.core.desktop import (  # noqa: E402
    SW_MINIMIZE,
    SW_RESTORE,
    WINDOW_ACTIONS,
    WindowActionSpec,
    assess_window_action,
    assess_window_target,
    window_action,
)

#: 占位标题/应用名（不写真实窗口标题、不带本机实例信息）
SYSTEM_APP = "explorer.exe"  # guard.SYSTEM_APPS 成员
PLAIN_APP = "notepad.exe"
SYSTEM_TITLE = "任务栏（占位标题）"  # 命中 guard.SYSTEM_TITLE_PATTERNS
PLAIN_TITLE = "占位标题 · 普通文档"


def win(pid, wid, title, app="App.exe", minimized=False, z=1):
    """造一个 list_windows 记录（字段与驱动实测一致，与 test_desktop 自足不耦合）。"""
    return {
        "app_name": app,
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        "is_on_screen": True,
        "layer": 0,
        "minimized": minimized,
        "pid": pid,
        "title": title,
        "window_id": wid,
        "z_index": z,
    }


PLAIN_WINDOWS = [win(100, 2001, PLAIN_TITLE, app=PLAIN_APP, z=2)]
SYSTEM_WINDOWS = [win(101, 3001, SYSTEM_TITLE, app=SYSTEM_APP, z=2)]


class StubDriver:
    """假的驱动：只答 list_windows。"""

    def __init__(self, windows=None):
        self.windows = PLAIN_WINDOWS if windows is None else windows
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        assert tool == "list_windows", f"窗口动作只应调 list_windows，收到 {tool}"
        return {"windows": self.windows}


class RecordingNative:
    """桩：记录每一次原生调用（真跑用例用）。"""

    def __init__(self, result=True):
        self.calls: list[tuple[str, list]] = []
        self.result = result

    def _log(self, name, args):
        self.calls.append((name, list(args)))
        return self.result

    def last_error(self):
        return 1400

    def show_window(self, hwnd, cmd):
        return self._log("show_window", [hwnd, cmd])

    def set_window_pos(self, hwnd, insert_after, x, y, cx, cy, flags):
        return self._log("set_window_pos", [hwnd, insert_after, x, y, cx, cy, flags])

    def set_foreground_window(self, hwnd):
        return self._log("set_foreground_window", [hwnd])

    def post_message(self, hwnd, msg, wparam, lparam):
        return self._log("post_message", [hwnd, msg, wparam, lparam])

    def get_window_long_ptr(self, hwnd, index):
        return self._log("get_window_long_ptr", [hwnd, index])

    def is_topmost(self, hwnd):
        self.calls.append(("is_topmost", [hwnd]))
        return True


class ExplodingNative:
    """拦截用例专用：任何一次原生调用都会让用例失败。"""

    def __getattr__(self, name):
        raise AssertionError(f"被拦截的动作不应触碰原生接口：{name}")


def spec_for(action: str) -> WindowActionSpec:
    """每个动作都给一份**参数合法**的 spec：证明拦截不是靠参数错误挡下来的。"""
    extra: dict = {}
    if action == "move_resize":
        extra = {"x": 10, "y": 20, "width": 800, "height": 600}
    elif action == "always_on_top":
        extra = {"always_on_top": True}
    return WindowActionSpec(action=action, window_id=3001, **extra)


# --------------------------------------------------------------------------
# 纯函数：目标维度判定（判据来自 guard）
# --------------------------------------------------------------------------


class TestAssessWindowTarget(unittest.TestCase):
    def test_system_app_is_dangerous_with_guard_rule(self):
        got = assess_window_target("minimize", {"app_name": SYSTEM_APP, "title": PLAIN_TITLE})
        self.assertTrue(got.dangerous)
        self.assertEqual(got.code, guard_mod.CONFIRM_CODE)
        self.assertEqual(got.rule, guard_mod.SYSTEM_WINDOW_RULE)
        self.assertIn("系统级窗口", got.reason)  # 文案也来自 guard，不是本地另写

    def test_system_title_is_dangerous_even_with_plain_app(self):
        got = assess_window_target("minimize", {"app_name": PLAIN_APP, "title": SYSTEM_TITLE})
        self.assertTrue(got.dangerous)
        self.assertEqual(got.rule, guard_mod.SYSTEM_WINDOW_RULE)

    def test_plain_window_is_safe(self):
        got = assess_window_target("minimize", {"app_name": PLAIN_APP, "title": PLAIN_TITLE})
        self.assertFalse(got.dangerous)
        self.assertIsNone(got.code)
        self.assertIsNone(got.rule)

    def test_no_target_is_safe_not_crash(self):
        for target in (None, {}):
            with self.subTest(target=target):
                self.assertFalse(assess_window_target("minimize", target).dangerous)

    def test_same_verdict_as_guard_is_system_window(self):
        """口径等价：core 的判定结果必须与 guard.is_system_window 完全一致。"""
        samples = [
            {"app_name": SYSTEM_APP, "title": PLAIN_TITLE},
            {"app_name": SYSTEM_APP.upper(), "title": ""},
            {"app_name": " other.exe ", "title": SYSTEM_TITLE},
            {"app_name": PLAIN_APP, "title": PLAIN_TITLE},
            {"app": SYSTEM_APP, "title": PLAIN_TITLE},  # 兼容 app 字段
            {},
        ]
        for target in samples:
            with self.subTest(target=target):
                self.assertEqual(
                    assess_window_target("minimize", target).dangerous,
                    guard_mod.is_system_window(target),
                )

    def test_every_guard_system_app_is_caught(self):
        """数据驱动：guard 的系统应用清单将来加名字，这里自动覆盖（不复制清单）。"""
        self.assertTrue(guard_mod.SYSTEM_APPS, "guard 的系统应用清单不该为空")
        for app in guard_mod.SYSTEM_APPS:
            with self.subTest(app=app):
                self.assertTrue(assess_window_target("minimize", {"app_name": app}).dangerous)

    def test_every_guard_title_pattern_is_caught(self):
        for pattern in guard_mod.SYSTEM_TITLE_PATTERNS:
            with self.subTest(pattern=pattern):
                target = {"app_name": PLAIN_APP, "title": f"占位 {pattern} 占位"}
                self.assertTrue(assess_window_target("minimize", target).dangerous)

    def test_action_name_dimension_is_assessed_in_core(self):
        """R-12：动作名维度下沉到 core（主路径 assess_window_action 两维都判）。"""
        self.assertIn("close", guard_mod.DANGEROUS_ACTIONS)  # 前提：close 是危险动作名
        plain = {"app_name": PLAIN_APP, "title": PLAIN_TITLE}
        # 主路径：close + 普通窗口 → 危险（原先把这一维留给门面，核心可绕过）
        self.assertTrue(assess_window_action("close", plain).dangerous)
        # 目标维度专用口径：单独看目标时，普通窗口不危险（这条语义保留不变）
        self.assertFalse(assess_window_target("close", plain).dangerous)
        # 反过来：minimize 不在危险动作清单里，但目标是系统级窗口 → 仍然危险
        self.assertNotIn("minimize", guard_mod.DANGEROUS_ACTIONS)
        self.assertTrue(assess_window_action("minimize", {"app_name": SYSTEM_APP}).dangerous)

    def test_target_only_action_name_is_not_a_dangerous_action(self):
        """判定靠"空动作名"这一手：它必须不在危险清单里，否则语义会被动作名带偏。"""
        self.assertNotIn(desktop_mod.TARGET_ONLY_ACTION, guard_mod.DANGEROUS_ACTIONS)


# --------------------------------------------------------------------------
# 闸门：解析出目标、下发前拦
# --------------------------------------------------------------------------


class TestGateBlocksSystemWindow(unittest.TestCase):
    def test_minimize_on_system_window_is_refused_in_dry_run(self):
        out = window_action(
            StubDriver(SYSTEM_WINDOWS),
            WindowActionSpec(action="minimize", app=SYSTEM_APP),
            native=ExplodingNative(),
        )
        self.assertFalse(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["code"], guard_mod.CONFIRM_CODE)
        self.assertTrue(out["requires_confirm"])
        self.assertEqual(out["danger"]["rule"], guard_mod.SYSTEM_WINDOW_RULE)
        self.assertIsNone(out["would_call"], "被拦下就不该给出调用计划")
        self.assertNotIn("executed", out, "被拦下就没执行过")

    def test_minimize_on_system_window_is_refused_when_executing(self):
        native = ExplodingNative()
        out = window_action(
            StubDriver(SYSTEM_WINDOWS),
            WindowActionSpec(action="minimize", app=SYSTEM_APP),
            execute=True,
            native=native,
        )
        self.assertFalse(out["ok"])
        self.assertFalse(out["dry_run"])
        self.assertEqual(out["code"], guard_mod.CONFIRM_CODE)

    def test_every_window_action_is_refused_on_system_window(self):
        """穷举动作集：参数全都合法，照样一条都不下发（含 minimize）。"""
        for action in sorted(WINDOW_ACTIONS):
            with self.subTest(action=action):
                out = window_action(
                    StubDriver(SYSTEM_WINDOWS), spec_for(action), native=ExplodingNative()
                )
                self.assertFalse(out["ok"], f"{action} 应被拦")
                self.assertEqual(out["code"], guard_mod.CONFIRM_CODE)
                self.assertEqual(out["action"], action)

    def test_refusal_shape_is_structured_and_traceable(self):
        out = window_action(
            StubDriver(SYSTEM_WINDOWS),
            WindowActionSpec(action="restore", app=SYSTEM_APP),
            native=ExplodingNative(),
        )
        self.assertEqual(
            set(out),
            {"ok", "dry_run", "action", "target", "would_call", "code", "requires_confirm", "danger", "error"},
        )
        self.assertEqual(
            out["target"], {"pid": 101, "window_id": 3001, "title": SYSTEM_TITLE}
        )
        self.assertEqual(out["error"]["code"], guard_mod.CONFIRM_CODE)
        self.assertIn("restore", out["error"]["message"])
        self.assertIn(guard_mod.SYSTEM_WINDOW_RULE, out["error"]["detail"])
        self.assertTrue(out["danger"]["reason"])

    def test_blocked_before_parameter_validation(self):
        """闸门在参数校验之前：安全判定优先，畸形参数不会盖掉"被拦"这件事。"""
        spec = WindowActionSpec(action="move_resize", window_id=3001)  # 缺 x/y/width/height
        out = window_action(StubDriver(SYSTEM_WINDOWS), spec, native=ExplodingNative())
        self.assertEqual(out["code"], guard_mod.CONFIRM_CODE)

    def test_system_window_picked_by_title_is_also_blocked(self):
        windows = [win(101, 3001, SYSTEM_TITLE, app=PLAIN_APP)]
        out = window_action(
            StubDriver(windows), WindowActionSpec(action="minimize", app="占位"), native=ExplodingNative()
        )
        self.assertEqual(out["code"], guard_mod.CONFIRM_CODE)

    def test_confirmed_true_is_the_core_level_escape_hatch(self):
        native = RecordingNative()
        out = window_action(
            StubDriver(SYSTEM_WINDOWS),
            WindowActionSpec(action="minimize", window_id=3001),
            execute=True,
            native=native,
            confirmed=True,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(native.calls, [("show_window", [3001, SW_MINIMIZE])])

    def test_core_decision_comes_from_guard_assess_action(self):
        """判据单一出处：core 必须真的调 guard.assess_action（不是本地另判一份）。"""
        stub = guard_mod.RiskVerdict.danger("桩：一律危险", "stub_rule")
        with mock.patch.object(guard_mod, "assess_action", return_value=stub) as called:
            out = window_action(
                StubDriver(PLAIN_WINDOWS),  # 目标是普通窗口，本来不危险
                WindowActionSpec(action="minimize", window_id=2001),
                native=ExplodingNative(),
            )
        self.assertFalse(out["ok"])
        self.assertEqual(out["danger"]["rule"], "stub_rule")
        called.assert_called_once()
        action, target = called.call_args.args
        # R-12 之后传的是真实动作名（两个维度都交给 guard），不再是空动作名
        self.assertEqual(action, "minimize")
        self.assertEqual(target["window_id"], 2001)
        self.assertEqual(target["app_name"], PLAIN_APP)


# --------------------------------------------------------------------------
# 回归：普通窗口一切照旧；动作名维度的边界不许越
# --------------------------------------------------------------------------


class TestPlainWindowUnchanged(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(desktop_mod, "_DPI_STATE", "stub-declared")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_dry_run_still_returns_planned_calls(self):
        out = window_action(StubDriver(), WindowActionSpec(action="minimize", window_id=2001))
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out["code"])
        self.assertEqual(out["would_call"]["calls"][0]["func"], "ShowWindow")
        self.assertIn("干跑", out["would_call"]["note"])

    def test_execute_still_sends_exactly_one_call(self):
        native = RecordingNative()
        out = window_action(
            StubDriver(),
            WindowActionSpec(action="restore", window_id=2001),
            execute=True,
            native=native,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(native.calls, [("show_window", [2001, SW_RESTORE])])

    def test_close_on_plain_window_requires_confirmation(self):
        """R-12：核心自己也要拦 close —— 未确认时不下发，确认后才发 WM_CLOSE。"""
        native = RecordingNative()
        out = window_action(
            StubDriver(),
            WindowActionSpec(action="close", window_id=2001),
            execute=True,
            native=native,
            confirmed=False,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], guard_mod.CONFIRM_CODE)
        self.assertIsNone(out["would_call"]), "拦住时不得给出下发计划"
        self.assertEqual(native.calls, []), "未确认绝不能下发任何原生调用"

        native2 = RecordingNative()
        out2 = window_action(
            StubDriver(),
            WindowActionSpec(action="close", window_id=2001),
            execute=True,
            native=native2,
            confirmed=True,
        )
        self.assertTrue(out2["ok"]), "显式确认后必须能真的关（不是谁都关不了）"
        self.assertEqual([name for name, _ in native2.calls], ["post_message"])

    def test_no_confirmed_kwarg_needed_by_facades(self):
        """门面（CLI/MCP）就是这么调的：只传 driver/spec/execute——普通窗口不受影响。"""
        out = window_action(StubDriver(), WindowActionSpec(action="minimize", window_id=2001), execute=True)
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
