"""PR-8 接线验收：CLI / MCP / Hermes 插件三面共用同一份核心。

这层测试专门盯"接线漂移"——历史上出过：门面各写一份 schema、CLI 少了某个子命令、
工具清单被写死数量。故这里的断言一律**以工具表常量为准**，不写死数字。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

from winuse import cli
from winuse.core import ocr as ocr_mod
from winuse.mcp import server as S

BRIDGE_DIR = Path(__file__).resolve().parents[1] / "adapters" / "hermes"

#: 每个工具的最小可运行参数（用于"工具 → CLI 子命令"逐项对照）
MIN_ARGS = {
    "winuse_ocr": {"image_path": "D:/x.png"},
    "winuse_act": {"action": "click"},
    "winuse_wait": {"kind": "window_appear"},
    "winuse_multi": {"steps": [{"action": "click"}]},
    "winuse_window": {"action": "minimize"},
}


def _load_bridge():
    """按普通模块加载桥接（与适配器测试的包加载方式互补）。"""
    if str(BRIDGE_DIR) not in sys.path:
        sys.path.insert(0, str(BRIDGE_DIR))
    import core_bridge  # type: ignore

    return core_bridge


class CliRegistrationTests(unittest.TestCase):
    def test_new_capability_commands_are_registered(self) -> None:
        for cmd in (
            "ocr", "notify", "system", "registry", "vdesktop",
            "wait", "multi", "window", "tools", "audit",
        ):
            with self.subTest(cmd=cmd):
                with self.assertRaises(SystemExit) as cm:
                    with contextlib.redirect_stdout(io.StringIO()):
                        cli.main([cmd, "--help"])
                self.assertEqual(cm.exception.code, 0, f"{cmd} 子命令应存在")


class ToolTableTests(unittest.TestCase):
    def test_catalog_matches_tool_names(self) -> None:
        self.assertEqual({t["name"] for t in S.tool_catalog()}, set(S.TOOL_NAMES))

    def test_every_tool_has_handler_and_valid_schema(self) -> None:
        self.assertEqual(set(S.TOOL_NAMES), set(S._TOOL_HANDLERS), "工具表与处理函数必须一一对应")
        for tool in S.tool_catalog():
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"])
                self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_cli_tools_command_exports_same_table(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["tools"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["count"], len(S.TOOL_NAMES))
        self.assertEqual({t["name"] for t in payload["tools"]}, set(S.TOOL_NAMES))


class BridgeArgvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge = _load_bridge()

    def test_every_tool_maps_to_an_existing_cli_command(self) -> None:
        """防漂移：每个工具都必须能翻成一个真实存在的 CLI 子命令。"""
        for tool in S.TOOL_NAMES:
            with self.subTest(tool=tool):
                argv = self.bridge.build_args(tool, dict(MIN_ARGS.get(tool, {})))
                with self.assertRaises(SystemExit) as cm:
                    with contextlib.redirect_stdout(io.StringIO()):
                        cli.main([argv[0], "--help"])
                self.assertEqual(cm.exception.code, 0, f"{tool} → {argv[0]} 子命令不存在")

    def test_ocr_argv(self) -> None:
        self.assertEqual(
            self.bridge.build_args("winuse_ocr", {"image_path": "D:/a.png", "language": "zh-Hans"}),
            ["ocr", "--image", "D:/a.png", "--language", "zh-Hans"],
        )

    def test_window_close_carries_confirm_and_execute(self) -> None:
        argv = self.bridge.build_args("winuse_window", {"action": "close", "app": "记事本", "confirm": True})
        self.assertEqual(argv[:3], ["window", "--action", "close"])
        self.assertIn("--confirm", argv)
        self.assertNotIn("--execute", argv, "没传 execute 就必须保持干跑")

    def test_multi_serializes_steps_and_stop_flag(self) -> None:
        argv = self.bridge.build_args(
            "winuse_multi", {"steps": [{"action": "click"}], "confirm": True, "stop_on_error": False}
        )
        self.assertEqual(argv[:2], ["multi", "--steps"])
        self.assertEqual(json.loads(argv[2]), [{"action": "click"}])
        self.assertIn("--confirm", argv)
        self.assertIn("--no-stop-on-error", argv)

    def test_registry_write_intent_flags(self) -> None:
        argv = self.bridge.build_args(
            "winuse_registry", {"hive": "HKCU", "path": "Software", "write": True, "value_name": "x", "value": "1"}
        )
        self.assertEqual(argv[:2], ["registry", "--hive"])
        self.assertIn("--write", argv)
        self.assertIn("--value-name", argv)

    def test_unknown_tool_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.bridge.build_args("winuse_nope", {})


class OcrScriptTests(unittest.TestCase):
    def test_script_normalizes_path_separators(self) -> None:
        """真机坑：WinRT 的 GetFileFromPathAsync 不认正斜杠，脚本必须规范化。"""
        script = ocr_mod.build_ocr_script(ocr_mod.OcrSpec(image_path="D:/tmp/a.png"))
        self.assertIn("GetFullPath", script)


if __name__ == "__main__":
    unittest.main()
