"""P2 新能力接线测试（等待族 element_gone / 树指纹 / 模糊查找 / 批量读值 / 剪贴板 / 会话状态）。

与 test_new_tools_wiring.py 同构：这些能力由并行子代理实现（按纪律只写自己的 core 模块 + 测试），
把它们接进出口（工具表 / CLI / Hermes 桥接）是主 agent 的活。本文件把「接上了没有」变成可执行断言，
否则会出现「模块 + 测试全绿，但没人能调用」的假完成。

三条链：
1. **MCP 链**：工具表声明 → handler 分发 → 落到正确的驱动调用（或明确**不**碰驱动）
2. **桥接链**：Hermes 工具参数 → 核心 CLI argv（缺一项就在宿主侧丢一项）
3. **CLI 侧**：子命令与 wait 的新参数真的注册进了 parser

负向断言比正向更重要，所以这里专门覆盖：
- 剪贴板读正文/写未确认时**零下发**（一次驱动调用都不能发生）
- 会话状态**不要求驱动**（它回答的是「驱动还有没有用」）
"""

from __future__ import annotations

import argparse
import json
import unittest
from dataclasses import dataclass, field

from winuse import cli_capabilities
from winuse.core import treehash as treehash_mod
from winuse.core import wait_for as wait_mod
from winuse.mcp import server as S

WINDOW = {
    "pid": 100,
    "window_id": 11,
    "app_name": "fake.exe",
    "title": "假窗口",
    "minimized": False,
    "is_on_screen": True,
    "z_index": 0,
    "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
}

#: P2 新增的 4 个工具（工具名 → 至少应声明的必填参数）
P2_TOOLS = {
    "winuse_find": ("text",),
    "winuse_read": ("elements",),
    "winuse_clipboard": ("action",),
    "winuse_session": (),
}

ELEMENTS = [
    {
        "index": 0,
        "token": "s00000001:0",
        "role": "Button",
        "label": "确定",
        "actions": ["invoke"],
        "enabled": True,
        "depth": 3,
        "frame": {"x": 10, "y": 20, "width": 60, "height": 30},
    },
    {
        "index": 1,
        "token": "s00000001:1",
        "role": "ListItem",
        "label": "修改日期▲",
        "actions": ["select"],
        "enabled": True,
        "depth": 4,
        "frame": {"x": 90, "y": 20, "width": 80, "height": 30},
    },
]


@dataclass
class RecordingDriver:
    windows: list = field(default_factory=lambda: [dict(WINDOW)])
    state: dict = field(
        default_factory=lambda: {
            "elements": [dict(e) for e in ELEMENTS],
            "snapshot_id": "s00000001",
            "elements_complete": True,
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
        if tool == "clipboard_read":
            # 驱动回包形状由核心解析（核心自带 73 项测试）——这里只保证本测试能跑到解析
            return {"ok": True, "types": ["CF_UNICODETEXT"], "text": "样例文本"}
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


class CatalogDeclaresP2Tools(unittest.TestCase):
    def test_tools_and_handlers_align(self) -> None:
        names = {t["name"] for t in S.tool_catalog()}
        for tool in P2_TOOLS:
            self.assertIn(tool, names, f"{tool} 必须出现在工具表里")
        self.assertEqual(names, set(S._TOOL_HANDLERS), "工具表与处理函数必须一一对应")

    def test_required_fields(self) -> None:
        for tool, required in P2_TOOLS.items():
            with self.subTest(tool=tool):
                schema = _schema(tool)
                self.assertEqual(schema["type"], "object")
                for name in required:
                    self.assertIn(name, schema.get("required", []), f"{tool} 必须要求 {name}")

    def test_wait_kind_enum_from_core(self) -> None:
        """wait 的 kind 枚举必须从核心现读——手写副本漏同步会让门面先于核心拒掉合法 kind。"""
        self.assertEqual(_schema("winuse_wait")["properties"]["kind"]["enum"], list(wait_mod.KINDS))
        self.assertIn("element_gone", wait_mod.KINDS)

    def test_wait_declares_element_addressing(self) -> None:
        props = _schema("winuse_wait")["properties"]
        for name in ("element_index", "element_token", "snapshot", "pattern"):
            self.assertIn(name, props, f"winuse_wait 必须声明 {name}（element_gone 的寻址方式）")

    def test_schemas_are_json_serializable(self) -> None:
        json.dumps(S.tool_catalog(), ensure_ascii=False)


class StateOptionsWithoutConfig(unittest.TestCase):
    def test_elements_are_not_trimmed_away(self) -> None:
        """cfg 缺失时 `max_elements` 不能被传成 None → 核心 `int(x or 0)` 变成「上限 0 个元素」，
        整棵树被裁光。这个坑潜伏很久：只要调用方没带配置对象就会踩到（回归锁）。"""
        drv = RecordingDriver()
        out = _call(drv, "winuse_state", {})
        self.assertTrue(out.get("elements"), f"cfg 缺失时不该裁光元素树：{out.get('stats')}")
        payload = _last(drv, "get_window_state")
        self.assertGreater(int(payload.get("max_elements") or 0), 0, "下推给驱动的上限不该退化成 0")


class TreeHashChain(unittest.TestCase):
    def test_state_can_include_tree_hash(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_state", {"include_tree_hash": True})
        self.assertTrue(out.get("tree_hash"), out)
        self.assertEqual(len(str(out["tree_hash"])), 64, "应是 64 位十六进制指纹")

    def test_state_omits_tree_hash_by_default(self) -> None:
        drv = RecordingDriver()
        self.assertNotIn("tree_hash", _call(drv, "winuse_state", {}))

    def test_tree_hash_is_whole_window(self) -> None:
        """指纹在裁剪/投影**之前**算：换 query 或改上限都不该改变它，否则「界面变了没」会误报。"""
        drv = RecordingDriver()
        plain = _call(drv, "winuse_state", {"include_tree_hash": True})
        projected = _call(drv, "winuse_state", {"include_tree_hash": True, "query": "复制"})
        capped = _call(drv, "winuse_state", {"include_tree_hash": True, "max_elements": 1})
        self.assertTrue(plain.get("tree_hash"), plain)
        self.assertEqual(plain["tree_hash"], projected["tree_hash"], "query 投影不该影响整窗指纹")
        self.assertEqual(plain["tree_hash"], capped["tree_hash"], "元素上限不该影响整窗指纹")

    def test_tree_hash_note_marks_driver_truncation(self) -> None:
        """只有**驱动侧**回不全时才是部分口径——那才是真要提醒调用方的情形。"""
        drv = RecordingDriver()
        self.assertEqual(
            _call(drv, "winuse_state", {"include_tree_hash": True})["tree_hash_note"],
            treehash_mod.SCOPE_NOTE_FULL,
        )
        drv.state["elements_complete"] = False
        self.assertEqual(
            _call(drv, "winuse_state", {"include_tree_hash": True})["tree_hash_note"],
            treehash_mod.SCOPE_NOTE_PARTIAL,
        )

    def test_tree_hash_call_ignores_user_params(self) -> None:
        """指纹单独取一次状态：不带 query、上限放到硬上限——否则指纹是「参数变了」的镜子。"""
        drv = RecordingDriver()
        _call(drv, "winuse_state", {"include_tree_hash": True, "query": "复制", "max_elements": 1})
        payloads = [p for n, p in drv.calls if n == "get_window_state"]
        self.assertEqual(len(payloads), 2, f"应有一次常规取状态 + 一次指纹取状态：{drv.calls}")
        self.assertNotIn("query", payloads[-1], f"指纹那次不该带 query：{payloads[-1]}")
        self.assertGreater(
            int(payloads[-1].get("max_elements") or 0),
            int(payloads[0].get("max_elements") or 0),
            f"指纹那次的元素上限必须更大：{payloads}",
        )

    def test_tree_hash_ignores_window_movement(self) -> None:
        """指纹只吃语义字段：窗口整体位移后指纹必须不变，否则「界面变了没」会被误报。"""
        from winuse.core.treehash import compute_tree_hash

        moved = []
        for element in ELEMENTS:
            shifted = dict(element)
            frame = dict(element["frame"])
            frame["x"] += 137
            frame["y"] -= 9
            shifted["frame"] = frame
            moved.append(shifted)
        self.assertEqual(
            compute_tree_hash([dict(e) for e in ELEMENTS]),
            compute_tree_hash(moved),
        )


class WaitElementGoneThroughMcp(unittest.TestCase):
    def test_params_reach_driver(self) -> None:
        drv = RecordingDriver()
        out = _call(
            drv,
            "winuse_wait",
            {"kind": "element_gone", "element_token": "s00000001:0", "timeout_seconds": 0.05, "interval_seconds": 0.05},
        )
        self.assertTrue(out.get("ok"), out)
        # 只断「接线到了」：元素仍在树里 → 未命中（核心的判定逻辑由子代理的 19 项测试覆盖）
        self.assertFalse(out.get("condition_met"), out)
        self.assertTrue(any(n == "get_window_state" for n, _ in drv.calls), drv.calls)

    def test_index_needs_snapshot(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_wait", {"kind": "element_gone", "element_index": 0})
        self.assertFalse(out.get("ok"), out)
        self.assertNotIn("get_window_state", [n for n, _ in drv.calls], "参数错必须在碰驱动之前抛")


class FindChain(unittest.TestCase):
    def test_substring_hit_returns_snapshot(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_find", {"text": "修改日期"})
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("snapshot_id"), "s00000001")
        self.assertTrue(out.get("count"), out)
        _last(drv, "get_window_state")

    def test_typo_still_hits(self) -> None:
        """模糊的价值在**非子串**命中：把「期」打成「斯」，精确子串一条也找不到。"""
        drv = RecordingDriver()
        out = _call(drv, "winuse_find", {"text": "修改日斯", "threshold": 0.5})
        self.assertTrue(out.get("count"), f"错字应仍能命中：{out}")
        self.assertNotIn("修改日斯", "修改日期▲", "这组数据必须是非子串关系，否则测不到模糊")
        self.assertTrue(
            any(float(m.get("score") or 0) < 1.0 for m in out["matches"]),
            f"错字命中不该是满分（满分=相等或互为子串）：{out['matches']}",
        )


class ReadChain(unittest.TestCase):
    def test_batch_reads_with_one_state_call(self) -> None:
        drv = RecordingDriver()
        out = _call(
            drv,
            "winuse_read",
            {
                "app": "fake.exe",
                "elements": [{"token": "s00000001:0"}, {"token": "s00000001:1"}],
                "fields": ["label"],
            },
        )
        self.assertTrue(out.get("ok"), out)
        # 口径：一次定位窗口（解析 target）+ 一次读值（run_read 内部只发一次状态请求）。
        # 关键是**不随元素个数增长**——所以这里锁的是 2，不是「有几条状态请求」。
        state_calls = [p for n, p in drv.calls if n == "get_window_state"]
        self.assertEqual(len(state_calls), 2, f"批量读值不该随元素个数增长状态请求：{drv.calls}")


class ClipboardChain(unittest.TestCase):
    def test_read_types_only(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_clipboard", {"action": "read"})
        self.assertTrue(out.get("ok"), out)
        payload = _last(drv, "clipboard_read")
        self.assertNotEqual(payload.get("include_text"), True, "只列类型时不该向驱动要正文")

    def test_read_text_requires_confirm(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_clipboard", {"action": "read", "include_text": True})
        self.assertFalse(out.get("ok"), out)
        self.assertTrue(out.get("requires_confirm"), out)
        self.assertEqual([n for n, _ in drv.calls], [], "未确认的隐私读取必须零下发")

    def test_write_requires_confirm(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_clipboard", {"action": "write", "text": "样例"})
        self.assertFalse(out.get("ok"), out)
        self.assertEqual([n for n, _ in drv.calls], [], "未确认的写剪贴板必须零下发")

    def test_write_dry_run_does_not_touch_driver(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_clipboard", {"action": "write", "text": "样例", "confirm": True})
        self.assertFalse(out.get("dry_run") is False, out)
        self.assertEqual([n for n, _ in drv.calls], [], "干跑不该下发")

    def test_write_executes_once(self) -> None:
        drv = RecordingDriver()
        out = _call(
            drv,
            "winuse_clipboard",
            {"action": "write", "text": "样例", "confirm": True, "execute": True},
        )
        self.assertTrue(out.get("ok"), out)
        writes = [p for n, p in drv.calls if n == "clipboard_write"]
        self.assertEqual(len(writes), 1, f"真写只该下发一次：{drv.calls}")


class SessionChain(unittest.TestCase):
    def test_session_needs_no_driver(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_session", {})
        self.assertTrue(out.get("ok"), out)
        self.assertEqual([n for n, _ in drv.calls], [], "会话状态回答的是「驱动还有没有用」，不该碰驱动")
        self.assertIn("locked", out)
        self.assertIn("availability", out)

    def test_explain_can_be_turned_off(self) -> None:
        drv = RecordingDriver()
        out = _call(drv, "winuse_session", {"explain": False})
        self.assertNotIn("availability", out)


class BridgeTranslatesP2Tools(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_hermes_adapter import bridge_module

        self.build = bridge_module().build_args

    def test_find(self) -> None:
        argv = self.build("winuse_find", {"text": "修改日期", "threshold": 0.6, "limit": 5})
        self.assertEqual(argv[0], "find")
        self.assertIn("--text", argv)
        self.assertIn("修改日期", argv)
        self.assertIn("--threshold", argv)

    def test_read_expands_element_arrays(self) -> None:
        argv = self.build(
            "winuse_read",
            {"elements": [{"index": 0}, {"token": "s1:2"}], "fields": ["label", "value"], "snapshot": "s1"},
        )
        self.assertEqual(argv[0], "read")
        self.assertIn("index:0", argv)
        self.assertIn("token:s1:2", argv)
        self.assertEqual(argv.count("--element"), 2)
        self.assertEqual(argv.count("--field"), 2)
        self.assertIn("--snapshot", argv)

    def test_clipboard_flags(self) -> None:
        argv = self.build(
            "winuse_clipboard",
            {"action": "write", "text": "样例", "confirm": True, "execute": True},
        )
        self.assertEqual(argv[0], "clipboard")
        self.assertIn("--action", argv)
        self.assertIn("--confirm", argv)
        self.assertIn("--execute", argv)

    def test_session_default_explain(self) -> None:
        argv = self.build("winuse_session", {})
        self.assertEqual(argv, ["session"])
        self.assertIn("--no-explain", self.build("winuse_session", {"explain": False}))

    def test_state_tree_hash_flag(self) -> None:
        """桥接漏翻 → Hermes 侧传了 include_tree_hash 也会被静默丢掉。"""
        self.assertIn("--include-tree-hash", self.build("winuse_state", {"include_tree_hash": True}))

    def test_wait_element_gone_params(self) -> None:
        argv = self.build(
            "winuse_wait",
            {"kind": "element_gone", "element_token": "s1:0", "snapshot": "s1"},
        )
        self.assertEqual(argv[0], "wait")
        self.assertIn("--element-token", argv)
        self.assertIn("s1:0", argv)
        self.assertIn("--snapshot", argv)


class CliRegistersP2Commands(unittest.TestCase):
    def _choices(self) -> set[str]:
        parser = argparse.ArgumentParser(prog="winuse")
        sub = parser.add_subparsers(dest="cmd")
        cli_capabilities.register(sub)
        return set(sub.choices)

    def test_commands_exist(self) -> None:
        choices = self._choices()
        for cmd in ("find", "read", "clipboard", "session"):
            self.assertIn(cmd, choices, f"cli_capabilities 必须注册 {cmd} 子命令")

    def test_wait_has_element_flags(self) -> None:
        parser = argparse.ArgumentParser(prog="winuse")
        sub = parser.add_subparsers(dest="cmd")
        cli_capabilities.register(sub)
        wait_parser = sub.choices["wait"]
        opts = {opt for action in wait_parser._actions for opt in action.option_strings}
        for flag in ("--element-index", "--element-token", "--snapshot"):
            self.assertIn(flag, opts, f"wait 子命令缺 {flag}")


if __name__ == "__main__":
    unittest.main()
