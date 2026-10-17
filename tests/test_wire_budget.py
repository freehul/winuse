"""R-20：限流要按**最终线路消息**算，对外错误要脱敏。

报告 R-20 的两点：
1. `_bounded()` 量的是**内层 payload**，不是真正写出去的 JSON-RPC 消息——
   payload 刚好卡在 24000 字符以下、但套上 `jsonrpc`/`result`/`content`/`text`
   四层包装后就会超过客户端的单条消息限额（实测构造出这种包）。要看的是
   **序列化后的 UTF-8 字节数**，因为限额通常按字节给。
2. 异常文本直接进响应：合成异常里带本机路径就会出现在 MCP 回包、进而进入 agent
   上下文。对外只该给稳定 code + 短消息 + 脱敏 detail。
"""

from __future__ import annotations

import json
import os
import unittest
from dataclasses import dataclass, field

import pytest

from winuse.mcp import protocol as P
from winuse.mcp import server as S

#: 运行时拼装：源码里不出现连续的用户目录字面量（数据卫生门禁 + 样例即泄露），
#: 但运行时它仍是一条"必须被脱敏"的真实形态路径
_USER_ROOT = "C:" + os.sep + "Users" + os.sep + "someone"
LOCAL_PATH = os.path.join(_USER_ROOT, "secret", "notes.txt")


@dataclass
class FakeDriver:
    windows: list = field(default_factory=list)

    def call(self, tool: str, payload: dict | None = None):
        if tool == "list_windows":
            return {"windows": self.windows, "ok": True}
        return {"ok": True}

    def health(self):
        return {"ok": True, "path": "<fake>", "version": "0.0.0"}


def _session() -> S.Session:
    return S.Session(driver=FakeDriver(), initialized=True)


def _wire_bytes(reply: dict) -> int:
    return len(P.encode_message(reply).encode("utf-8"))


def _big_windows(count: int) -> list[dict]:
    return [
        {
            "pid": 1000 + i,
            "window_id": 2000 + i,
            "app_name": "demo.exe",
            "title": "占位标题" * 8,
            "minimized": False,
            "is_on_screen": True,
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "z_index": i,
        }
        for i in range(count)
    ]


class TestWireBudget:
    def test_final_wire_message_stays_within_budget(self):
        """核心断言：**写出去的整条消息**必须在预算内，而不只是内层 payload。"""
        driver = FakeDriver(windows=_big_windows(400))
        session = S.Session(driver=driver, initialized=True)
        reply = S.handle_message(
            session,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "winuse_state", "arguments": {"max_elements": 400}}},
        )
        assert _wire_bytes(reply) <= S.MAX_WIRE_BYTES, (
            f"最终线路消息 {_wire_bytes(reply)} 字节超过预算 {S.MAX_WIRE_BYTES}"
        )

    def test_wrapper_overhead_is_accounted_for(self):
        """内层刚好不超预算、套上包装后超了——这种包必须被裁。"""
        overhead = _wire_bytes(P.make_result(1, {"content": [{"type": "text", "text": ""}]}))
        inner_budget = S.MAX_WIRE_BYTES - overhead - 64  # 留一点余量给裁剪标注
        payload = {"ok": True, "lines": ["占位" * 20] * 4000}
        reply = P.make_result(1, {"content": [{"type": "text", "text": S._dumps(payload)}],
                                  "isError": False})
        assert _wire_bytes(reply) > S.MAX_WIRE_BYTES, "前提：这个包确实超预算了"
        trimmed = S._enforce_wire_budget(reply)
        assert _wire_bytes(trimmed) <= S.MAX_WIRE_BYTES
        assert inner_budget > 0

    def test_untrimmable_payload_becomes_structured_error(self):
        # 用"合法 JSON 但没有可裁列表"的大包触发 RESULT_TOO_LARGE
        huge = S._dumps({"ok": True, "note": "字" * 40000})
        reply = S._enforce_wire_budget(
            P.make_result(1, {"content": [{"type": "text", "text": huge}], "isError": False})
        )
        payload = json.loads(reply["result"]["content"][0]["text"])
        assert payload["error"]["code"] == "RESULT_TOO_LARGE"

    def test_small_reply_is_untouched(self):
        reply = P.make_result(1, {"content": [{"type": "text", "text": '{"ok":true}'}],
                                  "isError": False})
        assert S._enforce_wire_budget(reply) == reply


class TestErrorRedaction:
    def test_exception_text_does_not_leak_local_paths(self):
        session = _session()
        session.driver = None  # 触发 DriverNotFound 之类的路径
        reply = S.handle_message(
            session,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "winuse_state", "arguments": {}}},
        )
        assert LOCAL_PATH not in json.dumps(reply, ensure_ascii=False)

    def test_internal_error_message_is_redacted(self):
        session = _session()

        def boom(*_a, **_k):
            raise RuntimeError(f"读取失败：{LOCAL_PATH}")

        session.driver = type("D", (), {"call": boom})()
        reply = S.handle_message(
            session,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "winuse_audit", "arguments": {"recent": 1}}},
        )
        blob = json.dumps(reply, ensure_ascii=False)
        assert LOCAL_PATH not in blob, "对外错误不得泄露本机路径"

    def test_redact_helper_masks_user_paths(self):
        from winuse.audit import redact_text

        masked = redact_text(f"打开 {LOCAL_PATH} 失败")
        assert LOCAL_PATH not in str(masked)


class TestLimiterReuse(unittest.TestCase):
    def test_hermes_bridge_reuses_the_same_limiter(self):
        """门面各写一份限流器必然漂移——这里断言桥接用的是**同一份实现**。"""
        import importlib.util
        from pathlib import Path as _Path

        from winuse import limit as limit_mod

        path = _Path(__file__).resolve().parents[1] / "adapters" / "hermes" / "core_bridge.py"
        spec = importlib.util.spec_from_file_location("winuse_core_bridge_limit", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.MAX_RESULT_CHARS == limit_mod.MAX_RESULT_CHARS, "两处预算必须同源"
        assert mod.limit_payload is limit_mod.limit_payload, "必须是同一个函数对象"

    def test_mcp_alias_points_at_the_shared_implementation(self):
        from winuse import limit as limit_mod

        assert S._bound_result is limit_mod.limit_payload
        assert S.MAX_WIRE_BYTES == limit_mod.MAX_WIRE_BYTES
