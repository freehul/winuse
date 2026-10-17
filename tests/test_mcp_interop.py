"""MCP 门面的**互通性**钉死用例：编码必须 UTF-8、单条结果必须限流。

两个问题都是真机踩出来的（WorkBuddy 作为客户端）：工具描述中文乱码；
调 `winuse_state` 回包 5.1 万字符后连接被关，之后所有工具都显示未连接。
"""

from __future__ import annotations

import io
import json

from winuse.mcp import server as S
from winuse.textio import force_utf8


def _payload(n: int) -> dict:
    """造一份「像 winuse_state」的大回包。"""
    return {
        "ok": True,
        "target": {"app_name": "demo", "title": "演示窗口"},
        "snapshot_id": 1,
        "elements": [{"index": i, "name": f"元素{i}" * 20, "value": ""} for i in range(n)],
    }


# --------------------------------------------------------------- 结果限流


def test_bounded_leaves_small_payload_alone():
    payload = _payload(2)
    assert S._bounded(payload) == payload


def test_bounded_trims_elements_and_flags():
    out = S._bounded(_payload(500))
    assert len(S._dumps(out)) <= S.MAX_RESULT_CHARS
    assert out["truncated"] is True
    assert out["total_before_truncate"] == 500
    assert 0 < out["returned"] < 500
    assert out["elements"][0]["index"] == 0  # 保前 N 条
    assert "truncate_hint" in out
    json.dumps(out)  # 裁剪后仍是合法 JSON（不能把 JSON 截断）


def test_bounded_falls_back_to_structured_error():
    out = S._bounded({"ok": True, "blob": "x" * (S.MAX_RESULT_CHARS + 10)})
    assert out["ok"] is False
    assert out["error"]["code"] == "RESULT_TOO_LARGE"


def test_tool_result_applies_bounding(monkeypatch):
    monkeypatch.setattr(S, "_call_tool", lambda session, params: _payload(500))
    result = S._tool_result(S.Session(driver=None, initialized=True), {"name": "winuse_state", "arguments": {}})
    text = result["content"][0]["text"]
    assert len(text) <= S.MAX_RESULT_CHARS
    assert json.loads(text)["truncated"] is True


# --------------------------------------------------------------- 编码口径


def test_force_utf8_switches_cp936_stream():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp936")
    assert force_utf8(stream) is True
    stream.write("中文")
    stream.flush()
    assert raw.getvalue() == "中文".encode("utf-8")


def test_force_utf8_tolerates_stream_without_reconfigure():
    assert force_utf8(io.StringIO()) is False


def test_serve_emits_utf8_when_stream_starts_as_cp936():
    """serve 必须把本地编码的流掰成 UTF-8——否则客户端看到的就是乱码。"""
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp936")
    # R-6 之后必须完成握手才能调工具（顺带验证 initialize 的回包也是 UTF-8）
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}}, ensure_ascii=False),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}, ensure_ascii=False),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ensure_ascii=False),
    ]
    code = S.serve(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=stdout, stderr=io.StringIO())
    stdout.flush()
    assert code == 0
    decoded = raw.getvalue().decode("utf-8")  # 按 UTF-8 能解 = 写的是 UTF-8
    回包 = [json.loads(x) for x in decoded.splitlines() if x.strip()]
    assert len(回包) == 2, f"initialize 与 tools/list 各一条回包，实际：{回包}"
    desc = next(t for t in 回包[-1]["result"]["tools"] if t["name"] == "winuse_state")["description"]
    assert "界面状态" in desc
