"""MCP 服务端分派与主循环测试（PR-6）。

要点：
- 分派层纯函数化（handle_message），可注入假 driver，不碰真实桌面
- 主循环 serve() 用内存流（StringIO）测：stdout 只许出现合法 MCP 消息
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field

import pytest

from winuse.mcp import protocol as P
from winuse.mcp import server as S


@dataclass
class FakeDriver:
    """极简假驱动：按脚本返回，记录调用（不依赖 tests/fake_driver.py，保持本测试自包含）。"""

    windows: list = field(default_factory=list)
    state: dict = field(default_factory=lambda: {"elements": [], "snapshot_id": "s00000001"})
    calls: list = field(default_factory=list)
    fail_on: str | None = None

    def call(self, tool: str, payload: dict | None = None):
        self.calls.append((tool, payload or {}))
        if self.fail_on == tool:
            from winuse.driver.errors import DriverError

            raise DriverError(f"假驱动故意让 {tool} 失败")
        if tool == "list_windows":
            return {"windows": self.windows, "ok": True}
        if tool == "get_window_state":
            return dict(self.state, ok=True)
        return {"ok": True}

    def health(self):
        return {"ok": True, "path": "<fake>", "version": "0.0.0"}


def _window(pid=100, title="假窗口", window_id=11):
    return {
        "pid": pid,
        "window_id": window_id,
        "app_name": "fake.exe",
        "title": title,
        "minimized": False,
        "is_on_screen": True,
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
    }


@pytest.fixture()
def session():
    return S.Session(driver=FakeDriver(windows=[_window()]), initialized=True)


class TestInitialize:
    def test_initialize_result_shape(self, session):
        out = S.handle_message(
            session,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "T", "version": "1"},
                },
            },
        )
        result = out["result"]
        assert out["id"] == 1
        assert result["protocolVersion"] == "2025-06-18"
        assert "tools" in result["capabilities"], "支持工具就必须声明 tools 能力"
        assert result["serverInfo"]["name"] == "winuse"
        assert result["serverInfo"]["version"]

    def test_initialize_version_fallback(self, session):
        out = S.handle_message(
            session, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1.0"}}
        )
        assert out["result"]["protocolVersion"] == P.PREFERRED_PROTOCOL_VERSION

    def test_initialized_notification_has_no_reply(self, session):
        assert S.handle_message(session, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        assert session.initialized is True


class TestToolListing:
    def test_tools_list_matches_catalog_constants(self, session):
        out = S.handle_message(session, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        tools = out["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == set(S.TOOL_NAMES), "工具表以 server.TOOL_NAMES 为唯一出处"
        assert names == set(S._TOOL_HANDLERS), "工具表与处理函数必须一一对应"
        for t in tools:
            assert t["description"], f"{t['name']} 必须有描述"
            assert isinstance(t["inputSchema"], dict)
            assert t["inputSchema"]["type"] == "object", "inputSchema 必须是 JSON Schema 对象（规范字段名）"

    def test_tools_list_is_json_serializable(self, session):
        out = S.handle_message(session, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        json.dumps(out, ensure_ascii=False)


class TestToolCall:
    def test_state_call_returns_text_content(self, session):
        out = S.handle_message(
            session,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "winuse_state", "arguments": {"app": "fake"}},
            },
        )
        content = out["result"]["content"]
        assert content[0]["type"] == "text"
        payload = json.loads(content[0]["text"])
        assert payload["ok"] is True
        assert out["result"].get("isError") is False

    def test_act_dry_run_by_default(self, session):
        out = S.handle_message(
            session,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "winuse_act",
                    "arguments": {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1"},
                },
            },
        )
        payload = json.loads(out["result"]["content"][0]["text"])
        assert payload["dry_run"] is True, "MCP 门面必须与 CLI 一致：默认干跑"

    def test_unknown_tool_is_protocol_error(self, session):
        out = S.handle_message(
            session,
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        )
        assert out["error"]["code"] == P.INVALID_PARAMS
        assert "nope" in out["error"]["message"]

    def test_missing_required_argument_is_protocol_error(self, session):
        out = S.handle_message(
            session,
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "winuse_act", "arguments": {}}},
        )
        assert out["error"]["code"] == P.INVALID_PARAMS

    def test_driver_failure_is_tool_execution_error_not_protocol_error(self):
        s = S.Session(driver=FakeDriver(windows=[_window()], fail_on="get_window_state"), initialized=True)
        out = S.handle_message(
            s,
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "winuse_state", "arguments": {"app": "fake"}}},
        )
        assert "error" not in out, "驱动失败属工具执行错误，不是协议错误"
        assert out["result"]["isError"] is True
        payload = json.loads(out["result"]["content"][0]["text"])
        assert payload["ok"] is False and payload["error"]["code"]


class TestProtocolEdges:
    def test_unknown_method(self, session):
        out = S.handle_message(session, {"jsonrpc": "2.0", "id": 10, "method": "resources/list"})
        assert out["error"]["code"] == P.METHOD_NOT_FOUND

    def test_ping_returns_empty_result(self, session):
        out = S.handle_message(session, {"jsonrpc": "2.0", "id": 11, "method": "ping"})
        assert out["result"] == {}

    def test_unknown_notification_silently_ignored(self, session):
        assert S.handle_message(session, {"jsonrpc": "2.0", "method": "notifications/whatever"}) is None


class TestServeLoop:
    def _run(self, lines: list[str]) -> tuple[list[dict], str]:
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        S.serve(stdin=stdin, stdout=stdout, stderr=stderr, driver_factory=lambda: FakeDriver(windows=[_window()]))
        out_lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        return [json.loads(ln) for ln in out_lines], stderr.getvalue()

    def test_handshake_then_tools_list(self):
        msgs, err = self._run(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            ]
        )
        assert [m["id"] for m in msgs] == [1, 2], "通知不得产生回包"
        assert msgs[1]["result"]["tools"]

    def test_every_stdout_line_is_valid_json(self):
        msgs, _ = self._run(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "winuse_state", "arguments": {}}}),
            ]
        )
        assert all(m.get("jsonrpc") == "2.0" for m in msgs), "stdout 只许出现合法 MCP 消息"

    def test_blank_lines_ignored(self):
        msgs, _ = self._run(["", "   ", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})])
        assert len(msgs) == 1 and msgs[0]["id"] == 1

    def test_broken_line_gets_parse_error_and_loop_continues(self):
        msgs, err = self._run(["{不是 json", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})])
        assert msgs[0]["error"]["code"] == P.PARSE_ERROR
        assert msgs[1]["id"] == 1, "一行坏掉不能让服务退出"

    def test_logs_go_to_stderr_not_stdout(self):
        msgs, err = self._run([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})])
        assert msgs, "初始化必须有回包"
        assert "winuse" in err.lower(), "启动日志应写 stderr"
