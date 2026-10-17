"""winuse_clipboard（剪贴板出口）单测。

剪贴板是**双向隐私面**，用例按方向分两组：

1. **读**：默认只列类型（下发给驱动的 `include_text` 必须是 false）；要纯文本属隐私读取，
   未确认时结构化拦截且**一个字节都不下发**；确认后回文本 + 类型 + 长度。
2. **写**：三选一（多给 / 都不给当场拒）；写会**整段覆盖**用户剪贴板且不可撤销 → 未确认拦下；
   `confirm=True` 且 `execute=False` 只回 `would_call`（不碰驱动）；真下发只走桩驱动。

两条贯穿纪律：

- **审计不含内容**：读到的文本、要写的文本都不出现在审计条目里，只记方向 / 类型 / 长度。
- **单测不碰真实桌面**：所有驱动调用走桩；`tests/fake_driver.py` 用来证伪
  「干跑也会调驱动」与「参数错误也会碰驱动」（与 `test_menu.py` / `test_notify.py` 同口径）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core import act as act_mod  # noqa: E402
from winuse.core import guard as guard_mod  # noqa: E402
from winuse.core.clipboard import (  # noqa: E402
    ACTION_READ,
    ACTION_WRITE,
    MAX_READ_TEXT_CHARS,
    RULE_READ_TEXT,
    RULE_WRITE,
    SUPPORTED_OPTIONS,
    TOOL_READ,
    TOOL_WRITE,
    ClipboardFileNotFound,
    ClipboardInvalidAction,
    ClipboardMissingValue,
    ClipboardMultipleValues,
    ClipboardNotAbsolutePath,
    ClipboardOptionUnknown,
    ClipboardOutputInvalid,
    parse_options,
    parse_read_result,
    pick_write_value,
    run_clipboard,
    validate_local_file,
)
from winuse.driver.cua import CuaDriver  # noqa: E402
from winuse.driver.errors import InvalidParams, WinUseError  # noqa: E402

#: `tests/fake_driver.py` 当真实驱动用（与 `test_menu.py` / `test_cua_driver.py` 同口径）
FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_driver.py")]

#: 剪贴板类型样本（驱动 0.28.2 真机实测形态：CF_* 加 Chromium 自建的两个 MIME 型别）
TYPES = ["CF_LOCALE", "CF_OEMTEXT", "CF_TEXT", "CF_UNICODETEXT", "Chromium Web Custom MIME Data Format"]

#: 样例文本：**刻意写成"别当凭据"**——测试样例里出现真凭据形状，本身就是泄露
SAMPLE_TEXT = "样例文本-别当凭据-8271"


def driver_read_result(text=None):
    """驱动 `clipboard_read` 的实测回执形状（0.28.2 真机跑出来的字段）。"""
    return {
        "content_redacted_from_telemetry": True,
        "privacy_sensitive": True,
        "supported": True,
        "text": text,
        "types": list(TYPES),
    }


class StubDriver:
    """桩驱动：记录每一次调用，可注入返回值或异常。"""

    def __init__(self, result=None, boom=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = driver_read_result() if result is None else result
        self.boom = boom

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if self.boom:
            raise self.boom
        return self.result


class NeverCalledDriver:
    """干跑/被拦用例专用：任何一次驱动调用都会让用例失败（证伪「其实偷偷碰了驱动」）。"""

    def call(self, tool, args=None, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError(f"这条路径不该下发驱动调用：{tool} {args}")


class Sink:
    def __init__(self):
        self.entries: list[dict] = []

    def __call__(self, entry):
        self.entries.append(entry)


def make_file(tmp_path, name="doc.txt"):
    """在临时目录里造一个真实文件（绝对路径由 tmp_path 给，源码里不出现本机路径字面量）。"""
    path = tmp_path / name
    path.write_text("placeholder", encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# 纯逻辑：动作名 / options / 三选一
# --------------------------------------------------------------------------


class TestActionAndOptions:
    def test_unknown_action_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            run_clipboard(StubDriver(), action="copy")
        assert ctx.value.code == ClipboardInvalidAction.code == "CLIPBOARD_INVALID_ACTION"

    def test_missing_action_is_refused(self):
        for bad in (None, "", "   "):
            with pytest.raises(WinUseError) as ctx:
                run_clipboard(StubDriver(), action=bad)
            assert ctx.value.code == "CLIPBOARD_INVALID_ACTION"

    def test_action_is_normalized(self):
        out = run_clipboard(StubDriver(), action="  READ ")
        assert out["ok"] is True and out["action"] == ACTION_READ

    def test_read_refuses_write_fields(self):
        with pytest.raises(InvalidParams):
            run_clipboard(StubDriver(), action="read", text="x")
        with pytest.raises(InvalidParams):
            run_clipboard(StubDriver(), action="read", file_path="D:/x.txt")

    def test_write_refuses_include_text(self):
        """写路径上给 include_text 是矛盾参数：**不静默忽略**（否则调用方以为会拿到文本）。"""
        with pytest.raises(InvalidParams):
            run_clipboard(StubDriver(), action="write", text=SAMPLE_TEXT, include_text=True)

    def test_boolean_flags_are_strict(self):
        """字符串 "false" 若被 bool() 吞掉，就会变成一次真正的剪贴板文本读取（隐私面）。"""
        with pytest.raises(InvalidParams):
            run_clipboard(StubDriver(), action="read", include_text="false")
        with pytest.raises(InvalidParams):
            run_clipboard(StubDriver(), action="write", text=SAMPLE_TEXT, confirm="yes")

    def test_parse_options_defaults_to_empty(self):
        assert parse_options(None) == {}
        assert parse_options({}) == {}

    def test_unknown_option_key_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            parse_options({"window": 1})
        assert ctx.value.code == ClipboardOptionUnknown.code == "CLIPBOARD_OPTION_UNKNOWN"

    def test_options_must_be_a_mapping(self):
        with pytest.raises(WinUseError) as ctx:
            parse_options(["max_text_chars"])
        assert ctx.value.code == "CLIPBOARD_OPTION_INVALID"

    def test_max_text_chars_is_clamped_to_the_builtin_cap(self):
        assert parse_options({"max_text_chars": 5})["max_text_chars"] == 5
        parsed = parse_options({"max_text_chars": MAX_READ_TEXT_CHARS * 10})
        assert parsed["max_text_chars"] == MAX_READ_TEXT_CHARS
        assert parsed["max_text_chars_clamped"] is True
        assert "max_text_chars" in SUPPORTED_OPTIONS

    def test_max_text_chars_must_be_a_positive_int(self):
        for bad in (0, -3, "8000", True, 1.5):
            with pytest.raises(WinUseError) as ctx:
                parse_options({"max_text_chars": bad})
            assert ctx.value.code == "CLIPBOARD_OPTION_INVALID", bad


class TestPickWriteValue:
    def test_text_only(self):
        assert pick_write_value(text=SAMPLE_TEXT) == ("text", SAMPLE_TEXT)

    def test_image_only(self):
        assert pick_write_value(image_path="D:/pic.png") == ("image", "D:/pic.png")

    def test_file_only(self):
        assert pick_write_value(file_path="D:/doc.txt") == ("file", "D:/doc.txt")

    def test_text_whitespace_is_content_and_is_not_stripped(self):
        """文本里首尾空白是**内容**，不许规范化（与路径相反）。"""
        assert pick_write_value(text="  a b  ") == ("text", "  a b  ")

    def test_paths_are_trimmed(self):
        assert pick_write_value(image_path="  D:/pic.png  ") == ("image", "D:/pic.png")

    def test_none_at_all_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            pick_write_value()
        assert ctx.value.code == ClipboardMissingValue.code == "CLIPBOARD_MISSING_VALUE"

    def test_blank_text_counts_as_not_given(self):
        """空文本等于"清空剪贴板"，本门面不替你猜意图（同 registry「不猜空值」口径）。"""
        with pytest.raises(WinUseError) as ctx:
            pick_write_value(text="   ")
        assert ctx.value.code == "CLIPBOARD_MISSING_VALUE"

    def test_two_values_are_refused_with_both_names(self):
        with pytest.raises(WinUseError) as ctx:
            pick_write_value(text=SAMPLE_TEXT, file_path="D:/doc.txt")
        assert ctx.value.code == ClipboardMultipleValues.code == "CLIPBOARD_MULTIPLE_VALUES"
        assert "text" in ctx.value.message and "file_path" in ctx.value.message

    def test_three_values_are_refused(self):
        with pytest.raises(WinUseError) as ctx:
            pick_write_value(text=SAMPLE_TEXT, image_path="D:/a.png", file_path="D:/b.txt")
        assert ctx.value.code == "CLIPBOARD_MULTIPLE_VALUES"
        assert "3" in ctx.value.message

    def test_non_string_value_is_invalid_params(self):
        for kwargs in ({"text": 123}, {"image_path": 5}, {"file_path": {"p": 1}}):
            with pytest.raises(InvalidParams):
                pick_write_value(**kwargs)


class TestValidateLocalFile:
    def test_relative_path_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            validate_local_file("image", "pic.png")
        assert ctx.value.code == ClipboardNotAbsolutePath.code == "CLIPBOARD_NOT_ABSOLUTE_PATH"

    def test_drive_relative_path_is_refused(self, tmp_path):
        """`C:name.png` 是"当前盘下的相对路径"，不是绝对路径——驱动会按未知基准解析。"""
        with pytest.raises(WinUseError) as ctx:
            validate_local_file("image", "C:name.png")
        assert ctx.value.code == "CLIPBOARD_NOT_ABSOLUTE_PATH"

    def test_missing_file_is_refused(self, tmp_path):
        with pytest.raises(WinUseError) as ctx:
            validate_local_file("file", str(tmp_path / "nope.txt"))
        assert ctx.value.code == ClipboardFileNotFound.code == "CLIPBOARD_FILE_NOT_FOUND"

    def test_directory_is_refused(self, tmp_path):
        with pytest.raises(WinUseError) as ctx:
            validate_local_file("file", str(tmp_path))
        assert ctx.value.code == "CLIPBOARD_FILE_NOT_FOUND"
        assert "目录" in ctx.value.message

    def test_existing_file_passes(self, tmp_path):
        path = make_file(tmp_path, "pic.png")
        assert validate_local_file("image", path) == path


# --------------------------------------------------------------------------
# 纯逻辑：驱动回执解析
# --------------------------------------------------------------------------


class TestParseReadResult:
    def test_types_only_never_reports_text(self):
        parsed = parse_read_result(driver_read_result(), include_text=False)
        assert parsed["types"] == TYPES
        assert parsed["text"] is None
        assert parsed["text_length"] is None
        assert parsed["supported"] is True

    def test_no_plain_text_on_clipboard_is_not_an_error(self):
        parsed = parse_read_result(driver_read_result(text=None), include_text=True)
        assert parsed["text"] is None
        assert parsed["text_length"] is None
        assert parsed["truncated"] is False

    def test_empty_string_text_is_a_real_value(self):
        """空串与 null 不是一回事：null=剪贴板上没有纯文本，""=真的是一段空文本。"""
        parsed = parse_read_result(driver_read_result(text=""), include_text=True)
        assert parsed["text"] == ""
        assert parsed["text_length"] == 0

    def test_text_is_truncated_with_the_full_length_kept(self):
        parsed = parse_read_result(driver_read_result(text="x" * 12), include_text=True, max_chars=5)
        assert parsed["text"] == "xxxxx"
        assert parsed["text_length"] == 12
        assert parsed["truncated"] is True

    def test_non_string_text_is_refused(self):
        """驱动换了形态（回了数字/对象）时不许把它当文本——宁可报错也不猜。"""
        with pytest.raises(WinUseError) as ctx:
            parse_read_result(driver_read_result(text=123), include_text=True)
        assert ctx.value.code == ClipboardOutputInvalid.code == "CLIPBOARD_OUTPUT_INVALID"

    def test_junk_types_are_filtered_not_invented(self):
        parsed = parse_read_result({"types": ["CF_TEXT", 7, None, {"nested": 1}, ""]}, include_text=False)
        assert parsed["types"] == ["CF_TEXT"]

    def test_missing_supported_key_defaults_to_supported(self):
        """认不出的回执形态照旧放行（只回业务字段、不带 supported 的驱动不该被判死）。"""
        assert parse_read_result({"types": ["CF_TEXT"]})["supported"] is True

    def test_supported_false_is_reported_as_false(self):
        assert parse_read_result({"supported": False, "types": []})["supported"] is False


# --------------------------------------------------------------------------
# 读：只列类型（不取内容）
# --------------------------------------------------------------------------


class TestReadTypesOnly:
    def test_lists_types_without_asking_for_text(self):
        driver = StubDriver()
        out = run_clipboard(driver, action="read")
        assert out["ok"] is True
        assert out["action"] == ACTION_READ
        assert out["tool"] == TOOL_READ
        assert out["types"] == TYPES
        assert out["types_count"] == len(TYPES)
        assert out["include_text"] is False
        assert out["read_only"] is True
        assert out["requires_confirm"] is False
        assert out["dry_run"] is False
        assert out["written_kind"] is None
        # 驱动的参数里必须明写 false：默认值靠不住，显式才是可断言的事实
        assert driver.calls == [(TOOL_READ, {"include_text": False})]

    def test_no_text_field_at_all_when_text_was_not_requested(self):
        """「没读过」就不给字段——避免把没读误读成"剪贴板是空的"。"""
        out = run_clipboard(StubDriver(), action="read")
        assert "text" not in out
        assert "text_length" not in out

    def test_types_only_read_is_not_gated_by_execute(self):
        """读不改变任何状态，所以不设干跑闸门（与 core.search 的只读口径一致）。"""
        out = run_clipboard(StubDriver(), action="read", execute=False)
        assert out["ok"] is True and out["types"] == TYPES

    def test_empty_clipboard_reports_empty_types(self):
        out = run_clipboard(StubDriver(result=driver_read_result()), action="read")
        assert out["types"] == TYPES  # 非空样本
        empty = run_clipboard(StubDriver(result={"supported": True, "types": []}), action="read")
        assert empty["ok"] is True and empty["types"] == []

    def test_unsupported_clipboard_is_a_failure_not_an_empty_list(self):
        """`supported=false` 是"读不到"，不许报成"剪贴板是空的"（同 search 的「缺工具 ≠ 无结果」）。"""
        out = run_clipboard(StubDriver(result={"supported": False, "types": []}), action="read")
        assert out["ok"] is False
        assert out["code"] == "CLIPBOARD_UNSUPPORTED"
        assert out["error"]["code"] == "CLIPBOARD_UNSUPPORTED"
        assert out["action"] == ACTION_READ


# --------------------------------------------------------------------------
# 读：纯文本的确认门
# --------------------------------------------------------------------------


class TestReadTextGate:
    def test_text_read_without_confirm_is_blocked_before_any_dispatch(self):
        out = run_clipboard(NeverCalledDriver(), action="read", include_text=True)
        assert out["ok"] is False
        assert out["requires_confirm"] is True
        assert out["code"] == guard_mod.CONFIRM_CODE == "CONFIRM_REQUIRED"
        assert out["danger"]["dangerous"] is True
        assert out["danger"]["rule"] == RULE_READ_TEXT
        assert out["would_call"] is None  # 被拦不给调用计划
        assert out["action"] == ACTION_READ
        assert out["include_text"] is True
        assert out["error"]["code"] == "CONFIRM_REQUIRED"
        assert "confirm=true" in out["error"]["detail"]

    def test_blocked_read_receipt_shape(self):
        out = run_clipboard(StubDriver(), action="read", include_text=True)
        assert set(out) == {
            "ok",
            "action",
            "dry_run",
            "tool",
            "written_kind",
            "include_text",
            "read_only",
            "would_call",
            "requires_confirm",
            "danger",
            "code",
            "error",
        }

    def test_confirmed_text_read_returns_text_types_and_length(self):
        driver = StubDriver(result=driver_read_result(text=SAMPLE_TEXT))
        out = run_clipboard(driver, action="read", include_text=True, confirm=True)
        assert out["ok"] is True
        assert out["text"] == SAMPLE_TEXT
        assert out["text_length"] == len(SAMPLE_TEXT)
        assert out["has_text"] is True
        assert out["types"] == TYPES
        assert out["privacy_sensitive"] is True
        assert out["dry_run"] is False
        assert out["danger"]["rule"] == RULE_READ_TEXT
        assert driver.calls == [(TOOL_READ, {"include_text": True})]

    def test_confirmed_read_drops_the_requires_confirm_flag(self):
        out = run_clipboard(StubDriver(result=driver_read_result(text="x")), action="read", include_text=True, confirm=True)
        assert "requires_confirm" not in out

    def test_confirmed_read_without_plain_text_reports_has_text_false(self):
        out = run_clipboard(StubDriver(result=driver_read_result()), action="read", include_text=True, confirm=True)
        assert out["ok"] is True
        assert out["text"] is None
        assert out["has_text"] is False
        assert out["text_length"] is None

    def test_default_truncation_is_bounded_and_traced(self):
        long_text = "x" * (MAX_READ_TEXT_CHARS + 10)
        out = run_clipboard(
            StubDriver(result=driver_read_result(text=long_text)), action="read", include_text=True, confirm=True
        )
        assert out["text_length"] == len(long_text)  # 长度按**全文**报，不因裁剪失真
        assert len(out["text"]) == MAX_READ_TEXT_CHARS
        assert out["truncated"] is True
        assert "truncate_hint" in out

    def test_options_max_text_chars_shrinks_the_payload(self):
        out = run_clipboard(
            StubDriver(result=driver_read_result(text="0123456789")),
            action="read",
            include_text=True,
            confirm=True,
            options={"max_text_chars": 5},
        )
        assert out["text"] == "01234"
        assert out["text_length"] == 10
        assert out["truncated"] is True
        assert out["max_text_chars"] == 5

    def test_non_string_text_from_driver_is_a_failure_receipt(self):
        out = run_clipboard(StubDriver(result=driver_read_result(text=123)), action="read", include_text=True, confirm=True)
        assert out["ok"] is False
        assert out["code"] == "CLIPBOARD_OUTPUT_INVALID"
        assert out["error"]["code"] == "CLIPBOARD_OUTPUT_INVALID"


# --------------------------------------------------------------------------
# 读：审计（只记方向 / 类型 / 长度，绝不记内容）
# --------------------------------------------------------------------------


class TestReadAudit:
    def test_audit_never_carries_the_text_body(self):
        sink = Sink()
        out = run_clipboard(
            StubDriver(result=driver_read_result(text=SAMPLE_TEXT)),
            action="read",
            include_text=True,
            confirm=True,
            audit=sink,
        )
        assert out["text"] == SAMPLE_TEXT  # 内容只回给调用方
        blob = json.dumps(sink.entries, ensure_ascii=False)
        assert SAMPLE_TEXT not in blob
        assert [e["event"] for e in sink.entries] == ["clipboard.read"]
        detail = sink.entries[-1]["detail"]
        assert detail["text_length"] == len(SAMPLE_TEXT)
        assert detail["types"] == TYPES
        assert detail["include_text"] is True

    def test_types_only_read_is_audited_without_text_length(self):
        sink = Sink()
        run_clipboard(StubDriver(), action="read", audit=sink)
        entry = sink.entries[-1]
        assert entry["event"] == "clipboard.read"
        assert entry["ok"] is True
        assert entry["detail"]["include_text"] is False
        assert "text_length" not in entry["detail"]
        assert entry["detail"]["types"] == TYPES

    def test_blocked_text_read_is_audited(self):
        sink = Sink()
        run_clipboard(StubDriver(), action="read", include_text=True, audit=sink)
        entry = sink.entries[-1]
        assert entry["event"] == "clipboard.blocked"
        assert entry["ok"] is False
        assert entry["code"] == "CONFIRM_REQUIRED"
        assert entry["detail"]["rule"] == RULE_READ_TEXT

    def test_read_failure_is_audited(self):
        sink = Sink()
        driver = StubDriver(boom=WinUseError("驱动炸了", "细节"))
        out = run_clipboard(driver, action="read", audit=sink)
        assert out["ok"] is False
        assert sink.entries[-1]["ok"] is False
        assert sink.entries[-1]["code"] == "WINUSE_ERROR"

    def test_audit_written_flag_is_exposed(self):
        def failing_sink(entry):
            return False

        out = run_clipboard(StubDriver(), action="read", audit=failing_sink)
        assert out["audit_written"] is False

    def test_no_sink_means_no_audit_field(self):
        out = run_clipboard(StubDriver(), action="read", audit=None)
        assert "audit_written" not in out


# --------------------------------------------------------------------------
# 写：门禁与干跑 / 真下发
# --------------------------------------------------------------------------


class TestWriteGate:
    def test_write_without_confirm_is_blocked(self):
        out = run_clipboard(NeverCalledDriver(), action="write", text=SAMPLE_TEXT)
        assert out["ok"] is False
        assert out["requires_confirm"] is True
        assert out["code"] == "CONFIRM_REQUIRED"
        assert out["danger"]["rule"] == RULE_WRITE
        assert out["danger"]["dangerous"] is True
        assert out["would_call"] is None
        assert out["written_kind"] == "text"
        assert out["dry_run"] is True

    def test_blocked_write_receipt_shape(self):
        out = run_clipboard(StubDriver(), action="write", text=SAMPLE_TEXT)
        assert set(out) == {
            "ok",
            "action",
            "dry_run",
            "tool",
            "written_kind",
            "would_call",
            "requires_confirm",
            "danger",
            "code",
            "error",
        }
        assert out["action"] == ACTION_WRITE
        assert out["tool"] == TOOL_WRITE
        assert "clipboard_write" in out["error"]["detail"]

    def test_confirm_without_execute_is_a_dry_run_that_never_touches_the_driver(self):
        out = run_clipboard(NeverCalledDriver(), action="write", text=SAMPLE_TEXT, confirm=True)
        assert out["ok"] is True
        assert out["dry_run"] is True
        assert out["would_call"]["tool"] == TOOL_WRITE
        # 干跑要给出**确切**的调用计划：确认者必须看见将要写进去的是什么
        assert out["would_call"]["args"] == {"text": SAMPLE_TEXT}
        assert "execute" in out["would_call"]["note"]
        assert out["written_kind"] == "text"
        assert out["code"] is None

    def test_execute_with_confirm_dispatches_exactly_once(self):
        driver = StubDriver(result=driver_read_result())
        out = run_clipboard(driver, action="write", text=SAMPLE_TEXT, confirm=True, execute=True)
        assert driver.calls == [(TOOL_WRITE, {"text": SAMPLE_TEXT})]
        assert out["ok"] is True
        assert out["dry_run"] is False
        assert out["written_kind"] == "text"
        assert out["written_chars"] == len(SAMPLE_TEXT)
        assert out["types"] == TYPES  # 驱动回的可读回类型
        assert "would_call" not in out
        # 真下发的回执不回显写入内容（回执会进会话/日志；要看将写什么就先干跑）
        assert SAMPLE_TEXT not in json.dumps(out, ensure_ascii=False)

    def test_image_kind_maps_to_image_path_arg(self, tmp_path):
        path = make_file(tmp_path, "pic.png")
        driver = StubDriver(result=driver_read_result())
        out = run_clipboard(driver, action="write", image_path=path, confirm=True, execute=True)
        assert driver.calls == [(TOOL_WRITE, {"image_path": path})]
        assert out["written_kind"] == "image"

    def test_file_kind_maps_to_file_path_arg(self, tmp_path):
        path = make_file(tmp_path, "doc.txt")
        driver = StubDriver(result=driver_read_result())
        out = run_clipboard(driver, action="write", file_path=path, confirm=True, execute=True)
        assert driver.calls == [(TOOL_WRITE, {"file_path": path})]
        assert out["written_kind"] == "file"

    def test_missing_file_is_refused_before_any_dispatch(self, tmp_path):
        """③：不存在的文件要在**碰驱动之前**拒（否则驱动只会给一句与原因无关的报错）。"""
        with pytest.raises(WinUseError) as ctx:
            run_clipboard(NeverCalledDriver(), action="write", file_path=str(tmp_path / "nope.txt"), confirm=True, execute=True)
        assert ctx.value.code == "CLIPBOARD_FILE_NOT_FOUND"

    def test_relative_image_path_is_refused_before_any_dispatch(self):
        with pytest.raises(WinUseError) as ctx:
            run_clipboard(NeverCalledDriver(), action="write", image_path="pic.png", confirm=True, execute=True)
        assert ctx.value.code == "CLIPBOARD_NOT_ABSOLUTE_PATH"

    def test_value_selection_is_validated_even_without_confirm(self):
        """三选一是**意图**问题：值都没定，给用户的"确认请求"就是一句空话，所以先校验。"""
        with pytest.raises(WinUseError) as ctx:
            run_clipboard(NeverCalledDriver(), action="write")
        assert ctx.value.code == "CLIPBOARD_MISSING_VALUE"
        with pytest.raises(WinUseError) as ctx:
            run_clipboard(NeverCalledDriver(), action="write", text=SAMPLE_TEXT, image_path="D:/a.png")
        assert ctx.value.code == "CLIPBOARD_MULTIPLE_VALUES"

    def test_write_dry_run_is_not_an_error(self, tmp_path):
        out = run_clipboard(StubDriver(), action="write", file_path=make_file(tmp_path), confirm=True)
        assert out["ok"] is True and out["dry_run"] is True


class TestWriteAudit:
    def test_executed_write_audit_keeps_only_kind_and_size(self):
        sink = Sink()
        run_clipboard(
            StubDriver(result=driver_read_result()),
            action="write",
            text=SAMPLE_TEXT,
            confirm=True,
            execute=True,
            audit=sink,
        )
        entry = sink.entries[-1]
        assert entry["event"] == "clipboard.executed"
        assert entry["ok"] is True
        assert entry["detail"]["kind"] == "text"
        assert entry["detail"]["written_chars"] == len(SAMPLE_TEXT)
        assert SAMPLE_TEXT not in json.dumps(sink.entries, ensure_ascii=False)

    def test_write_path_audit_never_carries_the_path(self, tmp_path):
        path = make_file(tmp_path, "doc.txt")
        sink = Sink()
        run_clipboard(StubDriver(result=driver_read_result()), action="write", file_path=path, confirm=True, execute=True, audit=sink)
        assert path not in json.dumps(sink.entries, ensure_ascii=False)
        assert sink.entries[-1]["detail"]["kind"] == "file"

    def test_dry_run_and_blocked_are_audited(self):
        sink = Sink()
        run_clipboard(StubDriver(), action="write", text=SAMPLE_TEXT, confirm=True, audit=sink)
        assert [e["event"] for e in sink.entries] == ["clipboard.dry_run"]

        sink = Sink()
        run_clipboard(StubDriver(), action="write", text=SAMPLE_TEXT, audit=sink)
        assert [e["event"] for e in sink.entries] == ["clipboard.blocked"]
        assert sink.entries[-1]["code"] == "CONFIRM_REQUIRED"

    def test_failed_write_is_audited(self):
        sink = Sink()
        driver = StubDriver(boom=WinUseError("驱动炸了", "细节"))
        out = run_clipboard(driver, action="write", text=SAMPLE_TEXT, confirm=True, execute=True, audit=sink)
        assert out["ok"] is False
        assert sink.entries[-1]["event"] == "clipboard.executed"
        assert sink.entries[-1]["ok"] is False


# --------------------------------------------------------------------------
# 失败归一（复用 act 的拒绝语义，不复制一份）
# --------------------------------------------------------------------------


class TestDriverFailureNormalization:
    def test_driver_refusal_is_not_success(self):
        driver = StubDriver(result={"ok": False, "code": "clipboard_locked", "message": "剪贴板被占用"})
        out = run_clipboard(driver, action="write", text=SAMPLE_TEXT, confirm=True, execute=True)
        assert out["ok"] is False
        assert out["code"] == "CLIPBOARD_LOCKED"
        assert "剪贴板被占用" in out["error"]["message"]

    def test_empty_driver_response_is_failure(self):
        out = run_clipboard(StubDriver(result={}), action="read")
        assert out["ok"] is False
        assert out["code"] == act_mod.EMPTY_RESPONSE_CODE

    def test_driver_exception_is_reported_not_raised(self):
        out = run_clipboard(StubDriver(boom=WinUseError("驱动炸了", "细节")), action="read")
        assert out["ok"] is False
        assert out["code"] == "WINUSE_ERROR"
        assert out["action"] == ACTION_READ

    def test_failure_receipt_shape(self):
        out = run_clipboard(StubDriver(boom=WinUseError("驱动炸了", "细节")), action="read")
        assert set(out) == {"ok", "action", "dry_run", "written_kind", "tool", "code", "error"}


# --------------------------------------------------------------------------
# 过一遍真实驱动边界（tests/fake_driver.py，与真实 CLI 同形）
# --------------------------------------------------------------------------


class TestViaFakeDriver:
    def test_types_only_read_reaches_the_cli(self):
        out = run_clipboard(CuaDriver(command=FAKE), action="read")
        assert out["ok"] is True
        assert out["tool"] == TOOL_READ
        assert out["types"] == []  # 假驱动不回 types 字段 → 不编一个

    def test_write_dry_run_never_spawns_the_driver(self):
        out = run_clipboard(CuaDriver(command=FAKE), action="write", text=SAMPLE_TEXT, confirm=True)
        assert out["dry_run"] is True
        assert "types" not in out
