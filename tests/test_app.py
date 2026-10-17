"""winuse_app（应用生命周期：launch / list / kill）单测。

覆盖四层，缺一层都算没守住：

1. **纯逻辑**（`build_app_call`）：动作 → 驱动调用。给什么参数就产什么调用，
   参数缺失/类型不对/动作未知一律抛带稳定 code 的异常（可穷举断言，不碰驱动）。
2. **裁剪**（`trim_apps`）：驱动 `list_apps` 的原始回执**实测 92,706 字节 / 261 条**，
   远超 MCP 线路预算——必须投影 + 截断 + 标注，且标注要跟 `winuse/limit.py` 同形
   （裁剪要留痕，不能静默少几条）。
3. **干跑与真跑**：默认干跑（一行都不下发），`execute=True` 才真发；真跑分支
   全部走桩驱动，**没有任何一次真实 launch / kill**。
4. **危险门与审计**：kill 必须在未确认时返回 `CONFIRM_REQUIRED` 且不下发；判据
   来自 `winuse.core.guard`（单一出处，本文件从 guard 的常量驱动穷举断言，
   不复制那份清单）；每次尝试都要落审计。

铁律：**单测不碰真实桌面**（桩驱动 + 桩 sink），也不写真名实姓的本机路径
（占位路径 `C:/Apps/Demo/...`，不是本机任何真实目录）。
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse import limit as limit_mod  # noqa: E402
from winuse.core import act as act_mod  # noqa: E402
from winuse.core import app as app_mod  # noqa: E402
from winuse.core import guard as guard_mod  # noqa: E402
from winuse.core.app import (  # noqa: E402
    DEFAULT_APP_LIMIT,
    MAX_APP_LIMIT,
    BadParam,
    build_app_call,
    run_app,
    trim_apps,
)
from winuse.driver import CuaDriver  # noqa: E402
from winuse.driver.errors import (  # noqa: E402
    DriverError,
    MissingParam,
    ParamOutOfRange,
    UnknownAction,
)

#: `tests/fake_driver.py` 当真实驱动用（同 `test_cua_driver.py` 的口径）：
#: 它只回一个通用回执，专门验证"驱动回执缺字段时本模块不编值"
FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_driver.py")]

#: 占位路径（不是本机真实路径；数据卫生铁律）
DEMO_PATH = "C:/Apps/Demo/demo.exe"
OTHER_PATH = "C:/Apps/Other/other.exe"
PLAIN_APP = "demo.exe"


def raw_app(
    name="Demo",
    pid=100,
    running=True,
    active=False,
    kind="desktop",
    launch_path=DEMO_PATH,
    last_used="2026-01-01T00:00:00Z",
    bundle_id=None,
    windows=None,
):
    """造一条 list_apps 记录（字段与驱动实测形态一致）。"""
    return {
        "active": active,
        "bundle_id": bundle_id,
        "kind": kind,
        "last_used": last_used,
        "launch_path": launch_path,
        "name": name,
        "pid": pid,
        "running": running,
        "windows": windows if windows is not None else [],
    }


def raw_payload(count=3):
    return {"apps": [raw_app(name=f"占位应用{i}", pid=100 + i) for i in range(count)]}


class StubDriver:
    """假的驱动：记录调用，按 tool 回预设回执；可注入异常验证错误归一化。"""

    def __init__(self, responses=None, error=None):
        self.responses = dict(responses or {})
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args=None, timeout_s=None, check_version=True):
        self.calls.append((tool, args))
        if self.error is not None:
            raise self.error
        return self.responses.get(tool, {"tool": tool})


@pytest.fixture
def audit_events():
    """审计 sink：`list.append` 本身就是合法 sink（协议见 audit.sink_event）。"""
    return []


# ---------------------------------------------------------------- 接口契约


def test_signature_matches_contract():
    """签名是**对外契约**：driver 位置参数，其余一律关键字专用。

    允许在末尾追加**可选**关键字参数（本项目所有核心入口都带 `audit=` 审计通道，
    见 `core.act.run_act` / `core.desktop.window_action`）——但不许改既定参数的
    名字、顺序与默认值。
    """
    params = list(inspect.signature(run_app).parameters.values())
    head = params[:8]
    assert [p.name for p in head] == [
        "driver", "action", "pid", "path", "args", "confirm", "execute", "options",
    ]
    assert head[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in head[1:])
    assert [p.default for p in head[2:]] == [None, None, None, False, False, None]
    for extra in params[8:]:
        assert extra.kind is inspect.Parameter.KEYWORD_ONLY, extra.name
        assert extra.default is not inspect.Parameter.empty, extra.name


def test_action_set_is_exactly_three():
    assert app_mod.APP_ACTIONS == frozenset({"launch", "list", "kill"})


def test_limit_constants_are_ordered_and_useful():
    """上限必须 ≥ 默认（否则默认值本身就越界），且默认要装得下常见问法
    ——本机实测"运行中的应用"只有 10 个；默认小于 20 就等于让 list 什么也看不出来。"""
    assert MAX_APP_LIMIT >= DEFAULT_APP_LIMIT
    assert DEFAULT_APP_LIMIT >= 20


# ---------------------------------------------------------------- 纯逻辑


class TestBuildAppCall:
    def test_unknown_action_raises(self):
        with pytest.raises(UnknownAction) as ctx:
            build_app_call("teleport")
        assert ctx.value.code == "UNKNOWN_ACTION"

    def test_launch_path_only(self):
        assert build_app_call("launch", path=DEMO_PATH) == ("launch_app", {"path": DEMO_PATH})

    def test_launch_without_path_or_identifier_raises_missing_param(self):
        with pytest.raises(MissingParam) as ctx:
            build_app_call("launch")
        assert ctx.value.code == "MISSING_PARAM"

    def test_launch_string_args_treated_as_one_argument(self):
        """单个字符串按**一个参数**处理：按空格切分会把带空格的路径拆坏。"""
        tool, args = build_app_call("launch", path=DEMO_PATH, args="--profile demo")
        assert tool == "launch_app"
        assert args["additional_arguments"] == ["--profile demo"]

    def test_launch_list_args_preserved_in_order(self):
        _, args = build_app_call("launch", path=DEMO_PATH, args=["--a", "x y", "--b"])
        assert args["additional_arguments"] == ["--a", "x y", "--b"]

    def test_launch_empty_args_list_not_sent(self):
        _, args = build_app_call("launch", path=DEMO_PATH, args=[])
        assert "additional_arguments" not in args

    def test_launch_non_string_arg_rejected(self):
        with pytest.raises(BadParam) as ctx:
            build_app_call("launch", path=DEMO_PATH, args=["--a", 2])
        assert ctx.value.code == "BAD_PARAM"

    def test_launch_args_wrong_container_rejected(self):
        with pytest.raises(BadParam):
            build_app_call("launch", path=DEMO_PATH, args={"a": 1})

    def test_launch_blank_path_rejected(self):
        with pytest.raises(BadParam):
            build_app_call("launch", path="   ")

    def test_launch_identifier_options_accepted(self):
        _, args = build_app_call("launch", path=None, options={"aumid": "Vendor.App_abc!App"})
        assert args == {"aumid": "Vendor.App_abc!App"}

    def test_launch_urls_option_accepted(self):
        _, args = build_app_call("launch", options={"urls": ["https://example.invalid/"]})
        assert args["urls"] == ["https://example.invalid/"]

    def test_launch_bad_url_type_rejected(self):
        with pytest.raises(BadParam):
            build_app_call("launch", options={"urls": "https://example.invalid/"})

    def test_launch_start_minimized_must_be_real_bool(self):
        _, args = build_app_call("launch", path=DEMO_PATH, options={"start_minimized": True})
        assert args["start_minimized"] is True
        with pytest.raises(BadParam):
            build_app_call("launch", path=DEMO_PATH, options={"start_minimized": "yes"})

    def test_launch_unknown_option_rejected(self):
        """不静默丢：认不出的选项一律拒（否则调用方以为生效了）。"""
        with pytest.raises(BadParam) as ctx:
            build_app_call("launch", path=DEMO_PATH, options={"foreground": True})
        assert "foreground" in ctx.value.message

    def test_launch_rejects_pid(self):
        with pytest.raises(BadParam):
            build_app_call("launch", pid=42, path=DEMO_PATH)

    def test_list_has_no_driver_args(self):
        """驱动 list_apps 的 schema 是 additionalProperties:false + 无属性 → 只能传 {}。"""
        assert build_app_call("list") == ("list_apps", {})
        assert build_app_call("list", options={"running": True, "query": "note"}) == ("list_apps", {})

    def test_list_filters_validated(self):
        with pytest.raises(BadParam):
            build_app_call("list", options={"kind": "store"})
        with pytest.raises(BadParam):
            build_app_call("list", options={"running": "yes"})
        with pytest.raises(BadParam):
            build_app_call("list", options={"query": 3})
        with pytest.raises(BadParam):
            build_app_call("list", options={"limit": "many"})
        with pytest.raises(BadParam):
            build_app_call("list", options={"sort": "name"})

    def test_list_limit_over_cap_is_out_of_range(self):
        with pytest.raises(ParamOutOfRange) as ctx:
            build_app_call("list", options={"limit": MAX_APP_LIMIT + 1})
        assert ctx.value.code == "PARAM_OUT_OF_RANGE"

    def test_list_limit_must_be_positive(self):
        with pytest.raises(BadParam):
            build_app_call("list", options={"limit": 0})

    def test_list_rejects_path_args_pid(self):
        for kwargs in ({"path": DEMO_PATH}, {"args": ["x"]}, {"pid": 7}):
            with pytest.raises(BadParam):
                build_app_call("list", **kwargs)

    def test_kill_needs_pid(self):
        assert build_app_call("kill", pid=42) == ("kill_app", {"pid": 42})
        with pytest.raises(MissingParam) as ctx:
            build_app_call("kill")
        assert ctx.value.code == "MISSING_PARAM"

    def test_kill_pid_must_be_positive_int(self):
        for bad in (True, "42", 0, -3, 1.5, None):
            with pytest.raises((BadParam, MissingParam)):
                build_app_call("kill", pid=bad)

    def test_kill_rejects_options_path_args(self):
        for kwargs in ({"options": {"force": True}}, {"path": DEMO_PATH}, {"args": ["x"]}):
            with pytest.raises(BadParam):
                build_app_call("kill", pid=42, **kwargs)


# ---------------------------------------------------------------- 裁剪


class TestTrimApps:
    def test_slim_projection_keeps_actionable_fields_only(self):
        payload = {"apps": [raw_app(name="占位应用", pid=7, bundle_id="Vendor_abc", windows=[{"x": 1}])]}
        out = trim_apps(payload)
        record = out["apps"][0]
        assert record["name"] == "占位应用"
        assert record["pid"] == 7
        assert record["running"] is True
        assert record["launch_path"] == DEMO_PATH
        # 原始 windows 数组不进结果（改成计数）——那是 list_windows / winuse_state 的活
        assert "windows" not in record
        assert record["window_count"] == 1

    def test_bundle_id_kept_only_when_it_differs_from_launch_path(self):
        same = trim_apps({"apps": [raw_app(bundle_id=DEMO_PATH)]})["apps"][0]
        assert "bundle_id" not in same
        uwp = trim_apps(
            {"apps": [raw_app(kind="uwp", bundle_id="Vendor_abc", launch_path="shell:appsFolder\\Vendor_abc!App")]}
        )["apps"][0]
        assert uwp["bundle_id"] == "Vendor_abc"

    def test_counts_reported(self):
        out = trim_apps(raw_payload(5))
        assert out["apps_total"] == 5
        assert out["apps_matched"] == 5
        assert out["returned"] == 5
        assert out["truncated"] is False
        assert out["truncate_hint"] is None

    def test_running_filter(self):
        payload = {"apps": [raw_app(name="A"), raw_app(name="B", pid=0, running=False)]}
        out = trim_apps(payload, {"running": True})
        assert [a["name"] for a in out["apps"]] == ["A"]
        assert out["apps_total"] == 2 and out["apps_matched"] == 1
        assert out["filters"] == {"running": True}

    def test_kind_filter(self):
        payload = {"apps": [raw_app(name="A"), raw_app(name="B", kind="uwp")]}
        out = trim_apps(payload, {"kind": "uwp"})
        assert [a["name"] for a in out["apps"]] == ["B"]

    def test_query_matches_name_path_and_bundle_id_case_insensitively(self):
        payload = {
            "apps": [
                raw_app(name="Notepad"),
                raw_app(name="Other", launch_path=OTHER_PATH),
                raw_app(name="Packaged", kind="uwp", bundle_id="Vendor.Notepad_abc", launch_path="shell:appsFolder\\x!App"),
            ]
        }
        names = [a["name"] for a in trim_apps(payload, {"query": "note"})["apps"]]
        assert names == ["Notepad", "Packaged"]
        assert [a["name"] for a in trim_apps(payload, {"query": "other.exe"})["apps"]] == ["Other"]

    def test_limit_truncates_with_annotations(self):
        out = trim_apps(raw_payload(10), {"limit": 4})
        assert len(out["apps"]) == 4
        assert out["truncated"] is True
        assert out["returned"] == 4
        assert out["total_before_truncate"] == 10
        assert out["apps_total"] == 10
        assert "4" in out["truncate_hint"] and "10" in out["truncate_hint"]

    def test_truncation_is_measured_after_filtering(self):
        payload = {"apps": [raw_app(name="A"), raw_app(name="B", pid=0, running=False)]}
        out = trim_apps(payload, {"running": True, "limit": 5})
        assert out["truncated"] is False  # 命中的只有 1 条，没到上限

    def test_default_limit_applies(self):
        out = trim_apps(raw_payload(DEFAULT_APP_LIMIT + 3))
        assert out["returned"] == DEFAULT_APP_LIMIT
        assert out["truncated"] is True

    def test_payload_shapes_tolerated(self):
        assert trim_apps({})["apps"] == []
        assert trim_apps(None)["apps"] == []
        assert trim_apps({"apps": None})["apps"] == []
        assert len(trim_apps([raw_app()])["apps"]) == 1  # 直接给数组也能认

    def test_broken_records_skipped_not_crashed(self):
        out = trim_apps({"apps": [raw_app(), "不是记录", None]})
        assert len(out["apps"]) == 1
        assert out["apps_total"] == 3  # 计数如实报告（不假装驱动只给了 1 条）

    def test_input_payload_not_mutated(self):
        """原件不删不改：裁剪只出新的对象。"""
        payload = raw_payload(3)
        before = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        trim_apps(payload, {"limit": 1})
        assert json.dumps(payload, ensure_ascii=False, sort_keys=True) == before

    def test_max_limit_result_fits_wire_budget(self):
        """上限必须**实测撑得住**线路预算：驱动原样 261 条 92,706 字节，
        投影后每条约 227 字节（实测均摊）。这里按偏长的路径造满上限，
        断言仍在 MCP 的 24,000 字节线路预算内——否则上层会被判 RESULT_TOO_LARGE。
        """
        long_path = "C:/Apps/" + "d" * 60 + "/demo.exe"  # 约 85 字符，比实测均值（57）偏长
        payload = {"apps": [raw_app(name=f"占位应用{i}", pid=1000 + i, launch_path=long_path) for i in range(MAX_APP_LIMIT + 20)]}
        out = trim_apps(payload, {"limit": MAX_APP_LIMIT})
        assert out["truncated"] is True
        assert len(out["apps"]) == MAX_APP_LIMIT
        wire = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        assert wire < limit_mod.MAX_WIRE_BYTES, f"上限条数撑不住线路预算：{wire} 字节"


# ---------------------------------------------------------------- 干跑


class TestDryRun:
    def test_launch_dry_run_does_not_touch_driver(self):
        driver = StubDriver()
        out = run_app(driver, action="launch", path=DEMO_PATH, args=["--demo"])
        assert driver.calls == []
        assert out["ok"] is True and out["dry_run"] is True
        assert out["action"] == "launch"
        assert out["tool"] == "launch_app"
        assert out["would_call"]["tool"] == "launch_app"
        assert out["would_call"]["args"]["path"] == DEMO_PATH
        assert out["would_call"]["args"]["additional_arguments"] == ["--demo"]
        assert out["code"] is None

    def test_list_dry_run_marks_read_only(self):
        driver = StubDriver()
        out = run_app(driver, action="list")
        assert driver.calls == []
        assert out["ok"] is True and out["dry_run"] is True
        assert out["read_only"] is True
        assert out["would_call"] == {"tool": "list_apps", "args": {}, "note": out["would_call"]["note"]}
        assert "execute=True" in out["would_call"]["note"]

    def test_kill_dry_run_after_confirm(self):
        driver = StubDriver()
        out = run_app(driver, action="kill", pid=42, confirm=True)
        assert driver.calls == []
        assert out["ok"] is True and out["dry_run"] is True
        assert out["would_call"]["args"] == {"pid": 42}
        assert out["danger"]["dangerous"] is True  # 危险动作即使干跑也如实标注

    def test_unknown_action_is_structured_failure(self):
        driver = StubDriver()
        out = run_app(driver, action="teleport")
        assert driver.calls == []
        assert out["ok"] is False
        assert out["code"] == "UNKNOWN_ACTION"
        assert out["error"]["code"] == "UNKNOWN_ACTION"
        assert out["would_call"] is None

    def test_missing_param_is_structured_failure(self):
        out = run_app(StubDriver(), action="launch")
        assert out["ok"] is False and out["code"] == "MISSING_PARAM"

    def test_bad_param_is_structured_failure(self):
        out = run_app(StubDriver(), action="list", options={"limit": "many"})
        assert out["ok"] is False and out["code"] == "BAD_PARAM"

    def test_failure_receipt_keeps_same_keys(self):
        """干跑/失败/成功三种回执同族（agent 按 ok+code 分支，不用猜字段有没有）。"""
        out = run_app(StubDriver(), action="launch")
        for key in ("ok", "dry_run", "action", "tool", "target", "would_call", "code", "error"):
            assert key in out, key


# ---------------------------------------------------------------- 真跑


class TestExecute:
    def test_launch_execute_passes_call_and_reports_pid_and_windows(self):
        driver = StubDriver(
            {
                "launch_app": {
                    "pid": 4321,
                    "name": "Demo",
                    "bundle_id": None,
                    "active": False,
                    "windows": [
                        {"window_id": 90001, "pid": 4321, "title": "占位标题", "minimized": False,
                         "app_name": PLAIN_APP, "bounds": {"x": 0, "y": 0, "width": 1, "height": 1}},
                    ],
                }
            }
        )
        out = run_app(driver, action="launch", path=DEMO_PATH, args=["--demo"], execute=True)
        assert driver.calls == [("launch_app", {"path": DEMO_PATH, "additional_arguments": ["--demo"]})]
        assert out["ok"] is True and out["dry_run"] is False
        assert out["result"]["pid"] == 4321
        assert out["result"]["window_count"] == 1
        assert out["result"]["windows"][0]["window_id"] == 90001
        # 窗口投影去掉 bounds 之类的大块坐标（清单类细节走 winuse_state）
        assert "bounds" not in out["result"]["windows"][0]
        # 回执完整（有 pid 也有窗口）就不该再挂提示——不做无意义的噪声
        assert "note" not in out

    def test_launch_execute_without_windows_says_so(self):
        """驱动文档明确：启动未稳定时 windows 会是空的（不是失败）。"""
        driver = StubDriver({"launch_app": {"pid": 7, "name": "Demo", "windows": []}})
        out = run_app(driver, action="launch", path=DEMO_PATH, execute=True)
        assert out["ok"] is True
        assert out["result"]["window_count"] == 0
        assert "winuse_state" in out["note"]

    def test_launch_execute_missing_pid_is_not_invented(self):
        driver = StubDriver({"launch_app": {"name": "Demo", "windows": []}})
        out = run_app(driver, action="launch", path=DEMO_PATH, execute=True)
        assert out["result"]["pid"] is None  # 不编值
        assert "pid" in out["note"]

    def test_list_execute_returns_trimmed_apps(self):
        driver = StubDriver({"list_apps": raw_payload(3)})
        out = run_app(driver, action="list", execute=True)
        assert driver.calls == [("list_apps", {})]
        assert out["ok"] is True and out["dry_run"] is False
        assert out["read_only"] is True
        assert len(out["apps"]) == 3
        assert out["apps_total"] == 3
        assert out["note"]  # 投影口径要写在回执里

    def test_list_execute_applies_filters_and_limit(self):
        driver = StubDriver({"list_apps": {"apps": [raw_app(name=f"A{i}") for i in range(6)] + [raw_app(name="B", pid=0, running=False)]}})
        out = run_app(driver, action="list", execute=True, options={"limit": 2})
        assert out["apps_total"] == 7 and out["apps_matched"] == 7 and out["returned"] == 2
        assert out["truncated"] is True
        out = run_app(driver, action="list", execute=True, options={"running": True})
        assert [a["name"] for a in out["apps"]] == [f"A{i}" for i in range(6)]

    def test_kill_execute_passes_pid(self):
        driver = StubDriver({"kill_app": {"terminated": True}})
        out = run_app(driver, action="kill", pid=4321, confirm=True, execute=True)
        assert driver.calls == [("kill_app", {"pid": 4321})]
        assert out["ok"] is True and out["dry_run"] is False
        assert out["result"] == {"terminated": True}  # 回执原样透传：不猜字段名、不静默丢

    def test_driver_refusal_is_not_success(self):
        driver = StubDriver({"kill_app": {"ok": False, "code": "no_such_pid", "message": "进程不存在"}})
        out = run_app(driver, action="kill", pid=4321, confirm=True, execute=True)
        assert out["ok"] is False
        assert out["code"] == "NO_SUCH_PID"
        assert "进程不存在" in out["error"]["message"]

    def test_empty_driver_response_is_not_success(self):
        driver = StubDriver({"launch_app": {}})
        out = run_app(driver, action="launch", path=DEMO_PATH, execute=True)
        assert out["ok"] is False
        assert out["code"] == act_mod.EMPTY_RESPONSE_CODE

    def test_driver_error_is_normalized(self):
        driver = StubDriver(error=DriverError("驱动调用失败：list_apps", "退出码 2"))
        out = run_app(driver, action="list", execute=True)
        assert out["ok"] is False and out["code"] == "DRIVER_ERROR"
        assert out["error"]["code"] == "DRIVER_ERROR"

    def test_refusal_helper_is_shared_with_act(self):
        """驱动拒绝语义只有一份实现（act 的 `_driver_refusal`）——本模块不复制。"""
        assert app_mod._driver_refusal is act_mod._driver_refusal


# ---------------------------------------------------------------- 危险门


class TestDangerGate:
    def test_kill_without_confirm_blocked_in_dry_run(self):
        driver = StubDriver()
        out = run_app(driver, action="kill", pid=42)
        assert driver.calls == []
        assert out["ok"] is False
        assert out["code"] == "CONFIRM_REQUIRED"
        assert out["requires_confirm"] is True
        assert out["would_call"] is None
        assert out["danger"]["dangerous"] is True
        assert out["danger"]["rule"] == guard_mod.KILL_RULE
        assert out["error"]["code"] == "CONFIRM_REQUIRED"
        assert "confirm" in out["error"]["detail"].lower()

    def test_kill_without_confirm_blocked_even_when_execute(self):
        driver = StubDriver({"kill_app": {"terminated": True}})
        out = run_app(driver, action="kill", pid=42, execute=True)
        assert driver.calls == [], "未确认就下发 = 真的杀进程"
        assert out["ok"] is False and out["code"] == "CONFIRM_REQUIRED"

    def test_kill_gate_runs_before_param_validation(self):
        """先过安全门再校验参数（与 core.act 的顺序一致）：没确认就报"要确认"。"""
        out = run_app(StubDriver(), action="kill")
        assert out["code"] == "CONFIRM_REQUIRED"
        confirmed = run_app(StubDriver(), action="kill", confirm=True)
        assert confirmed["ok"] is False and confirmed["code"] == "MISSING_PARAM"

    def test_launch_of_system_binary_inherits_guard_rules(self):
        """启动系统级二进制（guard.SYSTEM_APPS 里的锁屏 / 同意界面 / 外壳）要走确认门。

        判据不是新写一份清单，而是把目标喂给 `guard.assess_action`——清单将来加名字，
        这里自动覆盖（所以本用例**从 guard 的常量驱动**穷举）。
        """
        assert guard_mod.SYSTEM_APPS, "guard 的系统应用清单不该是空的"
        for name in sorted(guard_mod.SYSTEM_APPS):
            blocked = run_app(StubDriver(), action="launch", path=f"C:/Windows/System32/{name}")
            assert blocked["ok"] is False, name
            assert blocked["code"] == "CONFIRM_REQUIRED", name
            assert blocked["danger"]["rule"] == guard_mod.SYSTEM_WINDOW_RULE, name

    def test_system_binary_launch_passes_with_confirm(self):
        driver = StubDriver({"launch_app": {"pid": 9, "windows": []}})
        out = run_app(driver, action="launch", path="C:/Windows/System32/lockapp.exe", confirm=True, execute=True)
        assert out["ok"] is True
        assert driver.calls and driver.calls[0][0] == "launch_app"

    def test_plain_binary_launch_is_not_dangerous(self):
        out = run_app(StubDriver(), action="launch", path=DEMO_PATH)
        assert out["ok"] is True
        assert "danger" not in out

    def test_non_launch_actions_are_judged_by_action_name_only(self):
        """`list` 带系统二进制路径时该报 **BAD_PARAM**（list 不接受 path），
        而不是被目标维度判成"要确认"——那是误导性结论。"""
        out = run_app(StubDriver(), action="list", path="C:/Windows/System32/lockapp.exe")
        assert out["ok"] is False
        assert out["code"] == "BAD_PARAM"
        assert "path" in out["error"]["message"]

    def test_launch_target_is_matched_by_file_name_not_full_path(self):
        """路径前缀不该影响判定（否则 `C:/x/notepad.exe` 与 `C:/y/notepad.exe` 结论不同）。"""
        for prefix in ("C:/Windows/System32/", "C:/Tools/", "shell:appsFolder\\"):
            out = run_app(StubDriver(), action="launch", path=prefix + "notepad.exe")
            assert out["ok"] is True, prefix

    def test_verdict_is_delegated_to_guard(self, monkeypatch):
        """单一出处：本模块必须**问** guard，而不是自己判一套。"""
        seen: list[tuple] = []

        def spy(action, target=None, element_index=None):
            seen.append((action, target))
            return guard_mod.RiskVerdict.safe()

        monkeypatch.setattr(app_mod.guard_mod, "assess_action", spy)
        run_app(StubDriver(), action="kill", pid=1)
        run_app(StubDriver(), action="launch", path=DEMO_PATH)
        assert seen[0] == ("kill", None)
        assert seen[1][0] == "launch"
        assert seen[1][1] == {"app": PLAIN_APP}


# ---------------------------------------------------------------- 审计


class TestAudit:
    def test_dry_run_writes_audit(self, audit_events):
        out = run_app(StubDriver(), action="launch", path=DEMO_PATH, audit=audit_events.append)
        assert out["audit_written"] is True
        assert [e["event"] for e in audit_events] == ["app.dry_run"]
        entry = audit_events[0]
        assert entry["action"] == "launch"
        assert entry["ok"] is True

    def test_blocked_writes_audit(self, audit_events):
        out = run_app(StubDriver(), action="kill", pid=42, audit=audit_events.append)
        assert out["audit_written"] is True
        entry = audit_events[0]
        assert entry["event"] == "app.blocked"
        assert entry["ok"] is False
        assert entry["code"] == "CONFIRM_REQUIRED"
        assert entry["action"] == "kill"
        assert entry["target"]["pid"] == 42
        assert entry["detail"]["danger"]["dangerous"] is True

    def test_executed_writes_audit_with_confirmed_flag(self, audit_events):
        run_app(
            StubDriver({"kill_app": {"terminated": True}}),
            action="kill", pid=42, confirm=True, execute=True, audit=audit_events.append,
        )
        entry = audit_events[0]
        assert entry["event"] == "app.executed"
        assert entry["ok"] is True
        assert entry["detail"]["confirmed"] is True
        assert entry["target"]["pid"] == 42

    def test_list_execute_writes_audit_with_counts(self, audit_events):
        run_app(StubDriver({"list_apps": raw_payload(3)}), action="list", execute=True, audit=audit_events.append)
        entry = audit_events[0]
        assert entry["event"] == "app.executed"
        assert entry["detail"]["returned"] == 3

    def test_driver_failure_writes_audit(self, audit_events):
        driver = StubDriver(error=DriverError("驱动调用失败：kill_app", "退出码 2"))
        run_app(driver, action="kill", pid=42, confirm=True, execute=True, audit=audit_events.append)
        assert audit_events[0]["event"] == "app.failed"
        assert audit_events[0]["ok"] is False

    def test_no_sink_means_no_field(self):
        out = run_app(StubDriver(), action="launch", path=DEMO_PATH)
        assert "audit_written" not in out

    def test_audit_never_carries_launch_arguments(self, audit_events):
        """命令行参数里可能有密钥/路径：审计只记条数，不抄正文。"""
        secret = "sk-" + "placeholder-not-a-real-key"
        run_app(StubDriver(), action="launch", path=DEMO_PATH, args=["--token", secret], audit=audit_events.append)
        dumped = json.dumps(audit_events, ensure_ascii=False)
        assert secret not in dumped
        assert audit_events[0]["detail"]["arg_count"] == 2

    def test_audit_uses_shared_sink_protocol(self):
        """sink 返回 False 要如实反映成 audit_written=False（审计失败不能被吞）。"""
        out = run_app(StubDriver(), action="list", audit=lambda entry: False)
        assert out["audit_written"] is False


# ---------------------------------------------------------------- 与假驱动 CLI 联调


class TestFakeDriverCli:
    """走 `tests/fake_driver.py`（与真实 CLI 同形）：验证驱动调用真的发出去了。"""

    def test_list_via_fake_driver(self):
        out = run_app(CuaDriver(command=FAKE, timeout_s=20.0), action="list", execute=True)
        assert out["ok"] is True
        assert out["apps"] == [] and out["apps_total"] == 0  # 假驱动回执里没有 apps → 不编值
        assert out["note"]

    def test_launch_via_fake_driver_does_not_invent_pid(self):
        driver = CuaDriver(command=FAKE, timeout_s=20.0)
        out = run_app(driver, action="launch", path=DEMO_PATH, execute=True)
        assert out["ok"] is True
        assert out["result"]["pid"] is None
        assert "pid" in out["note"]

    def test_kill_via_fake_driver_passes_pid_through(self):
        out = run_app(CuaDriver(command=FAKE, timeout_s=20.0), action="kill", pid=4242, confirm=True, execute=True)
        assert out["ok"] is True
        assert out["result"]["args"] == {"pid": 4242}  # 假驱动把收到的参数原样回显
