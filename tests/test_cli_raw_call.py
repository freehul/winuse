"""CLI 原始驱动入口隔离（C-2）。

背景：`winuse call <tool> <payload>` 直接把任意驱动工具与参数交给驱动层，
不经过 `core.guard`、写值门禁，也没有审计 —— 项目宣称的"统一安全门面"在 CLI 侧
有一条公开旁路。任何能启动 CLI 的 agent 都能用它绕过 `execute/confirm/overwrite`。

处置（保留排查能力，但逼调用方显式承认）：
1. 不带 `--unsafe-driver-call` → 直接退出码 2，**一次驱动调用都不许发生**
2. 带开关才执行，且必须落一条 `driver.raw_call` 审计（事后可查谁调了什么）
3. `help` 里必须写明"绕过安全层"，不能只写"透传调用"
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from winuse import cli


class RecordingDriver:
    """假驱动：只记调用，不碰桌面。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, payload: dict | None = None):
        self.calls.append((tool, payload or {}))
        return {"ok": True, "tool": tool, "echo": payload or {}}

    def health(self):
        return {"ok": True, "path": "<fake>", "version": "9.9.9"}


@pytest.fixture()
def driver(monkeypatch):
    fake = RecordingDriver()
    monkeypatch.setattr(
        "winuse.config.Config.driver", lambda self, *a, **k: fake, raising=True
    )
    return fake


def test_without_opt_in_is_refused(driver, capsys):
    code = cli.main(["call", "list_windows"])
    out = capsys.readouterr()
    assert code == cli.EXIT_USAGE, "未显式承认就必须拒绝，不能默认放行"
    assert driver.calls == [], "拒绝时绝不能已经把调用发出去了"
    assert "绕过" in out.err, f"拒绝原因必须说清绕过安全层：{out.err!r}"


def test_call_help_mentions_bypass(capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["call", "--help"])
    text = capsys.readouterr().out
    assert "绕过安全层" in text, "help 必须写明这条命令绕过安全层，不能只写『透传调用』"
    assert cli.UNSAFE_FLAG in text


def test_opt_in_executes_and_is_audited(driver, tmp_path, monkeypatch):
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setenv("WINUSE_AUDIT_PATH", str(audit_file))
    monkeypatch.setattr("winuse.audit.default_path", lambda *a, **k: audit_file, raising=True)

    code = cli.main(["call", "list_windows", "--unsafe-driver-call"])
    assert code == cli.EXIT_OK
    assert driver.calls == [("list_windows", {})]

    lines = [json.loads(x) for x in audit_file.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines, "显式授权后的原始调用必须留痕"
    assert lines[-1]["event"] == "driver.raw_call"
    assert lines[-1]["action"] == "list_windows"
