"""winuse_session_state 单测：会话/锁屏状态判定 + 能力可用性翻译。

全部走**假原生边界**（`FakeSessionApi`）——绝不在单测里动真实桌面，
更不锁真实屏幕（锁屏会打断用户，是明确的副作用）。

四条路径逐一覆盖：
| 路径 | 原始信号 | 期望 |
|---|---|---|
| 正常桌面 | `OpenInputDesktop` 成功、名字 `Default` | `locked=False` / `secure_desktop=False` |
| 锁屏（Winlogon） | 名字 `Winlogon` | `locked=True` / `secure_desktop=True` |
| API 调用失败 | 打开失败、WTS 也失败 | 全 `None` + `ok=False`（**不是 False**） |
| 权限不足 | 打开失败 + `ERROR_ACCESS_DENIED`、WTS 能答 | `locked` 由 WTS 给出，`secure_desktop` 仍 `None` |

最后一条是本文件的重点：**`None` 绝不能被当成 `False`**——假阴性会被调用方
读成"可以操作"，比不知道更危险。所以每条"未知"路径都额外断言 `is None`
且可用性**不是** `True`。
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.session_state import (  # noqa: E402
    CAPABILITIES,
    INTERACTIVE_DESKTOP_NAME,
    SECURE_DESKTOP_NAMES,
    SESSION_NOTE,
    UNKNOWN_AVAILABILITY_REASON,
    build_availability,
    classify_input_desktop,
    detect_session_state,
    explain_action_availability,
    flags_to_locked,
    parse_session_info_ex,
)

#: WTSSessionInfoEx 的原始缓冲区（只关心前 5 个 DWORD）：
#: 0..3 = Level、4..7 = 对齐填充（联合体按 8 字节对齐）、8..11 = SessionId、
#: 12..15 = SessionState、16..19 = SessionFlags。
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1
WTS_SESSIONSTATE_UNKNOWN = 0xFFFFFFFF


def wts_buffer(flags: int, level: int = 1, session_id: int = 1, session_state: int = 0) -> bytes:
    """造一份 WTSSessionInfoEx 缓冲区（**按真实偏移**，不靠猜字段顺序）。

    `SessionFlags` 按无符号打包：0xFFFFFFFF（未知道值）要能原样放进去。
    """
    return struct.pack("<IIIiI", level, 0, session_id, session_state, flags) + b"\x00" * 240


def state(locked, secure_desktop, input_desktop=None, ok=True, note="样例"):
    """造一份 `detect_session_state` 形状的状态（纯字典，不碰任何原生调用）。"""
    return {
        "ok": ok,
        "locked": locked,
        "input_desktop": input_desktop,
        "secure_desktop": secure_desktop,
        "foreground_pid": None,
        "note": note,
    }


class FakeSessionApi:
    """假原生边界：按设定回放，并记录调用顺序（默认**不**碰真实 user32/wtsapi32）。"""

    def __init__(
        self,
        *,
        platform_ok=True,
        handle=0xCC,
        name: str | None = "Default",
        name_error: int | None = None,
        open_error=0,
        wts_buffer_bytes: bytes | None = None,
        wts_error: str | None = None,
        foreground_pid: int | None = 4242,
        raise_on=None,
    ):
        self._platform_ok = platform_ok
        self._handle = handle
        self._name = name
        self._name_error = name_error
        self._open_error = open_error
        self._wts = wts_buffer_bytes
        self._wts_error = wts_error
        self._foreground_pid = foreground_pid
        self._raise_on = raise_on or set()
        self.calls: list[tuple[str, tuple]] = []
        self.closed: list[int] = []

    def _guard(self, method: str) -> None:
        self.calls.append((method, ()))
        if method in self._raise_on:
            raise OSError(f"样例原生异常：{method}")

    def platform_ok(self) -> bool:
        return self._platform_ok

    def open_input_desktop(self) -> int:
        self._guard("open_input_desktop")
        return self._handle if not self._open_error else 0

    def desktop_name(self, handle: int) -> str | None:
        self._guard("desktop_name")
        if self._name_error is not None:
            return None
        return self._name

    def close_desktop(self, handle: int) -> bool:
        self.closed.append(handle)
        self.calls.append(("close_desktop", (handle,)))
        return True

    def last_error(self) -> int:
        self.calls.append(("last_error", ()))
        if self._name_error is not None:
            return self._name_error
        return self._open_error

    def session_info_ex(self) -> bytes | None:
        self._guard("session_info_ex")
        if self._wts_error is not None:
            return None
        return self._wts

    def foreground_pid(self) -> int | None:
        self._guard("foreground_pid")
        return self._foreground_pid

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


#: 便捷构造：正常桌面（名字 = Default）
def default_desktop_api(**kwargs):
    return FakeSessionApi(name=INTERACTIVE_DESKTOP_NAME, wts_buffer_bytes=wts_buffer(1), **kwargs)


#: 便捷构造：锁屏（名字 = Winlogon）
def locked_api(**kwargs):
    return FakeSessionApi(name="Winlogon", wts_buffer_bytes=wts_buffer(0), **kwargs)


# --------------------------------------------------------------------------
# 纯函数：桌面名 → 判定
# --------------------------------------------------------------------------


class TestClassifyInputDesktop(unittest.TestCase):
    def test_default_desktop_means_unlocked(self):
        got = classify_input_desktop(INTERACTIVE_DESKTOP_NAME)
        self.assertIs(got["locked"], False)
        self.assertIs(got["secure_desktop"], False)
        self.assertEqual(got["desktop_class"], "interactive")
        self.assertTrue(got["reason"])

    def test_name_is_case_and_space_tolerant(self):
        for name in ("Default", " default ", "DEFAULT"):
            with self.subTest(name=name):
                self.assertIs(classify_input_desktop(name)["locked"], False)

    def test_winlogon_means_locked_and_secure(self):
        got = classify_input_desktop("Winlogon")
        self.assertIs(got["locked"], True)
        self.assertIs(got["secure_desktop"], True)
        self.assertEqual(got["desktop_class"], "secure")
        self.assertIn("Winlogon", SECURE_DESKTOP_NAMES or ("Winlogon",))

    def test_winlogon_match_is_case_insensitive(self):
        self.assertIs(classify_input_desktop("WINLOGON")["locked"], True)
        self.assertIs(classify_input_desktop("winlogon")["locked"], True)

    def test_missing_name_is_unknown_not_unlocked(self):
        for name in (None, "", "   "):
            with self.subTest(name=name):
                got = classify_input_desktop(name)
                self.assertIsNone(got["locked"], "拿不到名字必须回 None——不是 False")
                self.assertIsNone(got["secure_desktop"])
                self.assertEqual(got["desktop_class"], "unknown")

    def test_unknown_desktop_name_is_not_guessed(self):
        """不认识的桌面名（如断开连接的会话桌面）不下结论：既不说锁了也不说没锁。"""
        got = classify_input_desktop("Disconnected")
        self.assertIsNone(got["locked"])
        self.assertIsNone(got["secure_desktop"])
        self.assertEqual(got["desktop_class"], "other")
        self.assertIn("Disconnected", got["reason"], "未知名字要如实回显，便于排障")


# --------------------------------------------------------------------------
# 纯函数：WTSSessionInfoEx 缓冲区 → SessionFlags
# --------------------------------------------------------------------------


class TestParseSessionInfoEx(unittest.TestCase):
    def test_level1_buffer_yields_session_flags(self):
        got = parse_session_info_ex(wts_buffer(WTS_SESSIONSTATE_LOCK))
        self.assertEqual(got["level"], 1)
        self.assertEqual(got["session_flags"], WTS_SESSIONSTATE_LOCK)

    def test_unlock_flag_is_read_verbatim(self):
        got = parse_session_info_ex(wts_buffer(WTS_SESSIONSTATE_UNLOCK))
        self.assertEqual(got["session_flags"], WTS_SESSIONSTATE_UNLOCK)

    def test_short_buffer_is_refused_not_misread(self):
        """缓冲区比需要的短就回 None：硬按偏移读会读到垃圾，那等于编数据。"""
        short = struct.pack("<IIIi", 1, 0, 1, 0)  # 20 字节布局里缺 SessionFlags 那 4 字节
        for raw in (b"", b"\x01\x00\x00\x00", short):
            with self.subTest(size=len(raw)):
                got = parse_session_info_ex(raw)
                self.assertIsNone(got["level"])
                self.assertIsNone(got["session_flags"])

    def test_unknown_level_has_no_flags(self):
        """只有 Level==1 的布局是我们认识的那套；别的层级不许按 1 的偏移解释。"""
        got = parse_session_info_ex(wts_buffer(0, level=2))
        self.assertEqual(got["level"], 2)
        self.assertIsNone(got["session_flags"])

    def test_garbage_is_not_a_crash(self):
        got = parse_session_info_ex(b"\xff" * 8)
        self.assertIsNone(got["level"])


class TestFlagsToLocked(unittest.TestCase):
    def test_lock_flag(self):
        self.assertIs(flags_to_locked(WTS_SESSIONSTATE_LOCK), True)

    def test_unlock_flag(self):
        self.assertIs(flags_to_locked(WTS_SESSIONSTATE_UNLOCK), False)

    def test_documented_unknown_flag(self):
        """MSDN：0xFFFFFFFF = 状态未知；未知不许落成 False。"""
        self.assertIsNone(flags_to_locked(WTS_SESSIONSTATE_UNKNOWN))

    def test_unexpected_flag_values_are_unknown(self):
        for flags in (2, 3, -1, None, "0"):
            with self.subTest(flags=flags):
                self.assertIsNone(flags_to_locked(flags))


# --------------------------------------------------------------------------
# 纯函数：状态 → 能力可用性
# --------------------------------------------------------------------------


class TestBuildAvailability(unittest.TestCase):
    def test_every_capability_has_available_and_reason(self):
        got = build_availability(state(False, False, INTERACTIVE_DESKTOP_NAME))
        self.assertEqual(sorted(got), sorted(CAPABILITIES))
        for name, entry in got.items():
            with self.subTest(capability=name):
                self.assertIn(entry["available"], (True, False, None))
                self.assertIsInstance(entry["reason"], str)
                self.assertTrue(entry["reason"])

    def test_interactive_desktop_makes_everything_available(self):
        got = build_availability(state(False, False, INTERACTIVE_DESKTOP_NAME))
        for name, entry in got.items():
            with self.subTest(capability=name):
                self.assertIs(entry["available"], True, name)

    def test_locked_desktop_blocks_input_and_reading(self):
        got = build_availability(state(True, True, "Winlogon"))
        for name in ("read_elements", "keyboard_input", "mouse_click", "drag", "launch_app"):
            with self.subTest(capability=name):
                self.assertIs(got[name]["available"], False, name)

    def test_screenshot_on_a_locked_desktop_is_unknown_not_false(self):
        """逐窗口捕获在锁屏下可能仍可用——没有实测依据就不下结论。"""
        got = build_availability(state(True, True, "Winlogon"))
        self.assertIsNone(got["screenshot"]["available"])
        self.assertIn("未验证", got["screenshot"]["reason"])

    def test_secure_desktop_alone_is_treated_as_blocked(self):
        got = build_availability(state(None, True, "Winlogon"))
        self.assertIs(got["keyboard_input"]["available"], False)

    def test_unknown_state_makes_everything_unknown(self):
        got = build_availability(state(None, None))
        for name, entry in got.items():
            with self.subTest(capability=name):
                self.assertIsNone(entry["available"], f"{name}：未知不许落成 True/False")
                self.assertEqual(entry["reason"], UNKNOWN_AVAILABILITY_REASON)

    def test_not_locked_but_secure_desktop_unknown_is_not_reported_available(self):
        """关键路径：WTS 说没锁，但输入桌面名读不到 → 仍可能是安全桌面。

        这时**任何**能力都不许报 True（报 True 就是假阴性）。
        """
        got = build_availability(state(False, None))
        for name, entry in got.items():
            with self.subTest(capability=name):
                self.assertIsNot(entry["available"], True, f"{name} 不许在证据不足时报可用")
                self.assertIsNone(entry["available"])

    def test_malformed_state_does_not_crash_and_stays_unknown(self):
        for bad in (None, {}, "not a dict", 123, {"locked": "yes"}):
            with self.subTest(bad=bad):
                got = build_availability(bad)
                for name, entry in got.items():
                    self.assertIsNone(entry["available"], name)


# --------------------------------------------------------------------------
# detect_session_state：四条路径
# --------------------------------------------------------------------------


class TestDetectSessionStateNormalDesktop(unittest.TestCase):
    def test_reports_unlocked_with_the_desktop_name(self):
        out = detect_session_state(default_desktop_api())
        self.assertTrue(out["ok"])
        self.assertIs(out["locked"], False)
        self.assertIs(out["secure_desktop"], False)
        self.assertEqual(out["input_desktop"], INTERACTIVE_DESKTOP_NAME)
        self.assertEqual(out["desktop_class"], "interactive")
        self.assertEqual(out["locked_basis"], "input_desktop_name")
        self.assertEqual(out["foreground_pid"], 4242)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["note"], SESSION_NOTE)

    def test_probe_block_is_filled_in(self):
        out = detect_session_state(default_desktop_api())
        probe = out["probe"]
        self.assertTrue(probe["input_desktop_opened"])
        self.assertTrue(probe["name_read"])
        self.assertTrue(probe["handle_closed"])
        self.assertIsNone(probe["win32_error"])

    def test_handle_is_closed(self):
        api = default_desktop_api()
        detect_session_state(api)
        self.assertEqual(api.closed, [api._handle])

    def test_wts_is_not_asked_when_the_name_already_answers(self):
        """主判据给出结论就不多问一次：多探一次就多一次口径冲突的机会。"""
        api = default_desktop_api()
        detect_session_state(api)
        self.assertNotIn("session_info_ex", api.methods())

    def test_missing_foreground_pid_is_none(self):
        out = detect_session_state(default_desktop_api(foreground_pid=None))
        self.assertIsNone(out["foreground_pid"])
        self.assertIs(out["locked"], False, "没有前台窗口不代表锁屏，主判据仍以桌面名为准")


class TestDetectSessionStateLocked(unittest.TestCase):
    def test_winlogon_desktop_is_reported_locked(self):
        api = locked_api()
        out = detect_session_state(api)
        self.assertTrue(out["ok"])
        self.assertIs(out["locked"], True)
        self.assertIs(out["secure_desktop"], True)
        self.assertEqual(out["input_desktop"], "Winlogon")
        self.assertEqual(out["desktop_class"], "secure")
        self.assertNotIn("session_info_ex", api.methods(), "名字已给出结论，不必再问 WTS")
        self.assertEqual(out["errors"], [])

    def test_handle_is_closed_on_the_locked_path_too(self):
        api = locked_api()
        detect_session_state(api)
        self.assertEqual(len(api.closed), 1)


class TestDetectSessionStateApiFailure(unittest.TestCase):
    def _failing(self, **kwargs):
        return FakeSessionApi(
            handle=0,
            name=None,
            open_error=87,
            wts_buffer_bytes=None,
            foreground_pid=None,
            **kwargs,
        )

    def test_unavailable_probe_is_not_reported_as_unlocked(self):
        out = detect_session_state(self._failing())
        self.assertFalse(out["ok"])
        self.assertIsNone(out["locked"], "探测失败必须回 None——回来 False 会被当成\"可以操作\"")
        self.assertIsNone(out["secure_desktop"])
        self.assertIsNone(out["input_desktop"])
        self.assertIsNone(out["foreground_pid"])
        self.assertIsNone(out["locked_basis"])
        self.assertEqual(out["desktop_class"], "unknown")

    def test_failures_are_recorded_not_swallowed(self):
        out = detect_session_state(self._failing())
        codes = [e["code"] for e in out["errors"]]
        self.assertIn("INPUT_DESKTOP_UNAVAILABLE", codes)
        self.assertIn("WTS_PROBE_UNAVAILABLE", codes)
        self.assertIsNone(out["probe"]["handle_closed"], "没打开成功就不该报\"已关闭\"")

    def test_name_read_failure_falls_back_to_wts(self):
        api = FakeSessionApi(name=None, name_error=122, wts_buffer_bytes=wts_buffer(WTS_SESSIONSTATE_UNLOCK))
        out = detect_session_state(api)
        self.assertIsNone(out["input_desktop"])
        self.assertIs(out["locked"], False)
        self.assertIsNone(out["secure_desktop"], "只凭 WTS 不能宣称\"没有安全桌面\"")
        self.assertEqual(out["locked_basis"], "wts_session_flags")
        self.assertFalse(out["probe"]["name_read"])
        self.assertEqual(len(api.closed), 1, "读到名字失败也必须关句柄")

    def test_native_exception_does_not_escape(self):
        api = FakeSessionApi(raise_on={"open_input_desktop", "session_info_ex", "foreground_pid"})
        out = detect_session_state(api)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["locked"])
        self.assertTrue(out["errors"])

    def test_non_windows_platform_is_declared_not_crashed(self):
        out = detect_session_state(FakeSessionApi(platform_ok=False))
        self.assertFalse(out["ok"])
        self.assertIsNone(out["locked"])
        self.assertIn("平台", out["note"])


class TestDetectSessionStatePermissionDenied(unittest.TestCase):
    """权限不足：`OpenInputDesktop` 失败 + `ERROR_ACCESS_DENIED`（真机实测就是这个）。

    真机实测依据：普通权限进程 `OpenDesktopW("Winlogon")` 直接回 `ERROR_ACCESS_DENIED`，
    所以锁屏时输入桌面（Winlogon）多半**打不开**——这时才需要 WTS 兜底。
    """

    def _denied(self, flags):
        return FakeSessionApi(handle=0, name=None, open_error=5, wts_buffer_bytes=wts_buffer(flags))

    def test_wts_resolves_locked_while_secure_desktop_stays_unknown(self):
        out = detect_session_state(self._denied(WTS_SESSIONSTATE_LOCK))
        self.assertTrue(out["ok"], "WTS 给出了确定答案，探测算跑通")
        self.assertIs(out["locked"], True)
        self.assertIsNone(out["secure_desktop"], "WTS 回答不了\"是不是安全桌面\"，只能是 None")
        self.assertIsNone(out["input_desktop"])
        self.assertEqual(out["locked_basis"], "wts_session_flags")
        self.assertEqual(out["probe"]["win32_error"], 5)
        self.assertEqual(out["probe"]["win32_error_name"], "ERROR_ACCESS_DENIED")

    def test_wts_unlocked_is_not_treated_as_operable(self):
        """WTS 说没锁，但输入桌面打不开（可能是 UAC 安全桌面）→ 可用性必须是 None。"""
        out = detect_session_state(self._denied(WTS_SESSIONSTATE_UNLOCK))

        class _NeverCalled:
            def __getattr__(self, name):
                raise AssertionError("显式给了 state，就不该再去探测")

        got = explain_action_availability(out, api=_NeverCalled())
        self.assertIsNone(got["drivable"])
        for name, entry in got["capabilities"].items():
            with self.subTest(capability=name):
                self.assertIsNone(entry["available"], f"{name}：证据不足不许报 True")
        self.assertEqual(got["unavailable"], [], "证据不足时不该宣称\"不可用\"，那是编结论")

    def test_unknown_wts_flag_falls_back_to_none(self):
        out = detect_session_state(self._denied(WTS_SESSIONSTATE_UNKNOWN))
        self.assertIsNone(out["locked"])
        self.assertFalse(out["ok"])


# --------------------------------------------------------------------------
# explain_action_availability：状态 → 逐项结论
# --------------------------------------------------------------------------


class TestExplainActionAvailability(unittest.TestCase):
    def test_detects_on_its_own_when_no_state_is_given(self):
        got = explain_action_availability(api=default_desktop_api())
        self.assertTrue(got["ok"])
        self.assertIs(got["locked"], False)
        self.assertIs(got["drivable"], True)
        self.assertEqual(got["unavailable"], [])
        self.assertEqual(got["unknown"], [])
        self.assertEqual(sorted(got["capabilities"]), sorted(CAPABILITIES))

    def test_accepts_a_ready_state_without_touching_the_native_api(self):
        class _NeverCalled:
            def __getattr__(self, name):
                raise AssertionError("显式给了 state，就不该再去探测")

        got = explain_action_availability(state(False, False, INTERACTIVE_DESKTOP_NAME), api=_NeverCalled())
        self.assertIs(got["drivable"], True)

    def test_locked_state_lists_what_will_fail(self):
        got = explain_action_availability(state(True, True, "Winlogon"))
        self.assertIs(got["drivable"], False)
        for name in ("read_elements", "keyboard_input", "mouse_click", "drag", "launch_app"):
            with self.subTest(capability=name):
                self.assertIs(got["capabilities"][name]["available"], False)
                self.assertIn(name, got["unavailable"])
        self.assertEqual(got["unknown"], ["screenshot"])

    def test_unknown_state_lists_nothing_as_unavailable(self):
        got = explain_action_availability(state(None, None))
        self.assertIsNone(got["drivable"])
        self.assertEqual(got["unavailable"], [])
        self.assertEqual(sorted(got["unknown"]), sorted(CAPABILITIES))

    def test_explicit_none_state_still_runs_the_probe(self):
        """`state=None` 是默认值，不能被当成"用户显式传了未知状态"。"""
        got = explain_action_availability(state=None, api=locked_api())
        self.assertIs(got["locked"], True)

    def test_malformed_state_does_not_crash(self):
        for bad in ("not a dict", 123, [], {}):
            with self.subTest(bad=bad):
                got = explain_action_availability(bad)
                self.assertFalse(got["ok"])
                self.assertIsNone(got["drivable"])

    def test_note_is_present(self):
        got = explain_action_availability(state(True, True, "Winlogon"))
        self.assertIsInstance(got["note"], str)
        self.assertTrue(got["note"])


# --------------------------------------------------------------------------
# 真机只读冒烟（唯一允许碰真实 API 的用例；环境不支持就 skip，不算失败）
# --------------------------------------------------------------------------


class TestRealMachineSmoke(unittest.TestCase):
    def test_detect_session_state_structure(self):
        if sys.platform != "win32":
            self.skipTest(f"非 Windows 平台（{sys.platform}），本机探测不适用")

        out = detect_session_state()
        self.assertIsInstance(out, dict)
        for key in ("ok", "locked", "input_desktop", "secure_desktop", "foreground_pid", "note"):
            self.assertIn(key, out)
        self.assertIsInstance(out["ok"], bool)
        self.assertIsInstance(out["note"], str)
        self.assertIsInstance(out["errors"], list)
        for key in ("locked", "secure_desktop"):
            self.assertIn(out[key], (True, False, None), key)
        self.assertIsInstance(out["probe"], dict)
        self.assertEqual(out["probe"]["handle_closed"] is None, not out["probe"]["input_desktop_opened"])

        if not out["ok"]:
            self.skipTest(f"本机探测拿不到信号（环境不支持）：{out['note']}")

        # 拿到输入桌面名就必须是字符串；名字是 Default 时判定必须自洽
        self.assertIsInstance(out["input_desktop"], str)
        if out["input_desktop"].lower() == INTERACTIVE_DESKTOP_NAME.lower():
            self.assertIs(out["locked"], False)
            self.assertIs(out["secure_desktop"], False)
        if out["locked"] is False:
            self.assertIs(out["secure_desktop"], False, "说不锁就必须同时说没有安全桌面")

    def test_explain_action_availability_structure(self):
        if sys.platform != "win32":
            self.skipTest(f"非 Windows 平台（{sys.platform}）")

        got = explain_action_availability()
        if not got["ok"]:
            self.skipTest(f"本机探测拿不到信号：{got['note']}")
        self.assertEqual(sorted(got["capabilities"]), sorted(CAPABILITIES))
        for name, entry in got["capabilities"].items():
            with self.subTest(capability=name):
                self.assertIn(entry["available"], (True, False, None))

    def test_real_probe_is_read_only(self):
        """只读保证：真机调用不得留下句柄（句柄泄漏是只读探测最容易犯的错）。"""
        if sys.platform != "win32":
            self.skipTest("非 Windows 平台")
        from winuse.core import session_state as mod

        api = mod.NativeSessionApi()
        first = detect_session_state(api)
        second = detect_session_state(api)
        # 两次探测的结果应一致（只读、无状态残留）
        self.assertEqual(first["locked"], second["locked"])
        self.assertEqual(first["input_desktop"], second["input_desktop"])


    def test_real_wts_fallback_path(self):
        """真机只读：强制主判据失败，验证**真** wtsapi32 的兜底路径。

        这是本模块风险最高的一段原生代码（`WTSSessionInfoEx` 的字段偏移是手写的），
        单测只能喂假字节，所以这里真调一次、真解一次：结构体偏移若写错，解出来的
        SessionFlags 基本不可能正好落在 0/1 上。

        只读：主判据被替换成"永远打不开"，其余全部继承真实实现；不切桌面、不锁屏。
        """
        if sys.platform != "win32":
            self.skipTest(f"非 Windows 平台（{sys.platform}）")
        from winuse.core import session_state as mod

        class _DeniedInputDesktop(mod.NativeSessionApi):
            """模拟真机实测的锁屏症状：普通权限打不开 Winlogon 桌面 → ACCESS_DENIED。"""

            def open_input_desktop(self) -> int:
                return 0

            def last_error(self) -> int:
                return mod.ERROR_ACCESS_DENIED

        out = detect_session_state(_DeniedInputDesktop())
        if out["probe"]["wts_session_flags"] is None:
            self.skipTest(f"本机 WTS 会话信息不可用：{out['probe']['wts_error']}")

        self.assertIsNone(out["input_desktop"])
        self.assertIsNone(out["secure_desktop"], "WTS 回答不了\"有没有安全桌面\"，必须是 None")
        self.assertEqual(out["locked_basis"], "wts_session_flags")
        self.assertEqual(out["probe"]["win32_error"], 5)
        self.assertIn("INPUT_DESKTOP_UNAVAILABLE", [e["code"] for e in out["errors"]])
        self.assertIn(out["probe"]["wts_session_flags"], (0, 1, 0xFFFFFFFF))

        got = explain_action_availability(out)
        for name, entry in got["capabilities"].items():
            with self.subTest(capability=name):
                if out["locked"] is False:
                    # 只证明"没锁"、没证明"输入桌面是我们的" → 不许报可用
                    self.assertIsNone(entry["available"], f"{name} 在证据不足时不许报 True")


if __name__ == "__main__":
    unittest.main(verbosity=2)
