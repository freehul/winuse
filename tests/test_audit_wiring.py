"""R-1：审计必须覆盖**所有门面的变更路径**，且写入端与查询端同源。

报告 R-1 的三处缺口：
1. `run_act` 只在调用方传 `audit` sink 时才写，而 `_tool_act` 没传 → MCP 真动作不落审计
2. 窗口 / 批量 / 通知 / 注册表等 handler 也都没 sink
3. `serve(env_path=...)` 拿到的配置路径没有保存进 `Session`，`_tool_audit` 固定调
   `default_path()` → 项目 `.env` 配了 `WINUSE_AUDIT_PATH` 时，**写入与查询不是同一个文件**

本文件钉住：
- MCP 的 act / window / multi（逐步）/ notify 都会落审计，事件名可对账
- 审计条目带来源与请求 id（事后能查到"哪个客户端、哪条请求干的"）
- 审计写失败**可观察**（回执 `audit_written=false`），且不影响业务结果
- `winuse_audit` 的查询路径与写入端一致（同一 `env_path`）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from winuse import audit as A
from winuse.mcp import server as S

ENV_BODY = "WINUSE_AUDIT_PATH={audit}\n"


@dataclass
class FakeDriver:
    windows: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def call(self, tool: str, payload: dict | None = None):
        self.calls.append((tool, payload or {}))
        if tool == "list_windows":
            return {"windows": self.windows, "ok": True}
        if tool == "get_window_state":
            return {"elements": [], "snapshot_id": "s00000001", "ok": True}
        return {"ok": True}

    def health(self):
        return {"ok": True, "path": "<fake>", "version": "0.0.0"}


def _window(pid=100, window_id=11, app="fake.exe"):
    return {
        "pid": pid,
        "window_id": window_id,
        "app_name": app,
        "title": "占位窗口",
        "minimized": False,
        "is_on_screen": True,
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        "z_index": 1,
    }


@pytest.fixture()
def env_file(tmp_path):
    """一份项目内 .env，审计路径指向临时文件（不碰用户真实 ~/.winuse）。"""
    audit_path = tmp_path / "audit.jsonl"
    env = tmp_path / ".env"
    env.write_text(ENV_BODY.format(audit=str(audit_path).replace("\\", "/")), encoding="utf-8")
    return env, audit_path


def _session(env_file, **kw) -> S.Session:
    env, _ = env_file
    # initialized=True：这里测的是审计接线，握手顺序由 test_mcp_lifecycle.py 守
    return S.Session(driver=FakeDriver(windows=[_window()]), env_path=str(env),
                     initialized=True, **kw)


def _call(session, name: str, arguments: dict, msg_id: int = 1) -> dict:
    return S.handle_message(
        session,
        {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
    )


def _entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ------------------------------------------------------------------ 覆盖范围


def test_mcp_act_writes_audit(env_file):
    session = _session(env_file)
    out = _call(session, "winuse_act",
                {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1"})
    assert "error" not in out, out
    events = [e["event"] for e in _entries(env_file[1])]
    assert events == ["act.dry_run"], "MCP 的 act 必须落审计（原先没有 sink，什么都不写）"
    entry = _entries(env_file[1])[0]
    assert entry["source"] == "mcp"
    assert entry["request_id"] == 1, "要能对上'哪条请求干的'"
    assert entry["action"] == "click"


def test_mcp_act_executed_is_marked(env_file):
    session = _session(env_file)
    _call(session, "winuse_act",
          {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1", "execute": True})
    entry = _entries(env_file[1])[0]
    assert entry["event"] == "act.executed"
    assert entry["ok"] is True


def test_mcp_window_writes_audit(env_file):
    session = _session(env_file)
    _call(session, "winuse_window", {"action": "minimize", "app": "fake", "execute": True})
    events = [e["event"] for e in _entries(env_file[1])]
    assert any(e.startswith("window.") for e in events), f"窗口动作必须落审计，实际：{events}"


def test_mcp_window_blocked_is_audited(env_file):
    session = _session(env_file)
    _call(session, "winuse_window", {"action": "close", "app": "fake", "execute": True})
    entries = _entries(env_file[1])
    assert [e["event"] for e in entries] == ["window.blocked"]
    assert entries[0]["code"] == "CONFIRM_REQUIRED"


def test_mcp_multi_audits_batch_and_every_step(env_file):
    session = _session(env_file)
    _call(session, "winuse_multi",
          {"steps": [{"action": "click", "element_index": 0}, {"action": "click", "element_index": 1}],
           "app": "fake", "snapshot": "s1", "confirm": True})
    events = [e["event"] for e in _entries(env_file[1])]
    # 顺序：逐步 `act.*` 在前，批层汇总 `batch.*` 在最后（汇总要等跑完才有 ok/stopped_at）
    assert events == ["act.dry_run", "act.dry_run", "batch.dry_run"], f"实际：{events}"
    batch_entry = _entries(env_file[1])[-1]
    assert batch_entry["detail"]["steps"] == 2
    assert batch_entry["detail"]["actions"] == ["click", "click"], "批层要能看出这是一次批量提交"


def test_mcp_multi_blocked_is_audited(env_file):
    session = _session(env_file)
    _call(session, "winuse_multi",
          {"steps": [{"action": "click", "element_index": 0}], "app": "fake", "snapshot": "s1"})
    entries = _entries(env_file[1])
    assert [e["event"] for e in entries] == ["batch.blocked"]
    assert entries[0]["code"] == "CONFIRM_REQUIRED"


def test_mcp_notify_execute_is_audited(env_file):
    session = _session(env_file)
    _call(session, "winuse_notify",
          {"title": "占位标题", "body": "占位正文", "level": "info", "execute": True})
    events = [e["event"] for e in _entries(env_file[1])]
    assert any(e.startswith("notify.") for e in events), f"通知执行必须落审计，实际：{events}"


def test_readonly_tools_write_no_audit(env_file):
    """只读工具不该往审计里灌噪音（审计是给'变更'用的）。"""
    session = _session(env_file)
    _call(session, "winuse_state", {"app": "fake", "max_elements": 1})
    _call(session, "winuse_display", {})
    assert _entries(env_file[1]) == []


# ------------------------------------------------------------ 写入 / 查询同源


def test_audit_query_uses_the_same_env_path(env_file):
    """写入端与查询端必须是同一个文件（原先查询固定 default_path()）。"""
    session = _session(env_file)
    _call(session, "winuse_act",
          {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1"})
    out = _call(session, "winuse_audit", {"recent": 5})
    payload = json.loads(out["result"]["content"][0]["text"])
    assert payload["returned"] == 1, "查到的必须是刚写进去的那条（同源）"
    assert Path(payload["path"]) == env_file[1]


def test_audit_query_falls_back_without_env(monkeypatch, tmp_path):
    """没有 env_path 时仍用默认路径（不能因为改动把默认路径弄丢）。"""
    default = tmp_path / "default.jsonl"
    monkeypatch.setattr(A, "default_path", lambda env_path=None: default)
    A.record({"event": "act.dry_run"}, path=default)
    session = S.Session(driver=FakeDriver(), env_path=None, initialized=True)
    out = _call(session, "winuse_audit", {"recent": 5})
    payload = json.loads(out["result"]["content"][0]["text"])
    assert Path(payload["path"]) == default


# ---------------------------------------------------------------- 失败可观察


def test_audit_write_failure_is_observable(env_file, monkeypatch):
    """审计写失败不能静默：回执要带 audit_written=false，业务本身照常返回。"""
    session = _session(env_file)
    monkeypatch.setattr(A, "record", lambda *a, **k: None)  # 落盘失败（返回 None）
    out = _call(session, "winuse_act",
                {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1"})
    payload = json.loads(out["result"]["content"][0]["text"])
    assert payload["ok"] is True, "审计失败不该阻断业务"
    assert payload["audit_written"] is False, "但必须在回执里可见"


class TestCliAudit:
    """CLI 门面同样要留痕（原先只有 `act` 接了 sink）。"""

    def _env(self, tmp_path) -> tuple[Path, Path]:
        audit_path = tmp_path / "cli-audit.jsonl"
        env = tmp_path / ".env"
        env.write_text(f'WINUSE_AUDIT_PATH={audit_path.as_posix()}\nWINUSE_CUA_DRIVER="{tmp_path}/nope"\n',
                       encoding="utf-8")
        return env, audit_path

    def test_cli_window_blocked_is_audited(self, tmp_path):
        from winuse import cli

        env, path = self._env(tmp_path)
        code = cli.main(["--env", str(env), "window", "--action", "close", "--pid", "1"])
        assert code == cli.EXIT_FAIL
        entries = _entries(path)
        assert [e["event"] for e in entries] == ["window.blocked"]
        assert entries[0]["source"] == "cli", "要能分清是命令行还是 MCP 客户端干的"

    def test_cli_multi_blocked_is_audited(self, tmp_path):
        from winuse import cli

        env, path = self._env(tmp_path)
        steps = json.dumps([{"action": "click", "element_index": 0}])
        code = cli.main(["--env", str(env), "multi", "--steps", steps, "--pid", "1"])
        assert code == cli.EXIT_FAIL
        entries = _entries(path)
        assert [e["event"] for e in entries] == ["batch.blocked"]
        assert entries[0]["detail"]["steps"] == 1

    def test_cli_sink_reports_failure_explicitly(self, tmp_path, monkeypatch):
        """落盘失败的 sink 必须返回 False（否则回执里的 audit_written 永远是 true）。"""
        from winuse import cli

        env, _ = self._env(tmp_path)
        monkeypatch.setattr(A, "record", lambda *a, **k: None)
        sink = cli._audit_sink(type("NS", (), {"env": str(env)})())
        assert sink({"event": "x"}) is False


def test_audit_written_true_when_persisted(env_file):
    session = _session(env_file)
    out = _call(session, "winuse_act",
                {"action": "click", "app": "fake", "element_index": 0, "snapshot": "s1"})
    payload = json.loads(out["result"]["content"][0]["text"])
    assert payload["audit_written"] is True
