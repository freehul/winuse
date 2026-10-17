"""严格参数解析（C-1）：布尔字段只认 JSON 原生布尔。

为什么要有这个测试：门面原先一律 `bool(args.get("execute", False))`，而 Python 里
`bool("false") is True`。上游模型输出字符串、客户端序列化出错或被恶意构造时，
`"false"` 会被当成 `true` —— 干跑门、确认门、覆盖授权门全部可被绕过。

本测试锁定三件事：
1. 原生布尔原样通过（含 `False`，不能因为假值就丢默认值）
2. 字符串/数字/容器一律 `INVALID_PARAMS`，且**错误里带字段名**
3. MCP 门面：伪布尔必须变成协议错误 -32602，且**绝不能下发驱动调用**
"""

from __future__ import annotations

import pytest

from winuse.core.params import InvalidParams, strict_bool
from winuse.mcp import protocol as P
from winuse.mcp import server as S


# --------------------------------------------------------------------- 单元


def test_native_bools_pass_through():
    assert strict_bool(True, field="execute") is True
    assert strict_bool(False, field="execute") is False, "显式 False 不能被默认值顶掉"


def test_missing_uses_default():
    assert strict_bool(None, field="execute", default=False) is False
    assert strict_bool(None, field="stop_on_error", default=True) is True


@pytest.mark.parametrize("bad", ["false", "true", "", "0", "1", "no", 0, 1, 0.0, [], {}, ["false"]])
def test_non_bool_types_are_rejected(bad):
    with pytest.raises(InvalidParams) as exc:
        strict_bool(bad, field="execute")
    assert exc.value.code == "INVALID_PARAMS"
    assert "execute" in str(exc.value), "错误里必须点名是哪个字段，否则调用方无法定位"


def test_rejection_message_names_the_received_type():
    with pytest.raises(InvalidParams) as exc:
        strict_bool("false", field="confirm")
    text = str(exc.value)
    assert "confirm" in text
    assert "str" in text, "应报出收到的类型，便于上游修序列化"


# ------------------------------------------------------------------ MCP 门面


def _session():
    """复用 MCP 测试里的假驱动形态（本文件自包含，不 import 别的测试模块）。"""
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class FakeDriver:
        windows: list = dc_field(default_factory=list)
        calls: list = dc_field(default_factory=list)

        def call(self, tool: str, payload: dict | None = None):
            self.calls.append((tool, payload or {}))
            if tool == "list_windows":
                return {"windows": self.windows, "ok": True}
            if tool == "get_window_state":
                return {"elements": [], "snapshot_id": "s00000001", "ok": True}
            return {"ok": True}

        def health(self):
            return {"ok": True, "path": "<fake>", "version": "0.0.0"}

    window = {
        "pid": 100,
        "window_id": 11,
        "app_name": "fake.exe",
        "title": "假窗口",
        "minimized": False,
        "is_on_screen": True,
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
    }
    return S.Session(driver=FakeDriver(windows=[window]), initialized=True)


def _call(name: str, arguments: dict) -> dict:
    return S.handle_message(
        _session(),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )


@pytest.mark.parametrize(
    "field_name",
    ["execute", "confirm", "overwrite"],
)
def test_string_false_on_act_is_protocol_error(field_name):
    """核心场景：`execute="false"` 绝不能变成真执行。"""
    out = _call(
        "winuse_act",
        {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1", field_name: "false"},
    )
    assert out["error"]["code"] == P.INVALID_PARAMS, "伪布尔必须是参数级错误，不是内部错误"
    assert field_name in out["error"]["message"]


@pytest.mark.parametrize("field_name", ["allow_minimized", "frames", "no_windows"])
def test_string_true_on_state_is_protocol_error(field_name):
    out = _call("winuse_state", {field_name: "true"})
    assert out["error"]["code"] == P.INVALID_PARAMS
    assert field_name in out["error"]["message"]


def test_string_false_does_not_reach_the_driver():
    """兜底断言：出错时一次驱动调用都不许发生（不是"解析完再拒绝"）。"""
    session = _session()
    S.handle_message(
        session,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "winuse_act",
                "arguments": {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1",
                              "execute": "false"},
            },
        },
    )
    assert session.driver.calls == [], "伪布尔必须在碰驱动之前就被拒"


def test_native_false_still_means_dry_run():
    out = _call(
        "winuse_act",
        {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1", "execute": False},
    )
    payload = S._tool_act(_session(), {"action": "click", "app": "fake", "element_index": 0,
                                       "snapshot": "s1", "execute": False})
    assert out["result"]["isError"] is False
    assert payload["dry_run"] is True, "原生 false 必须仍然是干跑"


def test_window_confirm_gate_rejects_string_false():
    """确认门同样不能被字符串绕过（close 是危险动作）。"""
    out = _call("winuse_window", {"action": "close", "app": "fake", "confirm": "false", "execute": "true"})
    assert out["error"]["code"] == P.INVALID_PARAMS
    assert "confirm" in out["error"]["message"]


# ------------------------------------------------------------ Hermes 桥接


def test_hermes_bridge_rejects_string_false():
    """桥接把参数拼成 CLI 命令行：`if args.get("execute")` 会把 "false" 当真。"""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    path = root / "adapters" / "hermes" / "core_bridge.py"
    spec = importlib.util.spec_from_file_location("winuse_core_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with pytest.raises(InvalidParams):
        mod.build_args("winuse_act", {"action": "click", "execute": "false"})

    with pytest.raises(InvalidParams):
        mod.build_args("winuse_state", {"frames": "true"})


def test_hermes_bridge_native_bool_unchanged():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    path = root / "adapters" / "hermes" / "core_bridge.py"
    spec = importlib.util.spec_from_file_location("winuse_core_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "--execute" in mod.build_args("winuse_act", {"action": "click", "execute": True})
    assert "--execute" not in mod.build_args("winuse_act", {"action": "click", "execute": False})
    assert "--execute" not in mod.build_args("winuse_act", {"action": "click"})
