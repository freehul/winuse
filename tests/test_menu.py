"""winuse_menu（应用菜单路径调用）单测。

覆盖四层：

1. **纯逻辑**（`build_menu_call`）：菜单路径 → 驱动调用。路径校验穷举断言
   （空路径 / 空段 / 超 16 段 / 单段超 200 字 / 类型不对），不碰驱动。
2. **门禁**：`invoke_menu` 在驱动侧就是 `read_only=false, destructive=true`
   ——它按路径**真的调用应用菜单的最终项**（新建/保存/退出都在里面），
   因此未确认时**连干跑都不给调用计划**（与 `core.act` / `core.desktop` 的
   门禁闸位一致：闸门在参数校验与干跑分支之前）。
3. **干跑与真跑**：确认后默认干跑（一个字节都不下发），`execute=True` 才真发；
   真跑分支全部走桩驱动，**没有任何一次真实菜单调用**。
4. **审计与失败归一**：每次尝试落一条（blocked / dry_run / executed）；
   驱动抛错 / 回空 / 回 `ok=false` 一律回收成 `ok=false` + 结构化 error，
   不把"没报错"当"调用了"。

铁律：**单测不碰真实桌面**（桩驱动 + 桩 sink），也不写真名实姓的本机路径。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core import act as act_mod  # noqa: E402
from winuse.core import guard as guard_mod  # noqa: E402
from winuse.core.menu import (  # noqa: E402
    ACTION,
    MAX_ITEM_CHARS,
    MAX_PATH_ITEMS,
    TOOL,
    build_menu_call,
    run_menu,
)
from winuse.driver.cua import CuaDriver  # noqa: E402
from winuse.driver.errors import WinUseError  # noqa: E402

#: `tests/fake_driver.py` 当真实驱动用（与 `test_cua_driver.py` 同口径）
FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_driver.py")]

TARGET = {"pid": 100, "window_id": 200, "app_name": "Demo.exe", "title": "Demo"}
PATH = ["文件", "最近使用的文件", "a.txt"]


class StubDriver:
    """按脚本返回的桩驱动：记录每一次调用，可选抛错。"""

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
# 纯逻辑：路径校验与组参
# --------------------------------------------------------------------------


class TestBuildMenuCall:
    def test_path_becomes_driver_call(self):
        tool, args = build_menu_call(TARGET, PATH)
        assert tool == TOOL == "invoke_menu"
        assert args == {"pid": 100, "window_id": 200, "path": PATH}

    def test_segments_are_trimmed(self):
        """驱动只在匹配前 trim；我们在下发前规范化，让回执里的路径就是实际匹配的标签。"""
        tool, args = build_menu_call(TARGET, ["  文件 ", "\t编辑\n"])
        assert args["path"] == ["文件", "编辑"]

    def test_missing_target_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            build_menu_call({}, PATH)
        assert ctx.value.code == "MISSING_TARGET"
        with pytest.raises(WinUseError) as ctx:
            build_menu_call({"pid": 100}, PATH)
        assert ctx.value.code == "MISSING_TARGET"

    def test_empty_path_is_refused(self):
        for bad in ([], (), None, ""):
            with pytest.raises(WinUseError) as ctx:
                build_menu_call(TARGET, bad)
            assert ctx.value.code == "MISSING_PARAM", bad

    def test_blank_segment_is_refused(self):
        """空白段永远匹配不到任何菜单项——当成参数错误当场拒，别浪费一次驱动往返。"""
        for bad in (["文件", "   "], ["文件", ""], [" ", "文件"]):
            with pytest.raises(WinUseError) as ctx:
                build_menu_call(TARGET, bad)
            assert ctx.value.code == "MISSING_PARAM", bad

    def test_non_string_segment_is_refused(self):
        with pytest.raises(WinUseError) as ctx:
            build_menu_call(TARGET, ["文件", 3])
        assert ctx.value.code == "MISSING_PARAM"

    def test_path_too_many_items_is_out_of_range(self):
        with pytest.raises(WinUseError) as ctx:
            build_menu_call(TARGET, ["项"] * (MAX_PATH_ITEMS + 1))
        assert ctx.value.code == "PARAM_OUT_OF_RANGE"
        # 上界本身合法（驱动 schema: maxItems 16）
        assert build_menu_call(TARGET, ["项"] * MAX_PATH_ITEMS)[1]["path"] == ["项"] * MAX_PATH_ITEMS

    def test_item_too_long_is_out_of_range(self):
        with pytest.raises(WinUseError) as ctx:
            build_menu_call(TARGET, ["x" * (MAX_ITEM_CHARS + 1)])
        assert ctx.value.code == "PARAM_OUT_OF_RANGE"
        build_menu_call(TARGET, ["x" * MAX_ITEM_CHARS])

    def test_single_item_path_is_allowed(self):
        """顶层单段（如「帮助」）也是合法路径——驱动 schema 允许 1 段。"""
        assert build_menu_call(TARGET, ["帮助"])[1]["path"] == ["帮助"]


# --------------------------------------------------------------------------
# 门禁：未确认不下发（连干跑都不给调用计划）
# --------------------------------------------------------------------------


class TestGate:
    def test_dry_run_without_confirm_is_blocked(self):
        driver = StubDriver()
        out = run_menu(driver, target=TARGET, path=PATH)
        assert out["ok"] is False
        assert out["dry_run"] is True
        assert out["requires_confirm"] is True
        assert out["code"] == guard_mod.CONFIRM_CODE == "CONFIRM_REQUIRED"
        assert out["would_call"] is None
        assert out["danger"]["dangerous"] is True
        assert out["danger"]["rule"] == "menu_invoke"
        assert driver.calls == []

    def test_execute_without_confirm_is_blocked(self):
        driver = StubDriver()
        out = run_menu(driver, target=TARGET, path=PATH, execute=True)
        assert out["ok"] is False
        assert out["dry_run"] is False
        assert out["requires_confirm"] is True
        assert driver.calls == []

    def test_blocked_receipt_shape(self):
        """被拦下的回执形状与 `core.desktop._blocked` 同形（同一套字段语义）。"""
        out = run_menu(StubDriver(), target=TARGET, path=PATH, audit=None)
        assert set(out) == {
            "ok",
            "dry_run",
            "action",
            "target",
            "would_call",
            "code",
            "requires_confirm",
            "danger",
            "error",
        }
        assert out["action"] == ACTION
        assert out["target"] == {"pid": 100, "window_id": 200}
        assert out["error"]["code"] == guard_mod.CONFIRM_CODE
        assert "menu_invoke" in out["error"]["detail"]

    def test_blocked_receipt_never_carries_the_menu_path_body(self):
        """回执里不留菜单路径正文——菜单标签常就等于用户内容（文件名/最近打开），
        与 act 的写值门禁「只回长度不回正文」同一数据卫生口径。"""
        out = run_menu(StubDriver(), target=TARGET, path=PATH, execute=True)
        blob = json.dumps(out, ensure_ascii=False)
        for segment in PATH:
            assert segment not in blob
        assert "3 段" in blob  # 段数可以留（排查够用，不泄露标签）

    def test_dry_run_with_confirm_gives_call_plan_without_dispatch(self):
        driver = StubDriver()
        out = run_menu(driver, target=TARGET, path=PATH, confirm=True)
        assert out["ok"] is True
        assert out["dry_run"] is True
        assert out["would_call"]["tool"] == TOOL
        assert out["would_call"]["args"]["path"] == PATH
        assert "干跑" in out["would_call"]["note"]
        assert driver.calls == []

    def test_execute_with_confirm_dispatches_once(self):
        driver = StubDriver({"invoked": True})
        out = run_menu(driver, target=TARGET, path=PATH, confirm=True, execute=True)
        assert len(driver.calls) == 1
        assert driver.calls[0] == (TOOL, {"pid": 100, "window_id": 200, "path": PATH})
        assert out["ok"] is True
        assert out["dry_run"] is False
        assert out["result"] == {"invoked": True}
        assert "requires_confirm" not in out

    def test_gate_runs_before_path_validation(self):
        """闸门在参数校验之前：畸形路径不会盖掉「被拦」这件事（与 desktop 同口径）。"""
        out = run_menu(StubDriver(), target=TARGET, path=[])
        assert out["ok"] is False
        assert out["code"] == guard_mod.CONFIRM_CODE


# --------------------------------------------------------------------------
# 审计与失败归一
# --------------------------------------------------------------------------


class TestAuditAndFailure:
    def test_audit_events_for_each_outcome(self):
        sink = Sink()
        run_menu(StubDriver(), target=TARGET, path=PATH, audit=sink)
        assert [e["event"] for e in sink.entries] == ["menu.blocked"]
        assert sink.entries[-1]["ok"] is False
        assert sink.entries[-1]["code"] == guard_mod.CONFIRM_CODE

        sink = Sink()
        run_menu(StubDriver(), target=TARGET, path=PATH, confirm=True, audit=sink)
        assert [e["event"] for e in sink.entries] == ["menu.dry_run"]

        sink = Sink()
        run_menu(StubDriver(), target=TARGET, path=PATH, confirm=True, execute=True, audit=sink)
        assert [e["event"] for e in sink.entries] == ["menu.executed"]
        assert sink.entries[-1]["ok"] is True

    def test_audit_written_flag_is_exposed(self):
        """审计是否真落盘要能看见（与 act 的 `audit_written` 同协议）。"""

        def failing_sink(entry):
            return False

        out = run_menu(StubDriver(), target=TARGET, path=PATH, audit=failing_sink)
        assert out["audit_written"] is False

    def test_no_sink_means_no_audit_field(self):
        out = run_menu(StubDriver(), target=TARGET, path=PATH, audit=None)
        assert "audit_written" not in out

    def test_driver_error_is_reported_not_raised(self):
        driver = StubDriver(boom=WinUseError("驱动炸了", "细节"))
        out = run_menu(driver, target=TARGET, path=PATH, confirm=True, execute=True)
        assert out["ok"] is False
        assert out["error"]["code"] == "WINUSE_ERROR"
        assert out["code"] == "WINUSE_ERROR"

    def test_driver_refusal_semantics_are_shared_with_act(self):
        """`ok=false` 的回执不是成功——复用 act 的拒绝归一（单一实现，不复制）。"""
        driver = StubDriver({"ok": False, "code": "menu_path_not_found", "message": "找不到该菜单路径"})
        out = run_menu(driver, target=TARGET, path=PATH, confirm=True, execute=True)
        assert out["ok"] is False
        assert out["error"]["code"] == "MENU_PATH_NOT_FOUND"
        assert "找不到该菜单路径" in out["error"]["message"]

    def test_empty_driver_response_is_failure(self):
        """空回执按失败处理（act.EMPTY_RESPONSE_CODE 同码）。"""
        driver = StubDriver({})
        out = run_menu(driver, target=TARGET, path=PATH, confirm=True, execute=True)
        assert out["ok"] is False
        assert out["error"]["code"] == act_mod.EMPTY_RESPONSE_CODE


# --------------------------------------------------------------------------
# 过一遍真实驱动边界（tests/fake_driver.py，与真实 CLI 同形）
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# T-61：路径没解析上时，用窗口里**真实的**菜单文本重试一次，并把候选回给调用方
#
# 驱动的 invoke_menu 语义是「exact path, resolved one live native level at a
# time」——精确匹配是它的设计，不是 bug；而 Win32 菜单标签带加速键后缀
# （"帮助(H)"），调用方凭直觉写"帮助"必然匹配不上。所以宽松匹配只能在核心做。
# --------------------------------------------------------------------------

from winuse.core import menu as menu_mod  # noqa: E402  （紧邻本节测试，便于对照）

TARGET2 = {"pid": 100, "window_id": 200, "app_name": "Demo.exe", "title": "Demo"}


class ScriptedDriver:
    """按调用序列返回不同结果的桩驱动（用来演练「先被拒、再重试」这条路径）。"""

    def __init__(self, results):
        self.calls: list[tuple[str, dict]] = []
        self._results = list(results)

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if not self._results:
            return {"ok": True}
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _refusal():
    return {
        "status": "refused",
        "refusal": {"code": "menu_path_unavailable", "message": "menu path segment 0 was not found"},
    }


def _state_with_menus(*labels):
    # 真实回执的元素必带 token（驱动字段名就是 token），解析要靠它定位菜单项，
    # 所以假数据也必须给——漏了它这条链路就永远定位不到元素。
    return {
        "ok": True,
        "elements": [
            {"index": i, "element_token": f"t{i}", "role": "MenuItem", "label": label, "actions": ["expand"]}
            for i, label in enumerate(labels)
        ],
    }


class TestLooseLabelMatching:
    def test_accelerator_suffix_is_stripped(self):
        assert menu_mod._strip_accelerator("帮助(H)") == "帮助"
        assert menu_mod._strip_accelerator("收藏夹(O)") == "收藏夹"
        assert menu_mod._strip_accelerator(" 文件(F) ") == "文件"
        # 括号里是词组时那不是加速键，剥掉会得到一个窗口里根本不存在的名字
        assert menu_mod._strip_accelerator("导出(CSV)") == "导出(CSV)"
        assert menu_mod._strip_accelerator("Save As...") == "Save As..."

    def test_exact_wins_then_looser_forms(self):
        actuals = ["文件(F)", "帮助(H)"]
        assert menu_mod._match_menu_label("帮助(H)", actuals) == "帮助(H)"  # 整串相等最优先
        assert menu_mod._match_menu_label(" 帮助 ", actuals) == "帮助(H)"  # 忽略空格
        assert menu_mod._match_menu_label("帮助", actuals) == "帮助(H)"  # 忽略加速键后缀
        assert menu_mod._match_menu_label("编辑", actuals) is None  # 绝不猜相近的

    def test_latin_labels_match_case_insensitively(self):
        actuals = ["&File", "Save As..."]
        assert menu_mod._match_menu_label("save as...", actuals) == "Save As..."


class TestRetryWithActualLabels:
    def test_retries_with_actual_label_and_reports_it(self):
        driver = ScriptedDriver(
            [
                _refusal(),  # 首次：原样下发 → 被拒
                _state_with_menus("文件(F)", "帮助(H)"),  # 取一次状态拿候选
                {"ok": True, "status": "invoked"},  # 重试：成功
            ]
        )
        out = menu_mod.run_menu(driver, target=TARGET2, path=["帮助"], execute=True, confirm=True)
        assert out["ok"] is True
        assert driver.calls[0][1]["path"] == ["帮助"]  # 首次原样下发，先相信调用方
        assert driver.calls[1][0] == "get_window_state"  # 被拒后才取样
        assert driver.calls[2][1]["path"] == ["帮助(H)"]  # 重试用窗口里的真实文本
        assert out["menu_path_resolved"] == {"from": ["帮助"], "to": ["帮助(H)"]}

    def test_reports_candidates_when_nothing_matches(self):
        driver = ScriptedDriver([_refusal(), _state_with_menus("文件(F)", "帮助(H)")])
        out = menu_mod.run_menu(driver, target=TARGET2, path=["编辑"], execute=True, confirm=True)
        assert out["ok"] is False
        assert out["menu_candidates"] == ["文件(F)", "帮助(H)"]
        assert "menu_path_resolved" not in out

    def test_no_extra_calls_when_the_path_resolves(self):
        driver = ScriptedDriver([{"ok": True, "status": "invoked"}])
        out = menu_mod.run_menu(driver, target=TARGET2, path=["帮助(H)"], execute=True, confirm=True)
        assert out["ok"] is True
        assert len(driver.calls) == 1  # 正常路径零额外开销

    def test_dry_run_never_samples_state(self):
        driver = ScriptedDriver([{"ok": True}])
        out = menu_mod.run_menu(driver, target=TARGET2, path=["帮助"], execute=False, confirm=True)
        assert out["dry_run"] is True
        assert driver.calls == []


class TestDeepPathResolution:
    """T-62：深层也自动纠正——逐层展开，只认那一层**新出现**的文本。

    为什么必须逐层：经典 Win32 菜单（WinRAR 那类）的树里只有顶层项，子项要那一层
    真被展开才存在；而展开后新出现的项排在元素列表前部，所以用「与上一层的差集」
    就能认出这一层的子项，而不会把别的层重名项错认过来（认错=点错功能）。
    """

    def test_first_segment_maps_and_second_uses_expanded_children(self):
        driver = ScriptedDriver(
            [
                _state_with_menus("文件(F)", "帮助(H)"),  # 取样①：顶层
                {"ok": True, "status": "ok"},  # 展开「帮助(H)」
                # 取样②：展开后的子项（排在前）+ 顶层仍在
                _state_with_menus("帮助主题(H)", "关于 WinRAR(A)...", "系统", "文件(F)", "帮助(H)"),
            ]
        )
        resolved = menu_mod._resolve_path_by_expanding(driver, TARGET2, ["帮助", "关于"])
        assert resolved == ["帮助(H)", "关于 WinRAR(A)..."]
        assert driver.calls[1][0] == "click"  # 展开走 act 通道（驱动那边叫 click）
        assert driver.calls[1][1].get("element_token")  # 用令牌定位，不用易过期的索引

    def test_stops_when_a_layer_has_no_match(self):
        driver = ScriptedDriver([_state_with_menus("文件(F)", "帮助(H)")])
        assert menu_mod._resolve_path_by_expanding(driver, TARGET2, ["编辑", "复制"]) is None

    def test_never_guesses_when_two_children_share_the_name(self):
        """歧义那一段**原样不动**（其余段照纠）——两个「关于」无法区分，猜错就是点到别的功能。"""
        driver = ScriptedDriver(
            [
                _state_with_menus("文件(F)", "帮助(H)"),
                {"ok": True, "status": "ok"},
                _state_with_menus("关于(A)", "关于(B)", "帮助(H)"),  # 两个都叫「关于」
            ]
        )
        # 第 1 段安全地纠成「帮助(H)」，第 2 段保持调用方原样交给驱动去判断。
        assert menu_mod._resolve_path_by_expanding(driver, TARGET2, ["帮助", "关于"]) == ["帮助(H)", "关于"]

    def test_single_segment_path_needs_no_expansion(self):
        driver = ScriptedDriver([_state_with_menus("文件(F)", "帮助(H)")])
        assert menu_mod._resolve_path_by_expanding(driver, TARGET2, ["帮助"]) == ["帮助(H)"]
        assert len(driver.calls) == 1  # 只有最后一段时才不展开；单段路径零展开


class TestViaFakeDriver:
    def test_execute_through_fake_driver_cli(self):
        driver = CuaDriver(command=FAKE)
        out = run_menu(driver, target=TARGET, path=PATH, confirm=True, execute=True)
        assert out["ok"] is True
        assert out["result"]["tool"] == TOOL
        assert out["result"]["args"]["path"] == PATH
        assert out["result"]["args"]["pid"] == 100

    def test_dry_run_never_spawns_driver(self):
        """干跑路径必须一个字节都不下发——用假驱动证伪「干跑也会调驱动」。"""
        driver = CuaDriver(command=FAKE)
        out = run_menu(driver, target=TARGET, path=PATH, confirm=True)
        assert out["dry_run"] is True
        assert "result" not in out
