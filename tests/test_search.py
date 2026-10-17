"""winuse_search（Everything 全盘搜索）单测：命令构造、输出解析、es.exe 定位、失败分流。

铁律：**不在单测里真跑 es.exe**。全盘搜索的耗时与结果都取决于本机索引状态
（是否装了 Everything、索引了哪些盘、机器上恰好有什么文件），真跑既慢又不可复现。
所以这里只有三条路：

1. 纯函数（`build_search_command` / `parse_search_output` / `split_query_terms` /
   `size_filter_terms` / `normalize_sort` / `decode_output`）逐条断言；
2. `monkeypatch subprocess.run` **假造子进程**，验证真跑分支（超时、退出码、解码、截断）；
3. 注入 `exists` / `which` 假实现（`resolve_es`），验证定位顺序，不碰真实文件系统。

真机实测样例（命令与输出形态）固化为常量，注释里写清来源，见 `REAL_*`。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core import search as search_mod  # noqa: E402
from winuse.driver.errors import InvalidParams, MissingParam, WinUseError  # noqa: E402

# ---------------------------------------------------------------------------
# 真机实测样例（本机 Everything 1.4.1.1032 / es.exe 1.1.0.30）
# ---------------------------------------------------------------------------
# 命令：es.exe -n 3 -csv -no-header -size -size-format 1 -date-modified \
#               -date-format 1 -path "D:\work\proj\docs" "*.md" "size:>10kb"
# 输出（stdout，CRLF 分行；Size 列为字节、Date 列为 ISO-8601 本地时间、路径被双引号包裹）：
REAL_CSV_OUTPUT = (
    '55494,2026-09-27T15:58:51,"D:\\Projects\\WinUse\\docs\\requirements\\notes.md"\r\n'
    '25355,2026-09-27T04:23:42,"D:\\Projects\\WinUse\\docs\\design\\WinUse-\u5ba1\u67e5\u62a5\u544a-20260927.md"\r\n'
    ',2026-09-27T13:36:05,"D:\\Projects\\WinUse\\winuse"\r\n'  # 目录：Size 列为空
)

# 命令：es.exe -n 3 -path "D:\work\proj\docs" "*.md"（不带列 → 每行一个完整路径）
REAL_PATH_LINES_OUTPUT = (
    "D:\\Projects\\WinUse\\docs\\design\\README.md\r\n"
    "D:\\Projects\\WinUse\\docs\\requirements\\notes.md\r\n"
)

# 命令：es.exe -n 3 -csv -size -date-modified -path ... "*.md"（**带表头**，用于验证兜底）
REAL_CSV_WITH_HEADER_OUTPUT = (
    "Size,Date Modified,Filename\r\n"
    + REAL_CSV_OUTPUT
)

# 真机故障样例：Everything 没在跑（或实例名不对）时 es.exe 的表现
# 命令：es.exe -n 2 -instance winuse_probe_missing "x"
# 结果：退出码 8，stderr = "Error 8: Everything IPC window not found. Please make sure Everything is running."
REAL_IPC_ERROR_STDERR = b"Error 8: Everything IPC window not found. Please make sure Everything is running."
REAL_IPC_ERROR_RC = 8

# 真机样例：中文路径在 stdout 里是**系统 ANSI 代码页**（本机 cp936）字节，不是 UTF-8
REAL_CHINESE_PATH = "D:\\samples\\\u4e2d\u6587 \u76ee\u5f55\u6d4b\u8bd5.txt"


class FakeProc:
    """最小 CompletedProcess 替身（只需要真跑分支读的三个字段）。"""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class Recorder:
    """记录 subprocess.run 的调用（argv 与 kwargs），并返回预设结果。"""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, exc=None):
        self.calls: list[dict] = []
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.exc = exc

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": dict(kwargs)})
        if self.exc is not None:
            raise self.exc
        return FakeProc(self.stdout, self.stderr, self.returncode)

    @property
    def argv(self) -> list[str]:
        assert self.calls, "subprocess.run 一次都没被调用"
        return self.calls[0]["argv"]


def patch_run(monkeypatch, recorder) -> Recorder:
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


def frozen_es(monkeypatch, path: str = "D:/tools/es.exe") -> str:
    """把 es.exe 定位结果冻结成 path（本用例不测定位，只测搜索链路）。"""
    monkeypatch.setattr(
        search_mod,
        "resolve_es",
        lambda **kwargs: (path, [{"source": "param", "path": path, "exists": True}]),
    )
    return path


# ===========================================================================
# 一、纯函数：搜索词与参数 → argv
# ===========================================================================


class TestSplitQueryTerms:
    def test_plain_words_split_into_and_terms(self):
        # 真机实测：es.exe 把**多个 argv 词**当 AND 条件，而**一个含空格的 argv**
        # 被 Everything 当整串短语（`*.md size:>1kb` 整串 → 0 条；拆成两个词 → 4 条）
        assert search_mod.split_query_terms("*.md size:>1kb") == ["*.md", "size:>1kb"]

    def test_single_word_stays_one_term(self):
        assert search_mod.split_query_terms("winuse") == ["winuse"]

    def test_quoted_group_keeps_spaces_as_one_phrase(self):
        assert search_mod.split_query_terms('"winuse report" *.md') == ["winuse report", "*.md"]

    def test_quotes_are_stripped_not_passed_through(self):
        # 真机实测：把带引号的串原样当一个 argv 传下去 → 0 条（es 把它当字面量）
        assert search_mod.split_query_terms('"中文 目录"') == ["中文 目录"]

    def test_blank_query_yields_no_terms(self):
        assert search_mod.split_query_terms("") == []
        assert search_mod.split_query_terms("   ") == []

    def test_whitespace_is_collapsed(self):
        assert search_mod.split_query_terms("  a\t\tb \n c ") == ["a", "b", "c"]


class TestSizeFilterTerms:
    def test_min_only(self):
        assert search_mod.size_filter_terms(1024, None) == ["size:>=1024"]

    def test_max_only(self):
        assert search_mod.size_filter_terms(None, 2048) == ["size:<=2048"]

    def test_both_becomes_range(self):
        # 真机实测：size:1000..20000 命中 2 条 == size:>=1000(4) ∩ size:<=20000(3)
        assert search_mod.size_filter_terms(1000, 20000) == ["size:1000..20000"]

    def test_zero_is_a_legal_value(self):
        assert search_mod.size_filter_terms(0, None) == ["size:>=0"]

    def test_none_none_yields_nothing(self):
        assert search_mod.size_filter_terms(None, None) == []

    def test_reversed_range_is_rejected(self):
        with pytest.raises(InvalidParams):
            search_mod.size_filter_terms(2048, 1024)

    def test_non_numeric_size_is_rejected(self):
        for bad in ("1kb", "abc", -1, [1]):
            with pytest.raises(InvalidParams):
                search_mod.size_filter_terms(bad, None)

    def test_numeric_string_is_accepted(self):
        assert search_mod.size_filter_terms("512", None) == ["size:>=512"]


class TestNormalizeSort:
    def test_value_passes_through_with_direction(self):
        assert search_mod.normalize_sort("size-descending") == "size-descending"
        assert search_mod.normalize_sort("name-ascending") == "name-ascending"

    def test_bare_name_is_accepted(self):
        assert search_mod.normalize_sort("date-modified") == "date-modified"

    def test_none_or_blank_means_no_sort(self):
        assert search_mod.normalize_sort(None) is None
        assert search_mod.normalize_sort("  ") is None

    def test_unknown_name_is_rejected(self):
        # 真机实测：-sort 给错名字时 es.exe 退出码 4 并打印整页 help（不是静默忽略）
        with pytest.raises(InvalidParams):
            search_mod.normalize_sort("bogus-name")

    def test_unknown_direction_is_rejected(self):
        with pytest.raises(InvalidParams):
            search_mod.normalize_sort("size-sideways")


class TestBuildSearchCommand:
    def test_argv_is_a_list_starting_with_es(self):
        argv = search_mod.build_search_command("D:/tools/es.exe", query="winuse", limit=50)
        assert isinstance(argv, list)
        assert argv[0] == "D:/tools/es.exe"

    def test_shell_metacharacters_stay_inert_argv_items(self):
        # 参数列表形式下 `&`、`|`、`;` 只是普通搜索词，不会被任何 shell 解释
        argv = search_mod.build_search_command("es.exe", query="winuse & del *", limit=5)
        assert all(isinstance(part, str) for part in argv)
        for token in ("winuse", "&", "del", "*"):
            assert token in argv
        assert not any("cmd.exe" in part or "sh -c" in part or "&&" in part for part in argv)

    def test_limit_and_csv_columns(self):
        argv = search_mod.build_search_command("es.exe", query="x", limit=51)
        assert ["-n", "51"] == argv[1:3]
        for flag in ("-csv", "-no-header", "-size", "-size-format", "1", "-date-modified", "-date-format", "1"):
            assert flag in argv, f"缺少 {flag}（缺列就无法解析 size/mtime）"

    def test_query_terms_are_separate_argv_items(self):
        argv = search_mod.build_search_command("es.exe", query="*.md size:>10kb", limit=5)
        assert "*.md" in argv
        assert "size:>10kb" in argv

    def test_regex_switch_is_immediately_followed_by_pattern(self):
        # 真机实测：`-r` 后面必须紧跟正则；`-r <过滤词> <正则>` 形式返回 0 条
        argv = search_mod.build_search_command("es.exe", regex=r"^WinUse.*\.md$", limit=5)
        index = argv.index("-r")
        assert argv[index + 1] == r"^WinUse.*\.md$"

    def test_regex_and_query_can_coexist_as_extra_terms(self):
        # 真机实测：`-r REGEX size:>10kb` 有效（过滤词作为附加 AND 条件）
        argv = search_mod.build_search_command("es.exe", regex="^a$", query="ext:md", limit=5)
        assert argv[argv.index("-r") + 1] == "^a$"
        assert "ext:md" in argv

    def test_path_scope(self):
        argv = search_mod.build_search_command("es.exe", query="x", path="D:\\Data", limit=5)
        assert argv[argv.index("-path") + 1] == "D:\\Data"

    def test_sort_flag(self):
        argv = search_mod.build_search_command("es.exe", query="x", sort="size-descending", limit=5)
        assert argv[argv.index("-sort") + 1] == "size-descending"

    def test_size_filters_become_search_terms(self):
        argv = search_mod.build_search_command("es.exe", query="*.md", min_size=1000, max_size=20000, limit=5)
        assert "size:1000..20000" in argv

    def test_no_query_regex_or_path_is_rejected(self):
        with pytest.raises(MissingParam):
            search_mod.build_search_command("es.exe", limit=5)

    def test_path_alone_is_a_legal_request(self):
        # 真机实测：只给 -path 时 es 列出该目录下的条目（合法的"清单"用法）
        argv = search_mod.build_search_command("es.exe", path="D:\\Data", limit=5)
        assert "-path" in argv

    def test_invalid_limit_is_rejected(self):
        for bad in (0, -3, "abc", None):
            with pytest.raises(Exception):
                search_mod.build_search_command("es.exe", query="x", limit=bad)


class TestCommandPreview:
    def test_quotes_tokens_with_spaces(self):
        preview = search_mod.command_preview(["es.exe", "-path", "D:\\My Data", "winuse"])
        assert '"D:\\My Data"' in preview
        assert preview.startswith("es.exe ")

    def test_plain_tokens_are_space_joined(self):
        assert search_mod.command_preview(["es.exe", "-n", "5"]) == "es.exe -n 5"

    def test_quotes_cmd_metacharacters_so_preview_is_pasteable(self):
        # `size:>10kb` 不加引号粘进 cmd 会被当成重定向；正则里的 `^` 是 cmd 的转义符
        preview = search_mod.command_preview(["es.exe", "size:>10kb", "-r", "^WinUse.*\\.md$"])
        assert '"size:>10kb"' in preview
        assert '"^WinUse.*\\.md$"' in preview


# ===========================================================================
# 二、纯函数：es.exe 输出 → 结果条目
# ===========================================================================


class TestParseSearchOutput:
    def test_real_csv_rows_with_size_and_mtime(self):
        rows = search_mod.parse_search_output(REAL_CSV_OUTPUT)
        assert len(rows) == 3
        assert rows[0]["path"] == "D:\\Projects\\WinUse\\docs\\requirements\\notes.md"
        assert rows[0]["name"] == "notes.md"
        assert rows[0]["size"] == 55494
        assert rows[0]["mtime"] == "2026-09-27T15:58:51"

    def test_quoted_path_with_commas_is_one_field(self):
        rows = search_mod.parse_search_output('12,2026-01-02T03:04:05,"D:\\a,b\\c,d.txt"\r\n')
        assert rows[0]["path"] == "D:\\a,b\\c,d.txt"

    def test_unicode_path_is_kept(self):
        rows = search_mod.parse_search_output(REAL_CSV_OUTPUT)
        assert rows[1]["name"] == "WinUse-\u5ba1\u67e5\u62a5\u544a-20260927.md"

    def test_directory_row_has_no_size(self):
        rows = search_mod.parse_search_output(REAL_CSV_OUTPUT)
        assert rows[2]["size"] is None
        assert rows[2]["name"] == "winuse"

    def test_header_row_is_tolerated(self):
        # 万一 -no-header 没生效（换 es 版本），也不能把表头当结果
        rows = search_mod.parse_search_output(REAL_CSV_WITH_HEADER_OUTPUT)
        assert len(rows) == 3
        assert all(row["path"].endswith((".md", "winuse")) for row in rows)

    def test_plain_path_lines_are_supported(self):
        rows = search_mod.parse_search_output(REAL_PATH_LINES_OUTPUT)
        assert [row["path"] for row in rows] == [
            "D:\\Projects\\WinUse\\docs\\design\\README.md",
            "D:\\Projects\\WinUse\\docs\\requirements\\notes.md",
        ]
        assert rows[0]["size"] is None and rows[0]["mtime"] is None

    def test_unquoted_comma_path_is_rejoined(self):
        # es 总会给路径加引号，但万一将来不加：至少别把路径截断
        rows = search_mod.parse_search_output("12,2026-01-02T03:04:05,D:\\a,b\\c.txt\r\n")
        assert rows[0]["path"] == "D:\\a,b\\c.txt"

    def test_blank_output_is_empty_list(self):
        assert search_mod.parse_search_output("") == []
        assert search_mod.parse_search_output("\r\n\r\n") == []

    def test_blank_lines_are_skipped(self):
        rows = search_mod.parse_search_output("\r\n" + REAL_PATH_LINES_OUTPUT + "\r\n")
        assert len(rows) == 2

    def test_bad_size_is_none_not_zero(self):
        # 不做猜测：解析不了的 size 给 None（0 会被误读成"空文件"）
        rows = search_mod.parse_search_output('not-a-number,2026-01-02T03:04:05,"D:\\a.txt"\r\n')
        assert rows[0]["size"] is None


class TestDecodeOutput:
    def test_ascii_passes_through(self):
        assert search_mod.decode_output(b"D:\\a\\b.txt") == "D:\\a\\b.txt"

    def test_utf8_bytes_are_decoded(self):
        # 若将来 es 改吐 UTF-8，也不能变成乱码
        assert search_mod.decode_output(REAL_CHINESE_PATH.encode("utf-8")) == REAL_CHINESE_PATH

    def test_ansi_codepage_bytes_are_decoded(self):
        # 真机实测：es.exe 的 stdout 是系统 ANSI 代码页（本机 cp936），不是 UTF-8
        try:
            raw = REAL_CHINESE_PATH.encode(search_mod.ansi_encoding())
        except (UnicodeEncodeError, LookupError):  # pragma: no cover - 非中文代码页的机器
            pytest.skip("本机 ANSI 代码页编不出中文样例")
        assert search_mod.decode_output(raw) == REAL_CHINESE_PATH

    def test_empty_stdout_is_empty_string(self):
        assert search_mod.decode_output(b"") == ""
        assert search_mod.decode_output(None) == ""

    def test_never_raises_on_garbage(self):
        assert isinstance(search_mod.decode_output(b"\xd6\xd0\xff\xfe\x00"), str)


# ===========================================================================
# 三、es.exe 定位（显式参数 → 配置 → PATH）
# ===========================================================================


def which_returns(path: str):
    """假 `shutil.which`：只有查 `es.exe` 时返回该路径（不碰真实 PATH）。"""
    return lambda name: path if name == search_mod.ES_EXE_NAME else None


def exists_only(path: str):
    """假 exists：只有 path 存在（不碰真实文件系统）。"""
    return lambda candidate: candidate == path


class TestResolveEs:
    def test_explicit_param_wins(self):
        found, candidates = search_mod.resolve_es(
            es_path="D:/explicit/es.exe",
            configured="D:/cfg/es.exe",
            which=which_returns("D:/path/es.exe"),
            exists=exists_only("D:/explicit/es.exe"),
        )
        assert found == "D:/explicit/es.exe"
        assert candidates[0]["source"] == "param"
        assert candidates[0]["exists"] is True

    def test_explicit_param_that_does_not_exist_does_not_fall_back(self):
        # 显式给了路径却不存在 → 报错，不偷偷换用别的 es.exe（否则结果不可归因）
        found, candidates = search_mod.resolve_es(
            es_path="D:/missing/es.exe",
            configured="D:/cfg/es.exe",
            which=which_returns("D:/cfg/es.exe"),
            exists=exists_only("D:/cfg/es.exe"),
        )
        assert found is None
        assert [item["source"] for item in candidates] == ["param"]

    def test_configured_value_is_second(self):
        found, candidates = search_mod.resolve_es(
            configured="D:/cfg/es.exe", which=lambda _: None, exists=exists_only("D:/cfg/es.exe")
        )
        assert found == "D:/cfg/es.exe"
        assert [item["source"] for item in candidates] == ["config"]

    def test_default_install_path_is_third(self):
        found, candidates = search_mod.resolve_es(
            configured=None,
            default_path="D:/default/es.exe",
            which=lambda _: None,
            exists=exists_only("D:/default/es.exe"),
        )
        assert found == "D:/default/es.exe"
        assert [item["source"] for item in candidates][:2] == ["config", "default"]

    def test_path_lookup_is_last(self):
        found, candidates = search_mod.resolve_es(
            configured="D:/cfg/missing.exe",
            default_path="D:/default/missing.exe",
            which=which_returns("D:/path/es.exe"),
            exists=exists_only("D:/path/es.exe"),
        )
        assert found == "D:/path/es.exe"
        assert candidates[-1]["source"] == "path"

    def test_nothing_found_lists_every_candidate(self):
        found, candidates = search_mod.resolve_es(
            configured="D:/cfg/missing.exe",
            default_path="D:/default/missing.exe",
            which=lambda _: None,
            exists=lambda _: False,
        )
        assert found is None
        assert [item["source"] for item in candidates] == ["config", "default", "path"]
        assert all(item["exists"] is False for item in candidates)

    def test_blank_config_value_falls_through_to_default(self):
        found, _candidates = search_mod.resolve_es(
            configured="   ", default_path="D:/default/es.exe", which=lambda _: None, exists=exists_only("D:/default/es.exe")
        )
        assert found == "D:/default/es.exe"

    def test_real_default_points_at_program_files(self):
        assert search_mod.DEFAULT_ES_PATH.lower().endswith("everything\\es.exe") or search_mod.DEFAULT_ES_PATH.lower().endswith(
            "everything/es.exe"
        )


class TestConfiguredEsPath:
    def test_reads_from_dotenv(self, tmp_path):
        # 反斜杠按字面处理（项目纪律：.env 里不做转义），所以这里写单反斜杠
        env = tmp_path / ".env"
        env.write_text('WINUSE_ES_PATH="D:\\tools\\Everything\\es.exe"\n', encoding="utf-8")
        value, error = search_mod.configured_es_path(env_path=env)
        assert value == "D:\\tools\\Everything\\es.exe"
        assert error is None

    def test_blank_value_is_none(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("WINUSE_ES_PATH=\n", encoding="utf-8")
        value, error = search_mod.configured_es_path(env_path=env)
        assert value is None and error is None

    def test_broken_env_is_reported_not_raised(self, tmp_path):
        # .env 坏了不该让搜索整体崩：定位失败原因随错误详情带出，其余候选照走
        env = tmp_path / ".env"
        env.write_text("WINUSE_STATE_MAX_ELEMENTS=abc\n", encoding="utf-8")
        value, error = search_mod.configured_es_path(env_path=env)
        assert value is None
        assert isinstance(error, str) and error


# ===========================================================================
# 四、run_search：真跑分支（假造 subprocess）
# ===========================================================================


@pytest.fixture(autouse=True)
def _no_real_es(monkeypatch):
    """兜底铁律：任何用例都不许在没有显式假造 subprocess 的情况下真起 es.exe。"""

    def boom(*args, **kwargs):  # pragma: no cover - 正常用例不会走到
        raise AssertionError("单测禁止真起 es.exe（请 monkeypatch subprocess.run）")

    monkeypatch.setattr(subprocess, "run", boom)


class TestRunSearchHappyPath:
    def test_success_shape(self, monkeypatch, tmp_path):
        frozen_es(monkeypatch)
        recorder = patch_run(monkeypatch, Recorder(stdout=REAL_CSV_OUTPUT.encode("utf-8")))
        got = search_mod.run_search(query="*.md", path=str(tmp_path))
        assert got["ok"] is True
        assert got["count"] == 3
        assert [row["name"] for row in got["results"]][0] == "notes.md"
        assert got["truncated"] is False
        assert "-n" in got["command"]
        assert recorder.calls

    def test_asks_for_one_extra_row_to_detect_truncation(self, monkeypatch, tmp_path):
        frozen_es(monkeypatch)
        recorder = patch_run(monkeypatch, Recorder(stdout=REAL_PATH_LINES_OUTPUT.encode("utf-8")))
        search_mod.run_search(query="*.md", max_results=50)
        assert recorder.argv[recorder.argv.index("-n") + 1] == "51"

    def test_truncates_and_flags(self, monkeypatch, tmp_path):
        frozen_es(monkeypatch)
        rows = b"".join(
            f'{i},2026-01-0{i + 1}T00:00:00,"D:\\\\p\\\\f{i}.txt"\r\n'.encode("utf-8") for i in range(6)
        )
        recorder = patch_run(monkeypatch, Recorder(stdout=rows))
        got = search_mod.run_search(query="f", max_results=5)
        assert got["count"] == 5
        assert got["truncated"] is True
        assert len(got["results"]) == 5
        assert recorder.argv[recorder.argv.index("-n") + 1] == "6"

    def test_exactly_max_results_is_not_truncated(self, monkeypatch):
        frozen_es(monkeypatch)
        rows = b"".join(
            f'{i},2026-01-0{i + 1}T00:00:00,"D:\\\\p\\\\f{i}.txt"\r\n'.encode("utf-8") for i in range(5)
        )
        patch_run(monkeypatch, Recorder(stdout=rows))
        got = search_mod.run_search(query="f", max_results=5)
        assert got["count"] == 5
        assert got["truncated"] is False

    def test_zero_results_is_ok_not_an_error(self, monkeypatch):
        # 与"找不到 es.exe"必须严格区分：0 条 = 搜索真的跑了
        frozen_es(monkeypatch)
        patch_run(monkeypatch, Recorder(stdout=b""))
        got = search_mod.run_search(query="no-such-thing-8f2a")
        assert got["ok"] is True
        assert got["count"] == 0
        assert got["results"] == []
        assert "error" not in got

    def test_max_results_above_cap_is_clamped_and_reported(self, monkeypatch):
        frozen_es(monkeypatch)
        recorder = patch_run(monkeypatch, Recorder(stdout=b""))
        got = search_mod.run_search(query="x", max_results=10**6)
        assert got["clamped"]["max_results"] == search_mod.MAX_SEARCH_RESULTS
        assert recorder.argv[recorder.argv.index("-n") + 1] == str(search_mod.MAX_SEARCH_RESULTS + 1)

    def test_ansi_stdout_is_decoded(self, monkeypatch):
        # 真跑时 es 给的是 ANSI 字节：中文路径不能变成乱码
        frozen_es(monkeypatch)
        try:
            payload = f'7,2026-01-02T03:04:05,"{REAL_CHINESE_PATH}"\r\n'.encode(search_mod.ansi_encoding())
        except (UnicodeEncodeError, LookupError):  # pragma: no cover
            pytest.skip("本机 ANSI 代码页编不出中文样例")
        patch_run(monkeypatch, Recorder(stdout=payload))
        got = search_mod.run_search(query="txt")
        assert got["results"][0]["path"] == REAL_CHINESE_PATH

    def test_timeout_reaches_subprocess(self, monkeypatch):
        frozen_es(monkeypatch)
        recorder = patch_run(monkeypatch, Recorder(stdout=b""))
        search_mod.run_search(query="x", timeout_seconds=7)
        assert recorder.calls[0]["kwargs"]["timeout"] == 7

    def test_no_shell_and_no_stdin_leak(self, monkeypatch):
        frozen_es(monkeypatch)
        recorder = patch_run(monkeypatch, Recorder(stdout=b""))
        search_mod.run_search(query="x")
        kwargs = recorder.calls[0]["kwargs"]
        assert not kwargs.get("shell", False)
        assert kwargs.get("capture_output") is True

    def test_windows_hides_console_window(self, monkeypatch):
        # 每搜一次闪一个黑窗是不能接受的（系统自带 es.exe 是控制台程序）
        frozen_es(monkeypatch)
        recorder = patch_run(monkeypatch, Recorder(stdout=b""))
        search_mod.run_search(query="x")
        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert recorder.calls[0]["kwargs"].get("creationflags", 0) == expected

    def test_read_only_flags_in_result(self, monkeypatch):
        # 只读语义要写在回执里：上层据此跳过确认门，不给用户添无意义的确认
        frozen_es(monkeypatch)
        patch_run(monkeypatch, Recorder(stdout=b""))
        got = search_mod.run_search(query="x")
        assert got["read_only"] is True
        assert got["requires_confirm"] is False

    def test_search_never_writes_audit(self, monkeypatch):
        """只读工具不落审计（项目约定：审计只记动作类 act.* / window.* / batch.* / notify.executed）。
        搜索写审计既没有意义，还会被误读成"发生过危险动作"。"""
        frozen_es(monkeypatch)
        patch_run(monkeypatch, Recorder(stdout=REAL_CSV_OUTPUT.encode("utf-8")))

        from winuse import audit as audit_mod

        def boom(*args, **kwargs):  # pragma: no cover
            raise AssertionError("搜索是只读操作，不得写审计")

        monkeypatch.setattr(audit_mod, "record", boom)
        monkeypatch.setattr(audit_mod, "sink_event", boom)
        got = search_mod.run_search(query="*.md")
        assert got["ok"] is True

    def test_es_path_reported_in_result(self, monkeypatch):
        frozen_es(monkeypatch, "D:/tools/es.exe")
        patch_run(monkeypatch, Recorder(stdout=b""))
        got = search_mod.run_search(query="x")
        assert got["es_path"] == "D:/tools/es.exe"
        assert got["command"].startswith("D:/tools/es.exe")


class TestRunSearchFailures:
    def test_missing_tool_returns_explicit_code_not_zero_results(self, monkeypatch):
        monkeypatch.setattr(
            search_mod,
            "resolve_es",
            lambda **kwargs: (
                None,
                [
                    {"source": "config", "path": "D:/cfg/es.exe", "exists": False},
                    {"source": "default", "path": "D:/def/es.exe", "exists": False},
                    {"source": "path", "path": None, "exists": False},
                ],
            ),
        )
        recorder = patch_run(monkeypatch, Recorder(stdout=b""))
        got = search_mod.run_search(query="*.md")
        assert got["ok"] is False
        assert got["error"]["code"] == search_mod.EsNotFound.code
        assert got["count"] == 0 and got["results"] == []
        assert got["truncated"] is False
        assert recorder.calls == []  # 没工具就别假装搜过
        assert "D:/def/es.exe" in got["error"]["detail"]  # 候选路径要能排障

    def test_explicit_missing_es_path_returns_not_found(self, monkeypatch):
        got = search_mod.run_search(query="x", es_path="D:/no-such-dir/es.exe")
        assert got["ok"] is False
        assert got["error"]["code"] == search_mod.EsNotFound.code

    def test_timeout_is_a_clear_code(self, monkeypatch):
        frozen_es(monkeypatch)
        patch_run(monkeypatch, Recorder(exc=subprocess.TimeoutExpired(cmd="es.exe", timeout=15)))
        got = search_mod.run_search(query="x", timeout_seconds=15)
        assert got["ok"] is False
        assert got["error"]["code"] == search_mod.SearchTimeout.code
        assert "15" in got["error"]["message"]

    def test_everything_not_running_maps_to_run_failed(self, monkeypatch):
        # 真机实测：Everything 没在跑 → 退出码 8 + stderr "Error 8: Everything IPC window not found..."
        frozen_es(monkeypatch)
        patch_run(monkeypatch, Recorder(stdout=b"", stderr=REAL_IPC_ERROR_STDERR, returncode=REAL_IPC_ERROR_RC))
        got = search_mod.run_search(query="x")
        assert got["ok"] is False
        assert got["error"]["code"] == search_mod.EsRunFailed.code
        assert "IPC" in got["error"]["detail"] or "Error 8" in got["error"]["detail"]

    def test_nonzero_exit_with_help_on_stdout_is_not_a_result(self, monkeypatch):
        # 真机实测：-sort 名字非法时 es 把整页 help 打到 stdout 并退出码 4
        frozen_es(monkeypatch)
        patch_run(monkeypatch, Recorder(stdout=b"ES 1.1.0.30\r\nUsage: es.exe ...", stderr=b"Error 4", returncode=4))
        got = search_mod.run_search(query="x")
        assert got["ok"] is False
        assert got["error"]["code"] == search_mod.EsRunFailed.code
        assert got["results"] == []

    def test_launch_failure_is_normalized(self, monkeypatch):
        frozen_es(monkeypatch)
        patch_run(monkeypatch, Recorder(exc=FileNotFoundError("es.exe 不见了")))
        got = search_mod.run_search(query="x")
        assert got["ok"] is False
        assert got["error"]["code"] == search_mod.EsRunFailed.code

    def test_no_condition_is_a_caller_error(self):
        # 编程错误直接抛：无 query/regex/path 的"搜索"等于无界全盘转储
        with pytest.raises(MissingParam):
            search_mod.run_search()

    def test_missing_search_path_is_rejected(self):
        missing = "D:/no-such-dir-8f2a/reports"
        with pytest.raises(search_mod.SearchPathNotFound):
            search_mod.run_search(query="*.md", path=missing)

    def test_file_as_path_is_rejected(self, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(search_mod.SearchPathNotFound):
            search_mod.run_search(query="*.md", path=str(target))

    def test_invalid_max_results_is_rejected(self):
        for bad in (0, -1, "abc"):
            with pytest.raises(InvalidParams):
                search_mod.run_search(query="x", max_results=bad)

    def test_failure_payload_has_the_same_shape(self, monkeypatch):
        monkeypatch.setattr(
            search_mod, "resolve_es", lambda **kwargs: (None, [{"source": "path", "path": None, "exists": False}])
        )
        got = search_mod.run_search(query="x")
        for key in ("ok", "count", "results", "truncated", "error"):
            assert key in got
        assert got["error"]["code"] and got["error"]["message"]

    def test_all_error_codes_are_winuse_errors(self):
        for exc in (
            search_mod.EsNotFound,
            search_mod.EsRunFailed,
            search_mod.SearchTimeout,
            search_mod.EsOutputInvalid,
            search_mod.SearchPathNotFound,
        ):
            assert issubclass(exc, WinUseError)
            assert exc.code and exc.code.isupper()


# ===========================================================================
# 五、只读与边界
# ===========================================================================


class TestReadOnlyGuarantees:
    def test_no_audit_import_needed_by_search(self):
        """搜索模块不许依赖 audit 的写入路径（避免哪天顺手写一条"危险动作"）。"""
        source = Path(search_mod.__file__).read_text(encoding="utf-8")
        assert "sink_event" not in source
        assert "audit.record" not in source

    def test_no_temporary_files_are_used(self, monkeypatch):
        """用 stdout 的 -csv 而非 -export-csv，就不需要在磁盘上落中转文件。"""
        source = Path(search_mod.__file__).read_text(encoding="utf-8")
        assert "-export-csv" not in source
        assert "tempfile" not in source
