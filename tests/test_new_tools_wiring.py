"""新能力接线测试（P0-4 / P1-1 / P1-2 / P1-3 / Everything 搜索）。

背景：这六个模块由并行子代理实现，按纪律**只写自己的 core 模块 + 测试**，
把它们接进出口（工具表 / CLI / Hermes 桥接）是主 agent 的活。本文件就是把
"接上了没有"变成可执行的断言——否则会出现"模块 + 测试全绿，但没人能调用"的
假完成（能力有、出口没有，正是这个项目反复踩的坑）。

覆盖两条链：
1. **MCP 链**：tool_catalog() 声明 → handler 分发 → 落到正确的驱动调用
2. **桥接链**：Hermes 工具参数 → 核心 CLI argv（缺一项就在宿主侧丢一项）

以及 CLI 侧：子命令真的注册进了 parser。
"""

from __future__ import annotations

import argparse
import json
import unittest
from dataclasses import dataclass, field

from winuse import cli_capabilities
from winuse.mcp import server as S

WINDOW = {
    "pid": 100,
    "window_id": 11,
    "app_name": "fake.exe",
    "title": "假窗口",
    "minimized": False,
    "is_on_screen": True,
    "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
}

#: 6 个新工具（工具名 → 至少应声明的参数）
NEW_TOOLS = {
    "winuse_screenshot": ("mode",),
    "winuse_drag": (),
    "winuse_app": ("action",),
    "winuse_search": (),
    "winuse_menu": ("path",),
    "winuse_verify": ("predicates",),
}


@dataclass
class RecordingDriver:
    windows: list = field(default_factory=lambda: [dict(WINDOW)])
    state: dict = field(
        default_factory=lambda: {
            "elements": [
                {
                    "index": 0,
                    "token": "s00000001:0",
                    "role": "Button",
                    "label": "确定",
                    "actions": ["invoke"],
                    "depth": 3,
                    "frame": {"x": 10, "y": 20, "width": 60, "height": 30},
                }
            ],
            "snapshot_id": "s00000001",
            "screenshot_width": 800,
            "screenshot_height": 600,
        }
    )
    calls: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def call(self, tool: str, payload: dict | None = None):
        self.calls.append((tool, dict(payload or {})))
        if tool == "list_windows":
            return {"windows": self.windows, "ok": True}
        if tool == "get_window_state":
            return dict(self.state, ok=True)
        if tool in self.extra:
            return self.extra[tool]
        return {"ok": True}

    def health(self):
        return {"ok": True, "path": "<fake>", "version": "0.0.0"}


def _call(driver, tool: str, arguments: dict, *, cfg=None) -> dict:
    session = S.Session(driver=driver, initialized=True, cfg=cfg)
    out = S.handle_message(
        session,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    return json.loads(out["result"]["content"][0]["text"])


def _last(driver, tool: str) -> dict:
    hits = [p for n, p in driver.calls if n == tool]
    assert hits, f"驱动没收到 {tool}；实际：{[n for n, _ in driver.calls]}"
    return hits[-1]


def _schema(tool: str) -> dict:
    entry = next(t for t in S.tool_catalog() if t["name"] == tool)
    return entry["inputSchema"]


class CatalogDeclaresNewTools(unittest.TestCase):
    def test_all_new_tools_are_declared(self) -> None:
        names = {t["name"] for t in S.tool_catalog()}
        for tool in NEW_TOOLS:
            self.assertIn(tool, names, f"{tool} 必须出现在工具表里")
        self.assertEqual(names, set(S._TOOL_HANDLERS), "工具表与处理函数必须一一对应")

    def test_required_fields(self) -> None:
        for tool, required in NEW_TOOLS.items():
            with self.subTest(tool=tool):
                schema = _schema(tool)
                self.assertEqual(schema["type"], "object")
                for name in required:
                    self.assertIn(name, schema.get("required", []), f"{tool} 必须要求 {name}")

    def test_enum_from_core(self) -> None:
        """枚举必须从核心常量生成（手写一份必然漂移）。"""
        from winuse.core import screenshot

        self.assertEqual(_schema("winuse_screenshot")["properties"]["mode"]["enum"], list(screenshot.MODES))
        from winuse.core import app as app_mod

        self.assertEqual(
            sorted(_schema("winuse_app")["properties"]["action"]["enum"]),
            sorted(app_mod.APP_ACTIONS),
        )

    def test_schemas_are_json_serializable(self) -> None:
        json.dumps(S.tool_catalog(), ensure_ascii=False)


class ScreenshotToolReachesDriver(unittest.TestCase):
    def test_window_mode_asks_driver_for_pixels(self) -> None:
        drv = RecordingDriver()
        drv.extra["get_window_state"] = dict(
            RecordingDriver().state,
            ok=True,
            screenshot_png_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==",
        )
        out = _call(drv, "winuse_screenshot", {"mode": "window", "app": "fake"})
        call = _last(drv, "get_window_state")
        self.assertTrue(call.get("include_screenshot"), "截图模式必须向驱动要像素")
        self.assertIn("path", out, f"应回报落盘路径，收到：{out}")

    def test_unknown_mode_is_rejected_before_driver(self) -> None:
        drv = RecordingDriver()
        out = S.handle_message(
            S.Session(driver=drv, initialized=True),
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "winuse_screenshot", "arguments": {"mode": "nope"}},
            },
        )
        self.assertIn("error", out, "非法 mode 必须被拒（参数级错误）")
        self.assertEqual(drv.calls, [], "参数错就不该碰驱动")


class DragToolReachesDriver(unittest.TestCase):
    def test_point_drag_dry_run_by_default(self) -> None:
        drv = RecordingDriver()
        out = _call(
            drv,
            "winuse_drag",
            {
                "app": "fake",
                "from_point": {"x": 10, "y": 10},
                "to_point": {"x": 100, "y": 100},
            },
        )
        self.assertTrue(out.get("ok"), out)
        self.assertFalse([n for n, _ in drv.calls if n == "drag"], "默认干跑不许下发 drag")

    def test_point_drag_executes_with_flag(self) -> None:
        drv = RecordingDriver()
        _call(
            drv,
            "winuse_drag",
            {
                "app": "fake",
                "from_point": {"x": 10, "y": 10},
                "to_point": {"x": 100, "y": 100},
                "execute": True,
            },
        )
        call = _last(drv, "drag")
        self.assertEqual((call["from_x"], call["from_y"]), (10, 10))
        self.assertEqual((call["to_x"], call["to_y"]), (100, 100))


class AppToolReachesDriver(unittest.TestCase):
    def test_list_needs_execute_and_hits_driver(self) -> None:
        drv = RecordingDriver()
        drv.extra["list_apps"] = {"ok": True, "apps": [{"name": "记事本", "pid": 1, "running": True}]}
        out = _call(drv, "winuse_app", {"action": "list", "execute": True})
        self.assertTrue(out.get("ok"), out)
        self.assertTrue([n for n, _ in drv.calls if n == "list_apps"])

    def test_kill_without_confirm_is_blocked(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_app", {"action": "kill", "pid": 123, "execute": True})
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("code"), "CONFIRM_REQUIRED")
        self.assertFalse([n for n, _ in drv.calls if n == "kill_app"], "未确认不许下发 kill")

    def test_launch_forwards_path(self) -> None:
        drv = RecordingDriver()
        _call(drv, "winuse_app", {"action": "launch", "path": "C:/Windows/System32/notepad.exe", "execute": True})
        call = _last(drv, "launch_app")
        self.assertIn("notepad", json.dumps(call, ensure_ascii=False))


class MenuToolReachesDriver(unittest.TestCase):
    def test_menu_requires_confirm_even_for_dry_run(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_menu", {"app": "fake", "path": ["文件", "保存"]})
        self.assertFalse(out.get("ok"))
        self.assertTrue(out.get("requires_confirm"))
        self.assertFalse([n for n, _ in drv.calls if n == "invoke_menu"])

    def test_menu_confirmed_dry_run_shows_plan(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_menu", {"app": "fake", "path": ["文件", "保存"], "confirm": True})
        self.assertTrue(out.get("ok"), out)
        self.assertFalse([n for n, _ in drv.calls if n == "invoke_menu"], "确认后仍应默认干跑")


class VerifyToolReachesDriver(unittest.TestCase):
    def test_verify_dry_run(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_verify", {"app": "fake", "predicates": [{"window": {"exists": True}}]})
        self.assertTrue(out.get("ok"), out)
        self.assertFalse([n for n, _ in drv.calls if n == "verify_state"], "默认干跑")

    def test_verify_executes_with_flag(self) -> None:
        drv = RecordingDriver()
        drv.extra["verify_state"] = {"ok": True, "status": "satisfied", "stable": True}
        _call(
            drv,
            "winuse_verify",
            {"app": "fake", "predicates": [{"window": {"exists": True}}], "execute": True},
        )
        self.assertTrue([n for n, _ in drv.calls if n == "verify_state"])


class SearchToolWorksWithoutDriver(unittest.TestCase):
    """搜索不需要驱动——没有驱动的会话也必须能用（别的工具都会被 _require_driver 拦）。"""

    def test_missing_es_reports_tool_absence_not_empty_result(self) -> None:
        out = S.handle_message(
            S.Session(driver=None, initialized=True),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "winuse_search",
                    "arguments": {"query": "*.md", "es_path": "Z:/definitely/not/here/es.exe"},
                },
            },
        )
        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ES_NOT_FOUND")


class BridgeTranslatesNewTools(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_hermes_adapter import bridge_module

        self.build = bridge_module().build_args

    def _argv(self, tool: str, args: dict) -> list[str]:
        argv = self.build(tool, args)
        assert "--" not in argv or argv[0] not in ("--",), argv
        return argv

    def test_screenshot(self) -> None:
        argv = self._argv("winuse_screenshot", {"mode": "window", "app": "x", "max_dimension": 640})
        self.assertEqual(argv[0], "screenshot")
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "window")
        self.assertEqual(argv[argv.index("--max-dimension") + 1], "640")

    def test_drag(self) -> None:
        argv = self._argv(
            "winuse_drag",
            {"app": "x", "from_point": {"x": 1, "y": 2}, "to_point": {"x": 3, "y": 4}, "execute": True},
        )
        self.assertEqual(argv[0], "drag")
        self.assertIn("--execute", argv)
        self.assertIn("--from-point", argv)

    def test_app(self) -> None:
        argv = self._argv("winuse_app", {"action": "launch", "path": "C:/a.exe", "execute": True})
        self.assertEqual(argv[0], "app")
        self.assertEqual(argv[argv.index("--action") + 1], "launch")

    def test_app_switch_args_use_equals_form(self) -> None:
        """T-60：附加参数必须逐个以 --arg=<值> 形状传给 CLI。

        --args 是 nargs="*"，--arg 用空格分隔也一样：argparse 会把以 "-" 开头的开关
        当成选项，直接报 unrecognized arguments（真机实测过，见 Backlog T-60）。等号
        形式不会被当成选项解析，是唯一安全的路子。
        """
        argv = self._argv(
            "winuse_app",
            {"action": "launch", "path": "x.exe", "args": ["-NoProfile", "-File", "s.ps1"]},
        )
        self.assertNotIn("--args", argv)
        self.assertIn("--arg=-NoProfile", argv)
        self.assertIn("--arg=-File", argv)
        self.assertIn("--arg=s.ps1", argv)
        # 每个附加参数都必须自带等号，不能出现裸的 "--arg"（裸形式会被 argparse 拒）
        self.assertNotIn("--arg", argv)

    def test_search(self) -> None:
        argv = self._argv("winuse_search", {"query": "*.md", "max_results": 5, "min_size": "10kb"})
        self.assertEqual(argv[0], "search")
        self.assertEqual(argv[argv.index("--max-results") + 1], "5")
        self.assertEqual(argv[argv.index("--min-size") + 1], "10kb")

    def test_menu(self) -> None:
        argv = self._argv("winuse_menu", {"app": "x", "path": ["文件", "保存"], "confirm": True})
        self.assertEqual(argv[0], "menu")
        self.assertIn("--path", argv)
        self.assertIn("--confirm", argv)

    def test_verify(self) -> None:
        preds = [{"window": {"exists": True}}]
        argv = self._argv("winuse_verify", {"app": "x", "predicates": preds, "stable_samples": 3})
        self.assertEqual(argv[0], "verify")
        self.assertIn("--predicate", argv)
        self.assertEqual(argv[argv.index("--stable-samples") + 1], "3")


class AppArgOptionReachesParser(unittest.TestCase):
    """T-60：launch 的开关参数要能通过 CLI 传进去（等号形式），旧形式不许破。"""

    def _parse(self, argv: list[str]):
        parser = argparse.ArgumentParser(prog="winuse")
        sub = parser.add_subparsers(dest="cmd")
        cli_capabilities.register(sub)
        return parser.parse_args(argv)

    def test_arg_equals_form_parses(self) -> None:
        ns = self._parse(["app", "--action", "launch", "--path", "x.exe", "--arg=-NoProfile", "--arg=-File"])
        self.assertEqual(ns.arg, ["-NoProfile", "-File"])

    def test_args_form_still_parses(self) -> None:
        ns = self._parse(["app", "--action", "launch", "--path", "x.exe", "--args", "a", "b"])
        self.assertEqual(ns.args, ["a", "b"])
        self.assertIsNone(ns.arg)


class CliRegistersNewCommands(unittest.TestCase):
    def _choices(self) -> set[str]:
        parser = argparse.ArgumentParser(prog="winuse")
        sub = parser.add_subparsers(dest="cmd")
        cli_capabilities.register(sub)
        return set(sub.choices)

    def test_commands_exist(self) -> None:
        choices = self._choices()
        for cmd in ("screenshot", "drag", "app", "search", "menu", "verify"):
            self.assertIn(cmd, choices, f"cli_capabilities 必须注册 {cmd} 子命令")


if __name__ == "__main__":
    unittest.main()
