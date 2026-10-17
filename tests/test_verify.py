"""winuse_verify（确定性断言）单测。

覆盖四层：

1. **谓词翻译**（`build_verify_call`）：把好写的谓词翻成驱动 `expect` 参数，
   并把驱动 schema 的硬约束在**本地**先拦（超 8 条、`element.exists=false`、
   选择器为空、bounds 缺字段、容差超 100、stable_samples/timeout_ms 越界）。
   为什么必须本地拦：`verify_state` 一次要跑 timeout_ms 的采样，参数错到
   驱动那边才发现，等于把编程错误变成一次真机往返 + 误导性的 unknown。
2. **结果解析**（`parse_verify_result`）：三态语义
   `satisfied / unsatisfied / unknown`，并把「unknown 绝不等于成功」写进 `ok`。
   不满足是**正常结果**（ok=True + verified=False），unknown 是**未证实**
   （ok=False + code=VERIFY_UNKNOWN）——混淆这两者会让 agent 把"没验证出来"
   当"验证通过"。
3. **干跑与真跑**：真跑分支全部走桩驱动，**没有一次真实断言下发**。
4. **审计与失败归一**：每次尝试落一条；驱动抛错 / 回空 / 回 ok=false 一律回收。

`verify_state` 在驱动分类里是 `read_only=true, destructive=false` ——
本模块因此**不设 confirm 门禁**（只读不改桌面状态），只保留干跑开关。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core import act as act_mod  # noqa: E402
from winuse.core.verify import (  # noqa: E402
    ACTION,
    CODE_UNKNOWN,
    CODE_UNSATISFIED,
    CODE_UNSTABLE,
    DEFAULT_STABLE_SAMPLES,
    DEFAULT_TIMEOUT_MS,
    MAX_PREDICATES,
    MAX_STABLE_SAMPLES,
    MAX_TIMEOUT_MS,
    SATISFIED,
    TOOL,
    UNKNOWN,
    UNSATISFIED,
    build_verify_call,
    parse_verify_result,
    run_verify,
)
from winuse.driver.errors import WinUseError  # noqa: E402

TARGET = {"pid": 100, "window_id": 200, "app_name": "Demo.exe", "title": "Demo"}

WINDOW_EXISTS = {"window": {"exists": True}}
ELEMENT_ROLE = {"element": {"selector": {"role": "Button"}}}


def satisfied_result(**over):
    """驱动实测回执形态（真机采样见 project-internal research notes）。"""
    base = {
        "elapsed_ms": 5383,
        "predicates": [
            {
                "index": 0,
                "observed_json": '{"bounds":{"height":777,"width":1080,"x":3846,"y":1089},"exists":true}',
                "status": "satisfied",
                "unknown_reason": None,
            }
        ],
        "samples": 5,
        "stable": True,
        "status": "satisfied",
    }
    base.update(over)
    return base


class StubDriver:
    def __init__(self, result=None, boom=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = {"ok": True} if result is None else result
        self.boom = boom

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if self.boom:
            raise self.boom
        return self.result


class Sink:
    def __init__(self):
        self.entries: list[dict] = []

    def __call__(self, entry):
        self.entries.append(entry)


# --------------------------------------------------------------------------
# 谓词翻译与本地校验
# --------------------------------------------------------------------------


class TestBuildVerifyCall:
    def test_window_exists_predicate(self):
        tool, args = build_verify_call(TARGET, [WINDOW_EXISTS])
        assert tool == TOOL == "verify_state"
        assert args == {
            "pid": 100,
            "window_id": 200,
            "expect": [{"window": {"exists": True}}],
            "include_screenshot": False,
            "stable_samples": DEFAULT_STABLE_SAMPLES,
            "timeout_ms": DEFAULT_TIMEOUT_MS,
        }

    def test_text_first_by_default(self):
        """默认不取截图：断言走文本闭环，不花截图与视觉推理的钱（与 state 同口径）。"""
        _, args = build_verify_call(TARGET, [WINDOW_EXISTS])
        assert args["include_screenshot"] is False
        _, args = build_verify_call(TARGET, [WINDOW_EXISTS], include_screenshot=True)
        assert args["include_screenshot"] is True

    def test_element_selector_gains_explicit_exists(self):
        """给了选择器就自动补 `exists: true`——驱动只接受 true（false 会被它直接拒），
        显式声明比靠缺省更清楚，回执也能自解释「这条断言要的是存在」。"""
        _, args = build_verify_call(TARGET, [ELEMENT_ROLE])
        assert args["expect"] == [{"element": {"selector": {"role": "Button"}, "exists": True}}]

    def test_element_selector_with_label_contains(self):
        _, args = build_verify_call(
            TARGET, [{"element": {"selector": {"role": "Button", "label_contains": "保存"}}}]
        )
        assert args["expect"][0]["element"]["selector"]["label_contains"] == "保存"

    def test_optional_element_fields_are_kept(self):
        _, args = build_verify_call(
            TARGET,
            [{"element": {"selector": {"label_contains": "保存"}, "enabled": True, "selected": False, "value_equals": "ok"}}],
        )
        el = args["expect"][0]["element"]
        assert el["enabled"] is True
        assert el["selected"] is False
        assert el["value_equals"] == "ok"

    def test_none_valued_optional_fields_are_dropped(self):
        """显式给 None = 「不关心这一项」→ 不下发（省得让驱动去解释 null）。"""
        _, args = build_verify_call(
            TARGET, [{"element": {"selector": {"role": "Button"}, "enabled": None, "value_equals": None}}]
        )
        el = args["expect"][0]["element"]
        assert "enabled" not in el
        assert "value_equals" not in el

    def test_window_and_element_in_one_predicate_is_allowed(self):
        _, args = build_verify_call(TARGET, [{"window": {"exists": True}, "element": ELEMENT_ROLE["element"]}])
        assert set(args["expect"][0]) == {"window", "element"}

    def test_window_bounds_predicate(self):
        _, args = build_verify_call(
            TARGET,
            [{"window": {"bounds": {"x": 1, "y": 2, "width": 300, "height": 400, "tolerance_px": 2}}}],
        )
        assert args["expect"][0]["window"]["bounds"] == {"x": 1, "y": 2, "width": 300, "height": 400, "tolerance_px": 2}

    def test_bounds_without_tolerance_is_allowed(self):
        _, args = build_verify_call(TARGET, [{"window": {"bounds": {"x": 0, "y": 0, "width": 10, "height": 10}}}])
        assert "tolerance_px" not in args["expect"][0]["window"]["bounds"]

    def test_missing_target_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call({"pid": 100}, [WINDOW_EXISTS])
        assert ctx.value.code == "MISSING_TARGET"

    def test_empty_or_non_sequence_predicates_is_refused(self):
        for bad in ([], (), None, "exists"):
            with pytest.raises(WinUseError) as ctx:
                build_verify_call(TARGET, bad)
            assert ctx.value.code == "MISSING_PARAM", bad

    def test_too_many_predicates_is_out_of_range(self):
        too_many = [WINDOW_EXISTS] * (MAX_PREDICATES + 1)
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, too_many)
        assert ctx.value.code == "PARAM_OUT_OF_RANGE"
        # 上界本身合法（驱动 schema: maxItems 8）
        assert len(build_verify_call(TARGET, [WINDOW_EXISTS] * MAX_PREDICATES)[1]["expect"]) == MAX_PREDICATES

    def test_predicate_must_be_mapping(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, ["window"])
        assert ctx.value.code == "INVALID_PARAMS"

    def test_unknown_predicate_key_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, [{"windows": {"exists": True}}])
        assert ctx.value.code == "INVALID_PARAMS"
        assert "window" in ctx.value.detail

    def test_empty_predicate_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, [{}])
        assert ctx.value.code == "MISSING_PARAM"

    def test_element_exists_false_is_refused_locally(self):
        """驱动明说：元素遍历不保证穷尽，「不存在」无法证明 → 它直接拒 false。
        本地先拦，错误信息能讲清为什么，而不是让驱动回一句 schema 报错。"""
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, [{"element": {"exists": False, "selector": {"role": "Button"}}}])
        assert ctx.value.code == "MISSING_PARAM"
        assert "不存在" in ctx.value.message

    def test_element_without_selector_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, [{"element": {"exists": True}}])
        assert ctx.value.code == "MISSING_PARAM"

    def test_selector_without_any_criterion_is_refused(self):
        for bad in ({}, {"role": ""}, {"label_contains": "   "}, {"other": "x"}):
            with pytest.raises(WinUseError) as ctx:
                build_verify_call(TARGET, [{"element": {"selector": bad}}])
            assert ctx.value.code in ("MISSING_PARAM", "INVALID_PARAMS"), bad

    def test_element_unknown_key_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, [{"element": {"selector": {"role": "Button"}, "visible": True}}])
        assert ctx.value.code == "INVALID_PARAMS"

    def test_element_bool_fields_must_be_bool(self):
        for bad in ({"enabled": "yes"}, {"selected": 1}, {"value_equals": 3}):
            with pytest.raises(WinUseError) as ctx:
                build_verify_call(TARGET, [{"element": {**bad, "selector": {"role": "Button"}}}])
            assert ctx.value.code in ("INVALID_PARAMS", "MISSING_PARAM"), bad

    def test_bounds_requires_all_four_numbers(self):
        for missing in ("x", "y", "width", "height"):
            bounds = {"x": 0, "y": 0, "width": 10, "height": 10}
            bounds.pop(missing)
            with pytest.raises(WinUseError) as ctx:
                build_verify_call(TARGET, [{"window": {"bounds": bounds}}])
            assert ctx.value.code == "MISSING_PARAM", missing

    def test_bounds_tolerance_range(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, [{"window": {"bounds": {"x": 0, "y": 0, "width": 1, "height": 1, "tolerance_px": 101}}}])
        assert ctx.value.code == "PARAM_OUT_OF_RANGE"
        build_verify_call(TARGET, [{"window": {"bounds": {"x": 0, "y": 0, "width": 1, "height": 1, "tolerance_px": 100}}}])

    def test_empty_window_predicate_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, [{"window": {}}])
        assert ctx.value.code == "MISSING_PARAM"

    def test_stable_samples_range(self):
        for bad in (0, MAX_STABLE_SAMPLES + 1, -1):
            with pytest.raises(WinUseError) as ctx:
                build_verify_call(TARGET, [WINDOW_EXISTS], stable_samples=bad)
            assert ctx.value.code == "PARAM_OUT_OF_RANGE", bad
        for ok in (1, MAX_STABLE_SAMPLES):
            assert build_verify_call(TARGET, [WINDOW_EXISTS], stable_samples=ok)[1]["stable_samples"] == ok

    def test_timeout_ms_range(self):
        for bad in (-1, MAX_TIMEOUT_MS + 1):
            with pytest.raises(WinUseError) as ctx:
                build_verify_call(TARGET, [WINDOW_EXISTS], timeout_ms=bad)
            assert ctx.value.code == "PARAM_OUT_OF_RANGE", bad
        for ok in (0, MAX_TIMEOUT_MS):
            assert build_verify_call(TARGET, [WINDOW_EXISTS], timeout_ms=ok)[1]["timeout_ms"] == ok

    def test_timeout_must_be_int(self):
        with pytest.raises(WinUseError) as ctx:
            build_verify_call(TARGET, [WINDOW_EXISTS], timeout_ms="5000")
        assert ctx.value.code == "INVALID_PARAMS"

    def test_predicates_are_not_mutated(self):
        """纯函数不得改调用方传进来的对象（否则复用同一份谓词会串味）。"""
        predicates = [{"element": {"selector": {"role": "Button"}}}]
        snapshot = [{"element": {"selector": {"role": "Button"}}}]
        build_verify_call(TARGET, predicates)
        assert predicates == snapshot


# --------------------------------------------------------------------------
# 结果解析：三态语义
# --------------------------------------------------------------------------


class TestParseVerifyResult:
    def test_satisfied_is_success(self):
        out = parse_verify_result(satisfied_result())
        assert out["ok"] is True
        assert out["verified"] is True
        assert out["status"] == SATISFIED
        assert out["stable"] is True
        assert out["samples"] == 5
        assert out["elapsed_ms"] == 5383
        assert out["predicates"][0]["status"] == "satisfied"
        assert out["predicates"][0]["observed"]["exists"] is True
        assert "error" not in out

    def test_unsatisfied_is_a_normal_result_not_an_error(self):
        """「没断言通过」是有效结论（像 wait 超时一样）：ok=True + verified=False。"""
        out = parse_verify_result(
            satisfied_result(status="unsatisfied", stable=False, predicates=[{"index": 0, "status": "unsatisfied", "observed_json": "{}", "unknown_reason": None}])
        )
        assert out["ok"] is True
        assert out["verified"] is False
        assert out["status"] == UNSATISFIED
        assert out["code"] == CODE_UNSATISFIED

    def test_unknown_never_implies_success(self):
        """驱动原话：unknown never implies success —— 本层 fail-closed。"""
        out = parse_verify_result(
            satisfied_result(
                status="unknown",
                stable=False,
                predicates=[{"index": 1, "status": "unknown", "observed_json": '{"matches":8}', "unknown_reason": "multi_match"}],
            )
        )
        assert out["ok"] is False
        assert out["verified"] is False
        assert out["code"] == CODE_UNKNOWN
        assert out["error"]["code"] == CODE_UNKNOWN
        assert "multi_match" in out["error"]["detail"]
        assert out["predicates"][0]["unknown_reason"] == "multi_match"

    def test_missing_status_is_treated_as_unknown(self):
        out = parse_verify_result({"predicates": []})
        assert out["ok"] is False
        assert out["status"] == UNKNOWN
        assert out["code"] == CODE_UNKNOWN

    def test_unrecognized_status_is_treated_as_unknown(self):
        out = parse_verify_result(satisfied_result(status="maybe"))
        assert out["ok"] is False
        assert out["status"] == UNKNOWN

    def test_satisfied_but_unstable_is_fail_closed(self):
        """status 说满足、stable 说没稳定 —— 自相矛盾时不许报成功（fail-closed）。"""
        out = parse_verify_result(satisfied_result(stable=False))
        assert out["ok"] is False
        assert out["verified"] is False
        assert out["code"] == CODE_UNSTABLE
        # status 保留驱动原话：回执要能同时回答「驱动说了什么」与「我们怎么判」
        assert out["status"] == SATISFIED

    def test_empty_response_is_failure(self):
        for empty in ({}, None):
            out = parse_verify_result(empty)
            assert out["ok"] is False
            assert out["error"]["code"] == act_mod.EMPTY_RESPONSE_CODE

    def test_driver_refusal_is_failure(self):
        out = parse_verify_result({"ok": False, "code": "window_not_found", "message": "窗口没了"})
        assert out["ok"] is False
        assert out["error"]["code"] == "WINDOW_NOT_FOUND"

    def test_observed_json_is_parsed_but_raw_is_kept(self):
        out = parse_verify_result(satisfied_result())
        assert out["predicates"][0]["observed"] == {"bounds": {"height": 777, "width": 1080, "x": 3846, "y": 1089}, "exists": True}
        assert out["predicates"][0]["observed_json"].startswith("{")

    def test_unparsable_observed_json_does_not_crash(self):
        out = parse_verify_result(
            satisfied_result(predicates=[{"index": 0, "status": "satisfied", "observed_json": "<不是 JSON>", "unknown_reason": None}])
        )
        assert out["ok"] is True
        assert out["predicates"][0]["observed"] is None
        assert out["predicates"][0]["observed_json"] == "<不是 JSON>"


# --------------------------------------------------------------------------
# 干跑 / 真跑 / 审计
# --------------------------------------------------------------------------


class TestRunVerify:
    def test_dry_run_defaults_to_no_dispatch(self):
        driver = StubDriver()
        out = run_verify(driver, target=TARGET, predicates=[WINDOW_EXISTS])
        assert driver.calls == []
        assert out["ok"] is True
        assert out["dry_run"] is True
        assert out["would_call"]["tool"] == TOOL
        assert out["would_call"]["args"]["expect"] == [{"window": {"exists": True}}]
        assert "干跑" in out["would_call"]["note"]
        assert "status" not in out

    def test_read_only_means_no_confirm_gate(self):
        """驱动分类 read_only=true / destructive=false → 不设 confirm 门禁。"""
        out = run_verify(StubDriver(), target=TARGET, predicates=[WINDOW_EXISTS])
        assert "requires_confirm" not in out
        assert "danger" not in out

    def test_execute_dispatches_once_and_projects_result(self):
        driver = StubDriver(satisfied_result())
        out = run_verify(driver, target=TARGET, predicates=[WINDOW_EXISTS], execute=True)
        assert len(driver.calls) == 1
        assert driver.calls[0][0] == TOOL
        assert out["dry_run"] is False
        assert out["ok"] is True
        assert out["verified"] is True
        assert out["status"] == SATISFIED
        assert out["action"] == ACTION
        assert out["args"]["include_screenshot"] is False
        # 原始回执保留（排查用），但不含截图 base64
        assert out["result"]["samples"] == 5

    def test_driver_error_is_reported_not_raised(self):
        driver = StubDriver(boom=WinUseError("驱动炸了", "细节"))
        out = run_verify(driver, target=TARGET, predicates=[WINDOW_EXISTS], execute=True)
        assert out["ok"] is False
        assert out["code"] == "WINUSE_ERROR"

    def test_audit_events(self):
        sink = Sink()
        run_verify(StubDriver(), target=TARGET, predicates=[WINDOW_EXISTS], audit=sink)
        assert [e["event"] for e in sink.entries] == ["verify.dry_run"]

        sink = Sink()
        run_verify(StubDriver(satisfied_result()), target=TARGET, predicates=[WINDOW_EXISTS], execute=True, audit=sink)
        assert [e["event"] for e in sink.entries] == ["verify.executed"]
        assert sink.entries[-1]["ok"] is True

    def test_audit_records_failed_verification(self):
        sink = Sink()
        out = run_verify(
            StubDriver(satisfied_result(status="unknown", stable=False)),
            target=TARGET,
            predicates=[WINDOW_EXISTS],
            execute=True,
            audit=sink,
        )
        assert out["ok"] is False
        assert sink.entries[-1]["ok"] is False
        assert sink.entries[-1]["code"] == CODE_UNKNOWN

    def test_parameter_errors_still_raise(self):
        """谓词拼错是编程错误，直接抛，不该被吞成 ok=false。"""
        with pytest.raises(WinUseError):
            run_verify(StubDriver(), target=TARGET, predicates=[{"element": {"exists": False, "selector": {"role": "Button"}}}])
