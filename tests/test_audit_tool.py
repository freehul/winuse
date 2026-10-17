"""T-26：审计查询接进 MCP 门面与插件门面（只读、限条数、逻辑仍只在核心）。

这里守的是**接线**：工具表里有它、处理函数认得它、插件桥接能翻成 CLI 参数、
查询过程一次驱动都不碰（只读意味着零桌面副作用）。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from winuse import audit as A
from winuse.mcp import server as S

ADAPTER_DIR = Path(__file__).resolve().parents[1] / "adapters" / "hermes"


class _ExplodingDriver:
    """审计查询不该碰驱动——碰了就炸（证明真的零副作用）。"""

    def call(self, *args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("审计查询是只读的，不得调用驱动")


def _session() -> S.Session:
    # 这些用例关心工具语义，不关心握手顺序（生命周期另有 test_mcp_lifecycle.py）
    return S.Session(driver=_ExplodingDriver(), initialized=True)


def _log(tmp_path: Path, count: int = 5) -> Path:
    path = tmp_path / "audit.jsonl"
    for i in range(count):
        A.record({"event": "act.dry_run", "action": "click", "i": i}, path=path)
    return path


def test_tool_registered_in_table_and_handlers():
    assert "winuse_audit" in S.TOOL_NAMES
    assert "winuse_audit" in S._TOOL_HANDLERS
    names = {t["name"] for t in S.tool_catalog()}
    assert "winuse_audit" in names


def test_recent_returns_entries_without_touching_driver(tmp_path, monkeypatch):
    path = _log(tmp_path)
    monkeypatch.setattr(A, "default_path", lambda env_path=None: path)
    out = S._call_tool(_session(), {"name": "winuse_audit", "arguments": {"recent": 3}})
    assert out["ok"] is True
    assert out["returned"] == 3
    assert [e["i"] for e in out["entries"]] == [2, 3, 4]  # 最近 3 条，按时间正序


def test_recent_is_clamped_to_max(tmp_path, monkeypatch):
    path = _log(tmp_path, 3)
    monkeypatch.setattr(A, "default_path", lambda env_path=None: path)
    out = S._call_tool(_session(), {"name": "winuse_audit", "arguments": {"recent": 9999}})
    assert out["requested"] == S.MAX_AUDIT_ROWS
    assert out["clamped"] is True
    assert out["requested_before_clamp"] == 9999


def test_bad_recent_is_param_error(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "default_path", lambda env_path=None: tmp_path / "audit.jsonl")
    with pytest.raises(S.WinUseError):
        S._call_tool(_session(), {"name": "winuse_audit", "arguments": {"recent": "abc"}})
    with pytest.raises(S.WinUseError):
        S._call_tool(_session(), {"name": "winuse_audit", "arguments": {"recent": 0}})


def test_stats_returns_rotation_summary(tmp_path, monkeypatch):
    path = _log(tmp_path, 2)
    monkeypatch.setattr(A, "default_path", lambda env_path=None: path)
    out = S._call_tool(_session(), {"name": "winuse_audit", "arguments": {"stats": True}})
    assert out["ok"] is True
    assert out["rotation"]["lines"] == 2


def _bridge():
    """按官方姿势把 adapters/hermes 当命名空间子包加载（与 test_hermes_adapter 同款）。"""
    root = sys.modules.get("hermes_plugins")
    if root is None:
        root = types.ModuleType("hermes_plugins")
        root.__path__ = []  # type: ignore[attr-defined]
        sys.modules["hermes_plugins"] = root
    full = "hermes_plugins.winuse"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            full, ADAPTER_DIR / "__init__.py", submodule_search_locations=[str(ADAPTER_DIR)]
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        module.__package__ = full
        sys.modules[full] = module
        spec.loader.exec_module(module)
    importlib.import_module(f"{full}.core_bridge")
    return sys.modules[f"{full}.core_bridge"]


def test_plugin_bridge_maps_audit_to_cli():
    bridge = _bridge()
    assert bridge.build_args("winuse_audit", {"recent": 3}) == ["audit", "--recent", "3"]
    assert bridge.build_args("winuse_audit", {"stats": True}) == ["audit", "--stats"]
