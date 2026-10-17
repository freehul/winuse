"""审计日志治理测试（PR-9 / T-22）：轮转 + 最近 N 条查询。

三条纪律（与 winuse/audit.py 的模块头一一对应）：
1. 轮转**不阻断业务**：轮转失败照样写日志，绝不抛异常给调用方
2. 轮转**不丢记录**（归档份数够时）：跨归档读出来的是一条连续历史
3. 查询入口**不得成为泄露源**：读出前再脱敏一遍（日志文件可能被手工改动）

数据卫生：一律 tmp_path + 占位符，不写真实路径/内网地址/凭据。
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from winuse import audit as A
from winuse import cli


def _env(tmp_path, **pairs) -> Path:
    """造一份项目内 .env（审计路径与轮转策略都只从这里读）。"""
    env = tmp_path / ".env"
    body = "# 测试用 .env\n" + "".join(f"{k}={v}\n" for k, v in pairs.items())
    env.write_text(body, encoding="utf-8")
    return env


def _lines(path) -> list[str]:
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _count(path) -> int:
    return len(_lines(path))


def _indexes(path) -> list[int]:
    return [json.loads(ln)["i"] for ln in _lines(path)]


def _total(path) -> int:
    """主文件 + 全部归档的条数合计。"""
    return _count(path) + sum(_count(p) for p in A.rotated_paths(path))


class TestRotationPolicy:
    def test_defaults_when_no_env(self):
        assert A.rotation_policy() == {"max_bytes": A.DEFAULT_MAX_BYTES, "keep": A.DEFAULT_KEEP}

    def test_env_file_overrides(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=2048, WINUSE_AUDIT_KEEP=2)
        assert A.rotation_policy(env) == {"max_bytes": 2048, "keep": 2}

    def test_bad_values_fall_back_to_defaults(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES="big", WINUSE_AUDIT_KEEP="x")
        assert A.rotation_policy(env) == {"max_bytes": A.DEFAULT_MAX_BYTES, "keep": A.DEFAULT_KEEP}

    def test_zero_disables_rotation_and_negative_keep_clamped(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=0, WINUSE_AUDIT_KEEP=-5)
        assert A.rotation_policy(env) == {"max_bytes": 0, "keep": 0}

    def test_ignores_process_environment(self, monkeypatch):
        """纪律：只读项目内 .env，刻意不读进程环境变量。"""
        monkeypatch.setenv("WINUSE_AUDIT_MAX_BYTES", "1")
        monkeypatch.setenv("WINUSE_AUDIT_KEEP", "99")
        assert A.rotation_policy() == {"max_bytes": A.DEFAULT_MAX_BYTES, "keep": A.DEFAULT_KEEP}

    def test_missing_env_file_is_not_fatal(self, tmp_path):
        assert A.rotation_policy(tmp_path / "nope.env") == {
            "max_bytes": A.DEFAULT_MAX_BYTES,
            "keep": A.DEFAULT_KEEP,
        }


class TestRotateIfNeeded:
    def test_false_when_file_missing_or_under_limit(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        assert A.rotate_if_needed(path) is False, "文件不存在不算轮转"
        path.write_text("x" * 20, encoding="utf-8")
        assert A.rotate_if_needed(path, max_bytes=1024) is False
        assert A.rotated_paths(path) == []

    def test_zero_max_bytes_disables_rotation(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        path.write_text("x" * 200, encoding="utf-8")
        assert A.rotate_if_needed(path, max_bytes=0) is False, "0 = 关闭轮转"
        assert A.rotate_if_needed(path, max_bytes=-1) is False
        assert A.rotated_paths(path) == []

    def test_rotates_and_reports_true(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        path.write_text("x" * 200, encoding="utf-8")
        assert A.rotate_if_needed(path, max_bytes=100) is True
        assert not path.exists(), "滚走的当前文件应让位给新日志"
        assert path.with_name("audit.jsonl.1").is_file()
        assert A.rotated_paths(path) == [path.with_name("audit.jsonl.1")]

    def test_incoming_bytes_reserves_room(self, tmp_path):
        """用「当前大小 + 本行字节数」判断，避免写完一整行才超限。"""
        path = tmp_path / "audit.jsonl"
        path.write_text("x" * 50, encoding="utf-8")
        assert A.rotate_if_needed(path, max_bytes=120, incoming_bytes=10) is False
        assert A.rotate_if_needed(path, max_bytes=120, incoming_bytes=100) is True

    def test_empty_file_is_not_rotated(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        path.write_text("", encoding="utf-8")
        assert A.rotate_if_needed(path, max_bytes=1, incoming_bytes=999) is False
        assert A.rotated_paths(path) == []

    def test_shifts_chain_and_drops_oldest(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        archive_1 = path.with_name("audit.jsonl.1")
        archive_2 = path.with_name("audit.jsonl.2")
        archive_3 = path.with_name("audit.jsonl.3")
        archive_1.write_text('{"i": 1}\n', encoding="utf-8")
        archive_2.write_text('{"i": 2}\n', encoding="utf-8")
        archive_3.write_text('{"i": 3}\n', encoding="utf-8")
        body = '{"i": 0, "pad": "' + "x" * 60 + '"}\n'  # 超过上限的当前文件
        path.write_text(body, encoding="utf-8")

        assert A.rotate_if_needed(path, max_bytes=10, keep=3) is True
        assert [p.name for p in A.rotated_paths(path)] == ["audit.jsonl.1", "audit.jsonl.2", "audit.jsonl.3"]
        assert _indexes(archive_1) == [0], "当前文件滚成新的 .1（最近一次轮转）"
        assert _indexes(archive_2) == [1], "原来的 .1 后移"
        assert _indexes(archive_3) == [2], "最旧的 .3 被挤掉"
        assert not path.with_name("audit.jsonl.4").exists()

    def test_cleans_up_over_quota_leftovers(self, tmp_path):
        """保留份数被调小后，超编的历史归档不该一直躺着。"""
        path = tmp_path / "audit.jsonl"
        for index in (1, 2, 3, 4, 5):
            path.with_name(f"audit.jsonl.{index}").write_text("{}\n", encoding="utf-8")
        path.write_text("x" * 50, encoding="utf-8")

        assert A.rotate_if_needed(path, max_bytes=10, keep=2) is True
        assert [p.name for p in A.rotated_paths(path)] == ["audit.jsonl.1", "audit.jsonl.2"]

    def test_never_raises_on_bad_targets(self, tmp_path):
        assert A.rotate_if_needed(tmp_path, max_bytes=1) is False, "目标是目录也不能抛"
        assert A.rotate_if_needed(tmp_path / "no" / "such" / "audit.jsonl", max_bytes=1) is False

    def test_rotated_paths_ignores_unrelated_names(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        (tmp_path / "audit.jsonl.1").write_text("{}\n", encoding="utf-8")
        (tmp_path / "audit.jsonl.10").write_text("{}\n", encoding="utf-8")
        (tmp_path / "audit.jsonl.bak").write_text("{}\n", encoding="utf-8")
        (tmp_path / "audit.jsonl.2").mkdir()
        assert [p.name for p in A.rotated_paths(path)] == ["audit.jsonl.1", "audit.jsonl.10"]

    def test_rotated_paths_on_missing_parent(self, tmp_path):
        assert A.rotated_paths(tmp_path / "no" / "such" / "audit.jsonl") == []


class TestRecordRotation:
    def test_caps_main_file_and_keeps_every_record(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=200, WINUSE_AUDIT_KEEP=20)
        path = tmp_path / "audit.jsonl"

        for i in range(40):
            assert A.record({"event": "act.dry_run", "i": i}, path=path, env_path=env) is not None

        assert len(A.rotated_paths(path)) >= 5, "前置条件：确实轮转过多次"
        assert path.stat().st_size <= 200, "主文件必须受上限约束"
        assert _total(path) == 40, "归档份数够时，轮转不该丢任何记录"

    def test_archive_index_1_is_newest(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=200, WINUSE_AUDIT_KEEP=3)
        path = tmp_path / "audit.jsonl"
        for i in range(30):
            A.record({"event": "e", "i": i}, path=path, env_path=env)

        first, second = A.rotated_paths(path)[:2]
        assert max(_indexes(second)) < min(_indexes(first)), "索引越大越旧"

    def test_keep_limits_archive_count(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=200, WINUSE_AUDIT_KEEP=2)
        path = tmp_path / "audit.jsonl"
        for i in range(40):
            A.record({"event": "e", "i": i}, path=path, env_path=env)

        assert [p.name for p in A.rotated_paths(path)] == ["audit.jsonl.1", "audit.jsonl.2"], "只保留 2 份"

    def test_keep_zero_discards_history(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=150, WINUSE_AUDIT_KEEP=0)
        path = tmp_path / "audit.jsonl"
        for i in range(40):
            A.record({"event": "e", "i": i}, path=path, env_path=env)

        assert A.rotated_paths(path) == [], "keep=0 = 不保留归档"
        assert 0 < _count(path) < 40

    def test_default_policy_does_not_rotate_small_log(self, tmp_path):
        """默认上限 5 MiB：正常体量不该无谓轮转（多余归档会干扰取证）。"""
        path = tmp_path / "audit.jsonl"
        for i in range(50):
            A.record({"event": "e", "i": i}, path=path)

        assert A.rotated_paths(path) == []
        assert _count(path) == 50

    def test_disabled_rotation_keeps_appending(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=0, WINUSE_AUDIT_KEEP=3)
        path = tmp_path / "audit.jsonl"
        for i in range(40):
            A.record({"event": "e", "i": i}, path=path, env_path=env)

        assert A.rotated_paths(path) == []
        assert _count(path) == 40

    def test_still_never_raises_when_target_is_a_directory(self, tmp_path):
        assert A.record({"event": "x"}, path=tmp_path) is None


class TestReadRecent:
    def test_returns_chronological_tail(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        for i in range(5):
            A.record({"event": "e", "i": i}, path=path)

        assert [e["i"] for e in A.read_recent(3, path=path)] == [2, 3, 4], "最后一条必须是最新的"

    def test_spans_rotated_archives(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=200, WINUSE_AUDIT_KEEP=20)
        path = tmp_path / "audit.jsonl"
        for i in range(40):
            A.record({"event": "e", "i": i}, path=path, env_path=env)

        out = A.read_recent(12, path=path)
        assert [e["i"] for e in out] == list(range(28, 40)), "跨归档读出来要是一条连续历史"

    def test_count_larger_than_log_returns_everything(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        for i in range(3):
            A.record({"event": "e", "i": i}, path=path)

        assert len(A.read_recent(99, path=path)) == 3

    @pytest.mark.parametrize("count", [0, -3, "abc", None])
    def test_non_positive_or_bad_count_returns_empty(self, tmp_path, count):
        path = tmp_path / "audit.jsonl"
        A.record({"event": "e"}, path=path)
        assert A.read_recent(count, path=path) == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert A.read_recent(5, path=tmp_path / "nope.jsonl") == []

    def test_directory_target_returns_empty(self, tmp_path):
        assert A.read_recent(5, path=tmp_path) == []

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        path.write_text('\n{"event": "a"}\n\n\n{"event": "b"}\n', encoding="utf-8")
        assert [e["event"] for e in A.read_recent(50, path=path)] == ["a", "b"]

    def test_broken_line_is_kept_as_unparsable(self, tmp_path):
        """坏行不丢——悄悄跳过会让人以为"没记录"，实际是记录坏了。"""
        path = tmp_path / "audit.jsonl"
        path.write_text('{"event": "ok"}\n半截行不是 JSON\n', encoding="utf-8")

        out = A.read_recent(10, path=path)
        assert out[0]["event"] == "ok"
        assert out[1].get("unparsable") is True
        assert "半截行" in out[1]["raw"]

    def test_non_object_json_line_is_unparsable(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        path.write_text('"just a string"\n', encoding="utf-8")
        assert A.read_recent(1, path=path)[0]["unparsable"] is True

    def test_hand_edited_secret_is_redacted_on_read(self, tmp_path):
        """纪律：查询入口也必须脱敏——日志文件可能被手工改动/篡改。"""
        secret = "sk" + "-abcdef123456"
        path = tmp_path / "audit.jsonl"
        path.write_text(
            json.dumps({"event": "e", "detail": {"note": f"key={secret}"}}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        blob = json.dumps(A.read_recent(1, path=path), ensure_ascii=False)
        assert secret not in blob
        assert "[REDACTED]" in blob

    def test_raw_broken_line_is_redacted_too(self, tmp_path):
        secret = "ark" + "-abcdef123456"
        path = tmp_path / "audit.jsonl"
        path.write_text(f"{{坏行 {secret}\n", encoding="utf-8")

        out = A.read_recent(1, path=path)
        assert secret not in json.dumps(out, ensure_ascii=False)

    def test_env_file_decides_which_log_is_read(self, tmp_path):
        custom = tmp_path / "logs" / "winuse-audit.jsonl"
        custom.parent.mkdir(parents=True, exist_ok=True)
        custom.write_text('{"event": "from-env", "i": 7}\n', encoding="utf-8")
        env = _env(tmp_path, WINUSE_AUDIT_PATH=custom)

        out = A.read_recent(1, env_path=env)
        assert [e["i"] for e in out] == [7], "路径口径走 .env 的 WINUSE_AUDIT_PATH"


class TestDescribeLog:
    def test_reports_size_lines_and_archives(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=200, WINUSE_AUDIT_KEEP=5)
        path = tmp_path / "audit.jsonl"
        for i in range(20):
            A.record({"event": "e", "i": i}, path=path, env_path=env)

        info = A.describe_log(path, env_path=env)
        assert info["exists"] is True
        assert info["size_bytes"] == path.stat().st_size
        assert info["lines"] == _count(path) >= 1
        assert len(info["rotated_files"]) == len(A.rotated_paths(path)) >= 1
        assert info["rotated_files"][0]["path"].endswith(".1")
        assert info["max_bytes"] == 200 and info["keep"] == 5
        assert info["error"] is None

    def test_missing_file_is_not_an_error(self, tmp_path):
        info = A.describe_log(tmp_path / "nope.jsonl")
        assert info["exists"] is False and info["lines"] == 0 and info["error"] is None

    def test_directory_target_reports_error_without_raising(self, tmp_path):
        info = A.describe_log(tmp_path)
        assert info["exists"] is False
        assert info["error"], "目标不是文件时不能假装是一份空日志"

    def test_reports_policy_from_env(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=4096, WINUSE_AUDIT_KEEP=1)
        info = A.describe_log(tmp_path / "audit.jsonl", env_path=env)
        assert info["max_bytes"] == 4096 and info["keep"] == 1


class TestAuditCommand:
    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_help_exits_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with pytest.raises(SystemExit) as cm:
                cli.main(["audit", "--help"])
        assert cm.value.code == 0

    def test_recent_reads_tail_via_env_path(self, tmp_path):
        custom = tmp_path / "logs" / "winuse-audit.jsonl"
        custom.parent.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            A.record({"event": "act.dry_run", "i": i}, path=custom)
        env = _env(tmp_path, WINUSE_AUDIT_PATH=custom)

        rc, out = self._run(["--env", str(env), "audit", "--recent", "2"])
        payload = json.loads(out)
        assert rc == 0 and payload["ok"] is True
        assert payload["returned"] == 2 and payload["requested"] == 2
        assert [e["i"] for e in payload["entries"]] == [1, 2]
        assert payload["path"] == str(custom)

    def test_recent_zero_is_usage_error(self, tmp_path):
        rc, out = self._run(["audit", "--recent", "0", "--path", str(tmp_path / "a.jsonl")])
        assert rc == 2, "用法错误用 2 退出码，别混成业务失败"
        assert json.loads(out)["error"]["code"] == "BAD_PARAM"

    def test_empty_log_is_ok_with_zero_entries(self, tmp_path):
        rc, out = self._run(["audit", "--recent", "5", "--path", str(tmp_path / "a.jsonl")])
        payload = json.loads(out)
        assert rc == 0 and payload["ok"] is True and payload["entries"] == []

    def test_stats_reports_rotation(self, tmp_path):
        env = _env(tmp_path, WINUSE_AUDIT_MAX_BYTES=200, WINUSE_AUDIT_KEEP=2)
        path = tmp_path / "audit.jsonl"
        for i in range(15):
            A.record({"event": "e", "i": i}, path=path, env_path=env)

        rc, out = self._run(["--env", str(env), "audit", "--stats", "--path", str(path)])
        payload = json.loads(out)
        assert rc == 0 and payload["ok"] is True
        assert payload["rotation"]["lines"] >= 1
        assert payload["rotation"]["rotated_files"], "轮转过的日志必须能看出归档"
        assert payload["rotation"]["max_bytes"] == 200

    def test_query_is_read_only(self, tmp_path):
        """查询不得改动日志——审计文件是取证材料。"""
        path = tmp_path / "audit.jsonl"
        A.record({"event": "e"}, path=path)
        before = path.read_bytes()

        rc, _ = self._run(["audit", "--recent", "5", "--path", str(path)])
        assert rc == 0
        assert path.read_bytes() == before
        assert A.rotated_paths(path) == []
