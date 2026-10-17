"""R-19：通知级别口径统一 + `msg.exe *` 广播收紧。

两个问题：
1. 核心级别是 `info/warn/error`，而 MCP 与 CLI 对外声明 `info/warning/error`
   → 上游按 schema 传 `warning` 会稳定拿到 `NOTIFY_INVALID_LEVEL`（契约打架）。
2. `build_msg_command` 固定用 `msg.exe *`（本机**全部**会话）——toast 不可用时，
   通知正文会出现在 RDP / 快速用户切换 / 多用户会话的屏幕上。

处置（用户拍板）：级别统一（`warning` 作为 `warn` 的别名）；msg 默认只发当前会话，
广播改成显式 `broadcast=true` + 确认门。
"""

from __future__ import annotations

import getpass
import unittest

from winuse.core import guard as guard_mod
from winuse.core import notify as notify_mod
from winuse.core.notify import NotifySpec, build_msg_command, run_notify


def _runner_recorder():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return 0, "", ""

    return calls, run


class TestLevelAlias(unittest.TestCase):
    def test_warning_is_accepted_as_warn_alias(self):
        """对外声明的 `warning` 必须能用——别名归一，不是照单拒绝。"""
        out = notify_mod.normalize_level("warning")
        self.assertEqual(out, "warn")

    def test_canonical_levels_pass_through(self):
        for level in ("info", "warn", "error"):
            self.assertEqual(notify_mod.normalize_level(level), level)

    def test_level_is_case_insensitive_and_trimmed(self):
        self.assertEqual(notify_mod.normalize_level(" Warning "), "warn")

    def test_unknown_level_still_refused(self):
        with self.assertRaises(notify_mod.NotifyInvalidLevel):
            notify_mod.normalize_level("fatal")

    def test_warning_level_actually_sends_with_warn_label(self):
        calls, run = _runner_recorder()
        out = run_notify(
            NotifySpec(title="占位标题", level="warning", execute=True),
            runner=run,
            which=lambda exe: exe.endswith("powershell.exe"),
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["level"], "warn", "回执要回归一后的规范级别")


class TestMsgTarget(unittest.TestCase):
    def test_default_targets_current_user_not_all_sessions(self):
        argv = build_msg_command(NotifySpec(title="占位", level="info"))
        self.assertNotEqual(argv[1], "*", "默认绝不能广播给全部会话")
        self.assertEqual(argv[1], getpass.getuser())

    def test_broadcast_flag_targets_all_sessions(self):
        argv = build_msg_command(NotifySpec(title="占位", level="info", broadcast=True))
        self.assertEqual(argv[1], "*")

    def test_time_switch_still_present(self):
        argv = build_msg_command(NotifySpec(title="占位", timeout_seconds=7))
        self.assertIn("/TIME:7", argv)


class TestBroadcastGate(unittest.TestCase):
    def test_broadcast_without_confirmation_is_refused_and_sends_nothing(self):
        calls, run = _runner_recorder()
        out = run_notify(
            NotifySpec(title="占位", body="正文", broadcast=True, execute=True),
            runner=run,
            which=lambda exe: exe.endswith("powershell.exe"),
        )
        self.assertFalse(out["ok"])
        self.assertTrue(out["requires_confirm"])
        self.assertEqual(out["error"]["code"], "CONFIRM_REQUIRED")
        self.assertEqual(calls, [], "未确认的广播一个进程都不许起")

    def test_broadcast_with_confirmation_sends(self):
        calls, run = _runner_recorder()
        out = run_notify(
            NotifySpec(title="占位", body="正文", broadcast=True, execute=True),
            runner=run,
            which=lambda exe: exe.endswith("msg.exe"),
            confirmed=True,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(calls[0][1], "*", "确认后必须真广播（不是谁都发不出去）")

    def test_plain_notify_needs_no_confirmation(self):
        calls, run = _runner_recorder()
        out = run_notify(
            NotifySpec(title="占位", execute=True),
            runner=run,
            which=lambda exe: exe.endswith("msg.exe"),
        )
        self.assertTrue(out["ok"])
        self.assertNotEqual(calls[0][1], "*")

    def test_broadcast_verdict_has_its_own_rule(self):
        verdict = guard_mod.assess_notify_broadcast(True)
        self.assertTrue(verdict.dangerous)
        self.assertEqual(verdict.rule, guard_mod.NOTIFY_BROADCAST_RULE)
        self.assertFalse(guard_mod.assess_notify_broadcast(False).dangerous)


if __name__ == "__main__":
    unittest.main(verbosity=2)
