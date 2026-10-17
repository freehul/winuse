"""动作出口参数（button / modifiers / by）的导出与转发测试。

背景（P0 出口缺口，2026-09-27）：
这三个参数其实**核心（core.act.ActSpec）与 CLI 层早已支持**——
`winuse/cli.py` 的 act 子命令有 `--button` / `--modifiers` / `--by`，
`build_act_call()` 也会把它们翻译成驱动参数。断线断在出口这一段：

1. `winuse/mcp/server.py` 的 `winuse_act` **工具表没声明**这三个字段
   → 客户端按 schema 生成调用，看不到就永远不会传（能力不可发现）；
2. `_tool_act` 只读了 modifiers（且按逗号串解析），**button / by 根本没转发**
   → 即使调用方硬传也会被静默丢弃；
3. `adapters/hermes/core_bridge.py` 的 `build_args` 没有这三项翻译
   → Hermes 侧走桥接时同样丢参数。

本测试把"工具表 → handler → 驱动参数"与"桥接 → CLI 参数"两条链都锁死，
防止再次出现"能力有、出口没有"的半新错位。
"""

from __future__ import annotations

import importlib
import json
import unittest
from dataclasses import dataclass, field

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


@dataclass
class RecordingDriver:
    """记录全部驱动调用的假驱动（不碰真桌面）。"""

    windows: list = field(default_factory=lambda: [dict(WINDOW)])
    state: dict = field(default_factory=lambda: {"elements": [], "snapshot_id": "s00000001"})
    calls: list = field(default_factory=list)

    def call(self, tool: str, payload: dict | None = None):
        self.calls.append((tool, dict(payload or {})))
        if tool == "list_windows":
            return {"windows": self.windows, "ok": True}
        if tool == "get_window_state":
            return dict(self.state, ok=True)
        return {"ok": True}

    def health(self):
        return {"ok": True, "path": "<fake>", "version": "0.0.0"}


def _session(driver: RecordingDriver) -> "S.Session":
    return S.Session(driver=driver, initialized=True)


def _call(driver: RecordingDriver, arguments: dict) -> dict:
    """按 MCP 协议真实走一遍 tools/call，返回解析后的工具回执。"""
    out = S.handle_message(
        _session(driver),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "winuse_act", "arguments": arguments},
        },
    )
    return json.loads(out["result"]["content"][0]["text"])


def _last(driver: RecordingDriver, tool: str) -> dict:
    hits = [payload for name, payload in driver.calls if name == tool]
    assert hits, f"驱动没有收到 {tool} 调用；实际调用：{[n for n, _ in driver.calls]}"
    return hits[-1]


def _act_schema() -> dict:
    tools = S.tool_catalog()
    entry = next(t for t in tools if t["name"] == "winuse_act")
    return entry["inputSchema"]


class ActSchemaDeclaresParams(unittest.TestCase):
    """工具表必须声明这三个字段——否则客户端不可发现（P0-1/2/3）。"""

    def test_button_enum(self) -> None:
        prop = _act_schema()["properties"].get("button")
        self.assertIsNotNone(prop, "winuse_act 必须声明 button（右键/中键点击）")
        self.assertEqual(prop.get("enum"), ["left", "right", "middle"])

    def test_modifiers_declared(self) -> None:
        prop = _act_schema()["properties"].get("modifiers")
        self.assertIsNotNone(prop, "winuse_act 必须声明 modifiers（组合键）")
        # 数组形式最贴合 MCP 惯例；字符串逗号串也必须兼容（见转发测试）
        self.assertIn(prop.get("type"), ("array", "string"))

    def test_by_enum(self) -> None:
        prop = _act_schema()["properties"].get("by")
        self.assertIsNotNone(prop, "winuse_act 必须声明 by（滚动粒度）")
        self.assertEqual(prop.get("enum"), ["line", "page"])


class ActForwardsParamsToDriver(unittest.TestCase):
    """handler 必须把参数真转发到驱动（不是只写在 schema 里撑门面）。"""

    def test_right_button_reaches_driver(self) -> None:
        drv = RecordingDriver()
        out = _call(
            drv,
            {
                "action": "click",
                "app": "fake",
                "element_token": "s00000001:1",
                "button": "right",
                "execute": True,
            },
        )
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(_last(drv, "click").get("button"), "right")

    def test_middle_button_reaches_driver(self) -> None:
        drv = RecordingDriver()
        _call(
            drv,
            {
                "action": "click",
                "app": "fake",
                "element_token": "s00000001:1",
                "button": "middle",
                "execute": True,
            },
        )
        self.assertEqual(_last(drv, "click").get("button"), "middle")

    def test_scroll_page_granularity_reaches_driver(self) -> None:
        drv = RecordingDriver()
        _call(
            drv,
            {
                "action": "scroll",
                "app": "fake",
                "element_token": "s00000001:1",
                "direction": "down",
                "amount": 1,
                "by": "page",
                "execute": True,
            },
        )
        self.assertEqual(_last(drv, "scroll").get("by"), "page")

    def test_modifiers_array_reaches_driver(self) -> None:
        """按 schema 传数组：必须原样成为驱动的 modifiers 数组。"""
        drv = RecordingDriver()
        _call(
            drv,
            {
                "action": "key",
                "app": "fake",
                "element_token": "s00000001:1",
                "key": "c",
                "modifiers": ["ctrl"],
                "execute": True,
            },
        )
        self.assertEqual(_last(drv, "press_key").get("modifiers"), ["ctrl"])

    def test_modifiers_comma_string_still_works(self) -> None:
        """兼容手写逗号串（旧调用方式不许因为加了数组支持而失效）。"""
        drv = RecordingDriver()
        _call(
            drv,
            {
                "action": "key",
                "app": "fake",
                "element_token": "s00000001:1",
                "key": "s",
                "modifiers": "ctrl,shift",
                "execute": True,
            },
        )
        self.assertEqual(_last(drv, "press_key").get("modifiers"), ["ctrl", "shift"])

    def test_dry_run_does_not_touch_desktop(self) -> None:
        """默认干跑：参数看得到，但一个变更动作都不许下发。"""
        drv = RecordingDriver()
        out = _call(
            drv,
            {
                "action": "click",
                "app": "fake",
                "element_token": "s00000001:1",
                "button": "right",
            },
        )
        self.assertTrue(out.get("ok"), out)
        self.assertFalse([n for n, _ in drv.calls if n == "click"], "干跑不许下发 click")


ADAPTER_DIR = None


def _bridge():
    """加载 Hermes 桥接子模块（复用插件命名空间包的官方加载姿势）。"""
    from tests.test_hermes_adapter import bridge_module

    return bridge_module()


class BridgeForwardsActParams(unittest.TestCase):
    """桥接层：MCP 参数 → 核心 CLI 参数，缺一项就在 Hermes 侧丢一项。"""

    def setUp(self) -> None:
        importlib.invalidate_caches()
        self.build = _bridge().build_args

    def test_button_by_modifiers_translated(self) -> None:
        argv = self.build(
            "winuse_act",
            {
                "action": "click",
                "app": "x",
                "element_index": 0,
                "snapshot": "s1",
                "button": "right",
                "by": "page",
                "modifiers": ["ctrl", "shift"],
            },
        )
        self.assertIn("--button", argv)
        self.assertEqual(argv[argv.index("--button") + 1], "right")
        self.assertIn("--by", argv)
        self.assertEqual(argv[argv.index("--by") + 1], "page")
        self.assertIn("--modifiers", argv)
        self.assertEqual(argv[argv.index("--modifiers") + 1], "ctrl,shift")

    def test_modifiers_string_passthrough(self) -> None:
        argv = self.build(
            "winuse_act",
            {"action": "key", "app": "x", "element_token": "t", "key": "v", "modifiers": "ctrl"},
        )
        self.assertEqual(argv[argv.index("--modifiers") + 1], "ctrl")

    def test_omitted_params_stay_omitted(self) -> None:
        """没传就不许塞默认值——核心自己管默认，桥接不该替它做主。"""
        argv = self.build("winuse_act", {"action": "click", "app": "x", "element_index": 0, "snapshot": "s1"})
        self.assertNotIn("--button", argv)
        self.assertNotIn("--by", argv)
        self.assertNotIn("--modifiers", argv)


if __name__ == "__main__":
    unittest.main()
