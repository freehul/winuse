"""R-6：JSON-RPC 2.0 信封校验 + MCP 生命周期状态机。

报告 R-6 的三处问题：
1. `Session.initialized` 只在收到 `notifications/initialized` 时置位，但
   `handle_message()` 没在 initialize 之前拒绝 `tools/list` / `tools/call`
   ——未握手的连接可以直接列工具、调工具。
2. `jsonrpc="1.0"`、缺 `jsonrpc`、`method` 不是字符串等非法信封都没被严格拒绝。
3. 既不合法数学意义上的请求、也不像响应（既无 method 又无 result/error）的对象
   被静默当成"无需回包"，客户端故障被吞掉。

规范依据：JSON-RPC 2.0（信封与错误码 -32600 / -32700）与 MCP 2025-06-18 lifecycle
（initialize → notifications/initialized → 才进入正常操作阶段）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from winuse.mcp import protocol as P
from winuse.mcp import server as S


@dataclass
class FakeDriver:
    windows: list = field(default_factory=list)

    def call(self, tool: str, payload: dict | None = None):
        if tool == "list_windows":
            return {"windows": self.windows, "ok": True}
        if tool == "get_window_state":
            return {"elements": [], "snapshot_id": "s1", "ok": True}
        return {"ok": True}

    def health(self):
        return {"ok": True, "path": "<fake>", "version": "0.0.0"}


def _session(**kw) -> S.Session:
    return S.Session(driver=FakeDriver(), **kw)


def _init_msg(msg_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "initialize",
        "params": {"protocolVersion": P.PREFERRED_PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}},
    }


def _initialized(session) -> None:
    S.handle_message(session, _init_msg())
    S.handle_message(session, {"jsonrpc": "2.0", "method": "notifications/initialized"})


# ------------------------------------------------------------ 生命周期


def test_tools_list_before_initialize_is_rejected():
    out = S.handle_message(_session(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert out["error"]["code"] == P.INVALID_REQUEST
    assert "初始化" in out["error"]["message"] or "initialize" in out["error"]["message"]


def test_tools_call_before_initialize_is_rejected():
    out = S.handle_message(
        _session(),
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "winuse_state", "arguments": {}}},
    )
    assert out["error"]["code"] == P.INVALID_REQUEST


def test_ping_is_allowed_before_initialize():
    """探活不该被握手顺序挡住（规范里 ping 是可随时用的）。"""
    out = S.handle_message(_session(), {"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert out["result"] == {}


def test_initialize_alone_is_not_enough():
    """initialize 之后还要收到 notifications/initialized 才算握手完成。"""
    session = _session()
    S.handle_message(session, _init_msg())
    out = S.handle_message(session, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    assert out["error"]["code"] == P.INVALID_REQUEST


def test_full_handshake_then_tools_work():
    session = _session()
    _initialized(session)
    out = S.handle_message(session, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
    assert "result" in out
    assert out["result"]["tools"]


def test_notification_gets_no_reply_even_before_initialize():
    out = S.handle_message(_session(), {"jsonrpc": "2.0", "method": "some/notification"})
    assert out is None


# -------------------------------------------------------------- 信封校验


@pytest.mark.parametrize(
    "msg",
    [
        {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
        {"id": 1, "method": "tools/list"},                      # 缺 jsonrpc
        {"jsonrpc": "2.0", "id": 1, "method": 123},             # method 不是字符串
        {"jsonrpc": "2.0", "id": 1, "method": ""},              # method 为空串
        {"jsonrpc": "2.0", "id": [1], "method": "tools/list"},  # id 是数组
        {"jsonrpc": "2.0", "id": {"a": 1}, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": True, "method": "tools/list"},  # bool 不算 id
        {},                                                     # 既无 method 也无 result/error
        {"jsonrpc": "2.0"},
    ],
)
def test_malformed_envelopes_are_invalid_request(msg):
    out = S.handle_message(_session(), msg)
    assert out is not None, "非法信封必须有回包，不能静默吞掉"
    assert out["error"]["code"] == P.INVALID_REQUEST


def test_response_shaped_message_is_ignored():
    """客户端误发的响应（有 result、无 method）应被忽略，而不是当非法请求。"""
    out = S.handle_message(_session(), {"jsonrpc": "2.0", "id": 9, "result": {}})
    assert out is None


def test_unknown_method_after_handshake_is_method_not_found():
    session = _session()
    _initialized(session)
    out = S.handle_message(session, {"jsonrpc": "2.0", "id": 10, "method": "no/such"})
    assert out["error"]["code"] == P.METHOD_NOT_FOUND


def test_validate_envelope_is_pure_and_reusable():
    assert P.validate_envelope({"jsonrpc": "2.0", "id": 1, "method": "ping"}) is None
    assert P.validate_envelope({"jsonrpc": "1.0", "id": 1, "method": "ping"})
