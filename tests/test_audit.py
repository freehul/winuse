"""审计日志测试（PR-7）。

两条纪律：
1. 审计**不得阻断业务**：写盘失败返回 None，不抛异常
2. 审计**不得成为泄露源**：疑似凭据必须脱敏，长文本截断（样例即泄露的教训）
"""

from __future__ import annotations

import json

from winuse import audit as A


class TestRecord:
    def test_appends_one_json_line(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        A.record({"event": "act.dry_run", "ok": True}, path=path)
        A.record({"event": "act.executed", "ok": True}, path=path)
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 2
        assert [json.loads(ln)["event"] for ln in lines] == ["act.dry_run", "act.executed"]

    def test_creates_parent_dir(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "audit.jsonl"
        entry = A.record({"event": "x"}, path=path)
        assert path.is_file()
        assert entry and entry["event"] == "x"

    def test_adds_timestamp_and_normalizes_shape(self, tmp_path):
        entry = A.record({"event": "act.dry_run", "action": "click"}, path=tmp_path / "a.jsonl")
        assert entry["ts"].startswith("20") and "T" in entry["ts"]
        assert entry["event"] == "act.dry_run"
        assert entry["action"] == "click"

    def test_never_raises_when_path_is_a_directory(self, tmp_path):
        assert A.record({"event": "x"}, path=tmp_path) is None

    def test_never_raises_on_unwritable_path(self, tmp_path):
        # Windows 保留名会让创建失败；不管平台如何，都不许把异常抛给业务
        bad = tmp_path / "nul" / "audit.jsonl"
        result = A.record({"event": "x"}, path=bad)
        assert result is None or result.get("event") == "x"

    def test_json_can_be_parsed_back(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        A.record({"event": "act.blocked", "code": "CONFIRM_REQUIRED", "target": {"app": "explorer.exe"}}, path=path)
        obj = json.loads(path.read_text(encoding="utf-8").strip())
        assert obj["code"] == "CONFIRM_REQUIRED"
        assert obj["target"]["app"] == "explorer.exe"


def _fake(prefix: str, body: str) -> str:
    """拼出假凭据：**不把疑似真实 key 的字面量写进测试文件**（样例即泄露）。"""
    return f"{prefix}{body}"


class TestRedaction:
    def test_masks_api_key_shapes(self):
        for secret in (_fake("sk", "-abcdef123456"), _fake("ark", "-abcdef123456"), _fake("agt_", "abcdef123456")):
            out = A.redact_text(f"调用失败：{secret}")
            assert secret not in out
            assert "[REDACTED]" in out

    def test_masks_bearer_tokens(self):
        token = _fake("", "abcdefgh12345678")
        out = A.redact_text(f"Authorization: Bearer {token}")
        assert token not in out

    def test_masks_password_and_token_assignments(self):
        assert "hunter2abc" not in A.redact_text("password=hunter2abc")
        assert "zzz999" not in A.redact_text("token: zzz999")
        assert "q1w2e3" not in A.redact_text("api_key = q1w2e3")

    def test_keeps_ordinary_text_readable(self):
        text = "点击开始菜单里的记事本"
        assert A.redact_text(text) == text

    def test_truncates_long_text(self):
        out = A.redact_text("长" * 500, limit=50)
        assert len(out) < 100
        assert "500" in out, "截断要标明原长度，便于排查"

    def test_none_and_empty_are_safe(self):
        assert A.redact_text(None) is None
        assert A.redact_text("") == ""


class TestBuildEntry:
    def test_build_entry_redacts_nested_values(self):
        entry = A.build_entry(
            event="act.executed",
            action="set_value",
            ok=True,
            target={"app": "notepad.exe", "pid": 1, "window_id": 2, "title": "x" * 200},
            detail={"value": "sk-abcdef123456"},
        )
        assert "sk-abcdef123456" not in json.dumps(entry, ensure_ascii=False)
        assert len(entry["target"]["title"]) < 200, "标题也可能带路径等隐私，要截断"
        assert entry["event"] == "act.executed"

    def test_default_path_is_under_home_winuse(self):
        p = A.default_path()
        assert p.name == "audit.jsonl" and ".winuse" in str(p)

    def test_env_file_can_override_audit_path(self, tmp_path):
        """路径覆盖走**项目内 .env**（沿用项目纪律：不读进程环境变量）。"""
        env = tmp_path / ".env"
        custom = tmp_path / "logs" / "winuse-audit.jsonl"
        env.write_text(f"# 注释行\nWINUSE_AUDIT_PATH={custom}\n", encoding="utf-8")
        assert A.default_path(env_path=env) == custom

        entry = A.record({"event": "x"}, path=custom)
        assert entry is not None and custom.exists(), "显式路径应可落盘"
