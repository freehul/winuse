"""MCP stdio 传输层的 JSON-RPC 2.0 编解码（零依赖，纯函数，便于单测）。

官方依据（modelcontextprotocol.io/specification/2025-06-18）：
- 传输层/stdio：消息是单条 JSON-RPC request / notification / response；
  **以换行分隔，且不得含内嵌换行**；服务端只能向 stdout 写合法 MCP 消息，日志走 stderr。
- 生命周期/版本协商：客户端在 initialize 里给出它支持的版本；服务端支持就原样回，
  不支持就回自己支持的最新版本。
- 工具/错误：协议错误（未知工具、非法参数）走 JSON-RPC error（-32602 等），
  工具**执行**错误走 result 里的 `isError: true`。
"""

from __future__ import annotations

import json

#: 本服务端支持的协议版本（新→旧）。首选即第一个。
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL_VERSION: str = SUPPORTED_PROTOCOL_VERSIONS[0]

# JSON-RPC 2.0 标准错误码（规范原文）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def encode_message(msg: dict) -> str:
    """把消息编码成**单行** JSON。

    紧凑序列化天然满足"不得含内嵌换行"：字符串里的换行会被转义成 `\\n` 两个字符。
    保留中文不转义（ensure_ascii=False）：体积更小，人工排查也看得懂。
    """
    return json.dumps(msg, ensure_ascii=False)


def decode_message(line: str) -> dict | None:
    """解析一行输入；空白行、非法 JSON、非对象一律返回 None（由调用方决定怎么处理）。"""
    text = line.strip()
    if not text:
        return None
    try:
        msg = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return msg if isinstance(msg, dict) else None


def make_result(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def make_error(msg_id, code: int, message: str, data: dict | None = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def is_request(msg: dict) -> bool:
    """请求 = 有 method 且有 id（需要回包）。"""
    return isinstance(msg, dict) and "method" in msg and "id" in msg


def is_notification(msg: dict) -> bool:
    """通知 = 有 method 但无 id（规范要求不回包）。"""
    return isinstance(msg, dict) and "method" in msg and "id" not in msg


#: `id` 允许的类型：字符串或数字（bool 是 int 的子类，必须排除）
_ID_TYPES = (str, int)


def validate_envelope(msg: object) -> str | None:
    """校验 JSON-RPC 2.0 信封；返回**拒绝原因**，合法返回 `None`（纯函数）。

    为什么要在入口严格校验（R-6）：本服务端原先对信封几乎不设防——
    `jsonrpc="1.0"`、`method` 是数字、`id` 是数组这些都照单全收，而"既没有
    method 也不像响应"的对象被静默当成"无需回包"。客户端故障因此与"服务端没收到"
    无法区分，排查时完全看不到线索。

    规则（照 JSON-RPC 2.0 + MCP 的实践口径）：
    - 必须是对象，且 `jsonrpc` 必须**恰好**是字符串 `"2.0"`
    - `method` 存在时必须是**非空字符串**
    - `id` 存在时必须是字符串或数字（`null` / 数组 / 对象 / 布尔一律拒）
    - `method` 与 `result`/`error` 都不存在 → 不是任何合法消息形态
    """
    if not isinstance(msg, dict):
        return "消息必须是 JSON 对象"
    if msg.get("jsonrpc") != "2.0":
        return f"jsonrpc 必须是字符串 \"2.0\"，收到 {msg.get('jsonrpc')!r}"
    has_method = "method" in msg
    if has_method:
        if not isinstance(msg["method"], str) or not msg["method"]:
            return f"method 必须是非空字符串，收到 {msg.get('method')!r}"
    elif "result" not in msg and "error" not in msg:
        return "既没有 method，也没有 result/error：不是请求、通知或响应"
    if "id" in msg and msg["id"] is not None:
        value = msg["id"]
        if isinstance(value, bool) or not isinstance(value, _ID_TYPES):
            return f"id 必须是字符串或数字，收到 {type(value).__name__}"
    return None


def negotiate_version(requested: str | None) -> str:
    """版本协商：支持则原样回，否则回本端首选（规范：应回本端支持的最新版本）。"""
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return str(requested)
    return PREFERRED_PROTOCOL_VERSION
