"""MCP stdio 传输层编解码测试（PR-6）。

口径来自官方规范（modelcontextprotocol.io/specification/2025-06-18/basic/transports#stdio）：
- 消息是单条 JSON-RPC request / notification / response
- **以换行分隔，且不得含内嵌换行** → 服务端序列化必须单行
- 服务端只能向 stdout 写合法 MCP 消息（日志走 stderr）
"""

from __future__ import annotations

import json

from winuse.mcp import protocol as P


class TestEncode:
    def test_single_line_even_with_newlines_inside_values(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"text": "第一行\n第二行\ttab"}}
        line = P.encode_message(msg)
        assert "\n" not in line and "\r" not in line, "编码后不得含字面换行（规范硬要求）"
        assert json.loads(line)["params"]["text"] == "第一行\n第二行\ttab"

    def test_compact_by_default_no_indent(self):
        line = P.encode_message({"jsonrpc": "2.0", "id": 1, "result": {"a": 1}})
        assert line == '{"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}'

    def test_keeps_cjk_readable(self):
        line = P.encode_message({"jsonrpc": "2.0", "id": 2, "result": {"title": "记事本"}})
        assert "记事本" in line, "ensure_ascii=False：中文不转义（体积更小、便于排查）"


class TestDecode:
    def test_round_trip(self):
        msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "winuse_state"}}
        assert P.decode_message(P.encode_message(msg)) == msg

    def test_blank_and_whitespace_lines_are_none(self):
        assert P.decode_message("") is None
        assert P.decode_message("   \r\n") is None

    def test_broken_json_is_none(self):
        assert P.decode_message("{not json") is None
        assert P.decode_message("[1,2,3") is None

    def test_non_object_json_is_none(self):
        assert P.decode_message("[1,2,3]") is None
        assert P.decode_message('"just a string"') is None


class TestMessageShape:
    def test_make_result(self):
        msg = P.make_result(3, {"ok": True})
        assert msg == {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}

    def test_make_error(self):
        msg = P.make_error(4, P.INVALID_PARAMS, "Unknown tool: nope", {"tool": "nope"})
        assert msg["jsonrpc"] == "2.0"
        assert msg["id"] == 4
        assert msg["error"]["code"] == P.INVALID_PARAMS
        assert msg["error"]["message"] == "Unknown tool: nope"
        assert msg["error"]["data"] == {"tool": "nope"}

    def test_make_error_data_optional(self):
        assert "data" not in P.make_error(5, P.METHOD_NOT_FOUND, "no such method")["error"]

    def test_is_request_vs_notification(self):
        assert P.is_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert not P.is_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert P.is_notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert not P.is_notification({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        # 响应（有 id 无 method）既不是请求也不是通知
        assert not P.is_request({"jsonrpc": "2.0", "id": 1, "result": {}})
        assert not P.is_notification({"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_jsonrpc_error_codes_match_spec(self):
        assert P.PARSE_ERROR == -32700
        assert P.INVALID_REQUEST == -32600
        assert P.METHOD_NOT_FOUND == -32601
        assert P.INVALID_PARAMS == -32602
        assert P.INTERNAL_ERROR == -32603


class TestVersionNegotiation:
    def test_supported_version_echoed(self):
        assert P.negotiate_version("2025-03-26") == "2025-03-26"
        assert P.negotiate_version("2024-11-05") == "2024-11-05"

    def test_latest_supported_echoed(self):
        assert P.negotiate_version(P.PREFERRED_PROTOCOL_VERSION) == P.PREFERRED_PROTOCOL_VERSION

    def test_unsupported_falls_back_to_preferred(self):
        assert P.negotiate_version("1.0.0") == P.PREFERRED_PROTOCOL_VERSION
        assert P.negotiate_version("2099-01-01") == P.PREFERRED_PROTOCOL_VERSION

    def test_missing_version_falls_back_to_preferred(self):
        assert P.negotiate_version(None) == P.PREFERRED_PROTOCOL_VERSION
        assert P.negotiate_version("") == P.PREFERRED_PROTOCOL_VERSION

    def test_supported_list_contains_preferred(self):
        assert P.PREFERRED_PROTOCOL_VERSION in P.SUPPORTED_PROTOCOL_VERSIONS
