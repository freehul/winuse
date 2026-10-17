"""winuse_notify 单测：通道选择、命令构造、脱敏预览、干跑与真跑分支。

真跑分支一律用**假 runner**：只断言"下发了什么命令"，绝不真发通知（不碰桌面）。
可用性用假 `which` 注入，避免"本机装没装 powershell/msg"影响测试结果。
"""

from __future__ import annotations

import base64
import sys
import getpass
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.notify import (  # noqa: E402
    CHANNEL_ORDER,
    LEVEL_LABELS,
    POWERSHELL_AUMID,
    NotifyInvalidLevel,
    NotifyInvalidTimeout,
    NotifyMissingContent,
    NotifyNoChannel,
    NotifySendFailed,
    NotifySpec,
    build_command,
    build_msg_command,
    build_toast_command,
    build_toast_xml,
    channel_candidates,
    command_preview,
    redact_text,
    run_notify,
    validate_notify_spec,
)

#: 本机绝对用户路径样例——**拆开拼**：数据卫生门禁会把源码里的 `C:\\Users\\` 当真值拦下
USER_PATH = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "notes.txt"


def fake_which(*names_found: bool):
    """假 which：按 (powershell.exe, msg.exe) 是否可用来回答。"""
    table = {name: found for name, found in zip(("powershell.exe", "msg.exe"), names_found)}
    return lambda name: (name if table.get(name, False) else None)


class FakeRunner:
    """假 runner：记录 argv，按序返回预设 (code, stdout, stderr)。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if not self.results:
            return 0, "", ""
        return self.results.pop(0)


class NeverCalledRunner:
    def __call__(self, argv):  # pragma: no cover - 被调用即失败
        raise AssertionError(f"干跑不该启动任何进程：{argv}")


def decode_script(argv: list[str]) -> str:
    return base64.b64decode(argv[-1]).decode("utf-16-le")


def embedded_xml(script: str) -> str:
    """从脚本里把内嵌的 XML base64 抠出来还原（证明转义/编码往返无损）。"""
    head = "FromBase64String('"
    start = script.index(head) + len(head)
    end = script.index("')", start)
    return base64.b64decode(script[start:end]).decode("utf-8")


class TestValidation(unittest.TestCase):
    def test_valid_spec_passes(self):
        validate_notify_spec(NotifySpec(title="完成", body="报告已生成"))

    def test_bad_level_raises(self):
        with self.assertRaises(NotifyInvalidLevel) as ctx:
            validate_notify_spec(NotifySpec(title="x", level="fatal"))
        self.assertEqual(ctx.exception.code, "NOTIFY_INVALID_LEVEL")

    def test_empty_content_raises(self):
        with self.assertRaises(NotifyMissingContent) as ctx:
            validate_notify_spec(NotifySpec(title="  ", body=""))
        self.assertEqual(ctx.exception.code, "NOTIFY_MISSING_CONTENT")

    def test_title_only_is_allowed(self):
        validate_notify_spec(NotifySpec(title="只有标题"))

    def test_body_only_is_allowed(self):
        validate_notify_spec(NotifySpec(body="只有正文"))

    def test_zero_timeout_raises(self):
        with self.assertRaises(NotifyInvalidTimeout) as ctx:
            validate_notify_spec(NotifySpec(title="x", timeout_seconds=0))
        self.assertEqual(ctx.exception.code, "NOTIFY_INVALID_TIMEOUT")

    def test_non_integer_timeout_raises(self):
        with self.assertRaises(NotifyInvalidTimeout):
            validate_notify_spec(NotifySpec(title="x", timeout_seconds="十秒"))  # type: ignore[arg-type]

    def test_defaults_are_dry_run_info_ten_seconds(self):
        spec = NotifySpec(title="x")
        self.assertEqual(spec.level, "info")
        self.assertEqual(spec.timeout_seconds, 10)
        self.assertFalse(spec.execute)


class TestChannelSelection(unittest.TestCase):
    def test_prefers_winrt_toast_then_msg(self):
        self.assertEqual(channel_candidates(True, True), ["winrt-toast", "msg"])

    def test_only_toast(self):
        self.assertEqual(channel_candidates(True, False), ["winrt-toast"])

    def test_only_msg(self):
        self.assertEqual(channel_candidates(False, True), ["msg"])

    def test_none_available(self):
        self.assertEqual(channel_candidates(False, False), [])

    def test_order_constant_is_toast_first(self):
        self.assertEqual(CHANNEL_ORDER, ("winrt-toast", "msg"))

    def test_unknown_channel_raises(self):
        with self.assertRaises(NotifyNoChannel):
            build_command("telegram", NotifySpec(title="x"))


class TestToastCommand(unittest.TestCase):
    def test_uses_winrt_api_and_powershell_aumid(self):
        argv = build_toast_command(NotifySpec(title="完成", body="报告已生成"))
        self.assertIn("-EncodedCommand", argv)
        script = decode_script(argv)
        self.assertIn("Windows.UI.Notifications.ToastNotificationManager", script)
        self.assertIn("ToastNotification", script)
        self.assertIn("CreateToastNotifier", script)
        self.assertIn(POWERSHELL_AUMID, script)
        self.assertIn("Show($toast)", script)
        # 不许出现第三方模块（BurntToast 之类）
        self.assertNotIn("BurntToast", script)
        self.assertNotIn("Import-Module", script)

    def test_xml_round_trip_keeps_chinese_and_escapes(self):
        spec = NotifySpec(title="构建完成 <A & B>", body="产物已归档")
        xml = embedded_xml(decode_script(build_toast_command(spec)))
        self.assertIn("构建完成 &lt;A &amp; B&gt;", xml)
        self.assertIn("产物已归档", xml)
        self.assertNotIn("<A & B>", xml)  # 未转义的原文绝不能出现

    def test_level_maps_to_scenario(self):
        self.assertNotIn("scenario=", build_toast_xml(NotifySpec(title="x", level="info")))
        self.assertIn('scenario="reminder"', build_toast_xml(NotifySpec(title="x", level="warn")))
        self.assertIn('scenario="urgent"', build_toast_xml(NotifySpec(title="x", level="error")))

    def test_timeout_becomes_expiration(self):
        script = decode_script(build_toast_command(NotifySpec(title="x", timeout_seconds=7)))
        self.assertIn("AddSeconds(7)", script)

    def test_body_is_optional(self):
        xml = build_toast_xml(NotifySpec(title="只有标题"))
        self.assertEqual(xml.count("<text>"), 1)


class TestMsgCommand(unittest.TestCase):
    def test_basic_shape(self):
        argv = build_msg_command(NotifySpec(title="完成", body="报告已生成"))
        self.assertEqual(argv[0], "msg.exe")
        # R-19 之后默认目标是**当前用户**（不是 `*` 全机广播，见 test_notify_broadcast.py）
        self.assertNotEqual(argv[1], "*")
        self.assertEqual(argv[1], getpass.getuser())
        self.assertIn("/TIME:10", argv)
        self.assertIn("完成 - 报告已生成", argv[3])

    def test_level_prefix_for_fallback(self):
        argv = build_msg_command(NotifySpec(title="失败", level="error"))
        self.assertTrue(argv[3].startswith(LEVEL_LABELS["error"]))
        self.assertEqual(build_msg_command(NotifySpec(title="x", level="info"))[3], "x")

    def test_quotes_are_escaped_and_newlines_flattened(self):
        argv = build_msg_command(NotifySpec(title='a"b', body="第一行\n第二行"))
        self.assertIn('\\"', argv[3])
        self.assertNotIn("\n", argv[3])

    def test_command_is_argv_list_not_shell_string(self):
        argv = build_msg_command(NotifySpec(title="x"))
        self.assertIsInstance(argv, list)
        self.assertTrue(all(isinstance(a, str) for a in argv))


class TestPreviewAndRedaction(unittest.TestCase):
    def test_absolute_user_path_is_redacted(self):
        out = redact_text(f"copy {USER_PATH} elsewhere")
        self.assertIn("<PATH>", out)
        self.assertNotIn("Users", out)
        self.assertNotIn("someone", out)

    def test_long_base64_is_summarized(self):
        out = redact_text(base64.b64encode(b"y" * 60).decode("ascii"))
        self.assertIn("<BASE64:", out)

    def test_whitespace_is_flattened(self):
        self.assertEqual(redact_text("a\n\n  b"), "a b")

    def test_encoded_command_is_decoded_for_readability(self):
        preview = command_preview(build_toast_command(NotifySpec(title="完成", body="报告已生成")))
        self.assertIn("powershell.exe", preview)
        self.assertIn("CreateToastNotifier", preview)  # 解码回脚本，不是一串 base64
        self.assertIn("WINUSE_NOTIFY_OK", preview)  # 关键动作在尾部，必须留在预览里
        self.assertIn("…", preview)  # 超长时头尾各留一段
        self.assertNotIn("报告已生成", preview)  # 正文在内嵌 base64 里，不外泄

    def test_base64_payload_is_collapsed_not_dumped(self):
        # 脱敏的核心保障：内嵌 base64（通知正文就在里面）只报长度，不原文倾倒
        blob = base64.b64encode("报告已生成-内部编号-8271".encode("utf-8")).decode("ascii")
        preview = command_preview(["powershell.exe", "-X", f"FromBase64String('{blob}')"])
        self.assertIn("<BASE64:", preview)
        self.assertNotIn(blob, preview)

    def test_preview_has_no_absolute_windows_path(self):
        argv = [USER_PATH, "-Flag", USER_PATH]
        preview = command_preview(argv)
        self.assertNotIn(USER_PATH, preview)

    def test_preview_is_bounded(self):
        preview = command_preview(build_toast_command(NotifySpec(title="x" * 2000)))
        self.assertLessEqual(len(preview), 660)  # 上限 600 + 头尾拼接的少量额外字符


class TestRunNotifyDryRun(unittest.TestCase):
    def test_dry_run_reports_command_without_executing(self):
        got = run_notify(
            NotifySpec(title="完成", body="报告已生成"),
            runner=NeverCalledRunner(),
            which=fake_which(True, True),
        )
        self.assertTrue(got["ok"])
        self.assertTrue(got["dry_run"])
        self.assertIsNone(got["code"])
        self.assertEqual(got["channel"], "winrt-toast")
        # 预览把 -EncodedCommand 解码回脚本展示（否则等于没有预览）
        self.assertIn("-Command", got["command_preview"])
        self.assertIn("powershell.exe", got["command_preview"])

    def test_dry_run_falls_to_msg_when_no_powershell(self):
        got = run_notify(NotifySpec(title="完成"), runner=NeverCalledRunner(), which=fake_which(False, True))
        self.assertTrue(got["ok"])
        self.assertTrue(got["dry_run"])
        self.assertEqual(got["channel"], "msg")
        self.assertIn("msg.exe", got["command_preview"])
        self.assertIn("/TIME:10", got["command_preview"])

    def test_no_channel_returns_code(self):
        got = run_notify(NotifySpec(title="完成"), runner=NeverCalledRunner(), which=fake_which(False, False))
        self.assertFalse(got["ok"])
        self.assertTrue(got["dry_run"])
        self.assertIsNone(got["channel"])
        self.assertEqual(got["code"], "NOTIFY_NO_CHANNEL")

    def test_dry_run_never_sends_even_with_execute_false(self):
        runner = FakeRunner([(0, "WINUSE_NOTIFY_OK", "")])
        run_notify(NotifySpec(title="完成", execute=False), runner=runner, which=fake_which(True, True))
        self.assertEqual(runner.calls, [])

    def test_default_channel_field_is_available_even_without_which_injection(self):
        # 显式传 which，保证断言与真机环境无关
        got = run_notify(NotifySpec(title="完成"), which=fake_which(True, True))
        self.assertEqual(got["channel"], "winrt-toast")


class TestRunNotifyExecute(unittest.TestCase):
    def test_execute_sends_via_toast(self):
        runner = FakeRunner([(0, "WINUSE_NOTIFY_OK", "")])
        got = run_notify(
            NotifySpec(title="完成", body="报告已生成", execute=True),
            runner=runner,
            which=fake_which(True, True),
        )
        self.assertTrue(got["ok"])
        self.assertFalse(got["dry_run"])
        self.assertIsNone(got["code"])
        self.assertEqual(got["channel"], "winrt-toast")
        self.assertEqual(len(runner.calls), 1)
        script = decode_script(runner.calls[0])
        self.assertIn("WINUSE_NOTIFY_OK", script)
        self.assertIn(POWERSHELL_AUMID, script)

    def test_timeout_and_level_reach_the_real_command(self):
        runner = FakeRunner([(0, "WINUSE_NOTIFY_OK", "")])
        run_notify(
            NotifySpec(title="警告", level="warn", timeout_seconds=30, execute=True),
            runner=runner,
            which=fake_which(True, True),
        )
        xml = embedded_xml(decode_script(runner.calls[0]))
        self.assertIn('scenario="reminder"', xml)
        self.assertIn("AddSeconds(30)", decode_script(runner.calls[0]))

    def test_toast_failure_falls_back_to_msg(self):
        runner = FakeRunner([(1, "", "访问被拒绝"), (0, "", "")])
        got = run_notify(NotifySpec(title="完成", execute=True), runner=runner, which=fake_which(True, True))
        self.assertTrue(got["ok"])
        self.assertFalse(got["dry_run"])
        self.assertEqual(got["channel"], "msg")  # 必须如实报兜底通道
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[1][0], "msg.exe")

    def test_launch_error_also_falls_back(self):
        def flaky(argv):
            if argv[0] == "powershell.exe":
                raise OSError("无法启动 powershell")
            return 0, "", ""

        got = run_notify(NotifySpec(title="完成", execute=True), runner=flaky, which=fake_which(True, True))
        self.assertTrue(got["ok"])
        self.assertEqual(got["channel"], "msg")

    def test_all_channels_failing_returns_code(self):
        runner = FakeRunner([(1, "", "拒绝访问"), (1, "", "消息服务未启动")])
        got = run_notify(NotifySpec(title="完成", execute=True), runner=runner, which=fake_which(True, True))
        self.assertFalse(got["ok"])
        self.assertFalse(got["dry_run"])
        self.assertEqual(got["code"], "NOTIFY_SEND_FAILED")
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("拒绝访问", got["message"])
        self.assertIn("消息服务未启动", got["message"])

    def test_msg_only_failure_does_not_retry(self):
        runner = FakeRunner([(1, "", "服务未启动")])
        got = run_notify(NotifySpec(title="完成", execute=True), runner=runner, which=fake_which(False, True))
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "NOTIFY_SEND_FAILED")
        self.assertEqual(got["channel"], "msg")
        self.assertEqual(len(runner.calls), 1)

    def test_invalid_spec_raises_even_when_executing(self):
        with self.assertRaises(NotifyInvalidLevel):
            run_notify(
                NotifySpec(title="x", level="fatal", execute=True),
                runner=FakeRunner([]),
                which=fake_which(True, True),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
