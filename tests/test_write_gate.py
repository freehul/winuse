"""写值门禁（EXISTING_CONTENT）：`set_value` 不许静默整段替换既有内容。

回归来源：记事本事故——对非空编辑框 set_value 把用户草稿整段替换。
当年那道闸只写在验收脚本里，产品路径裸奔；本文件把产品路径的行为钉死。

桩驱动：只关心"写值前核心自己去读了一次状态"，不下发任何真实调用。
"""

from __future__ import annotations

import json

import pytest

from winuse.core.act import (
    EXISTING_CONTENT_CODE,
    UNVERIFIED_CODE,
    GATE_EMPTY,
    GATE_OVERWRITE,
    GATE_UNVERIFIED,
    GATE_UNVERIFIED_BYPASS,
    ActSpec,
    check_write_gate,
    run_act,
)

TARGET = {"pid": 4242, "window_id": "win-1"}
DRAFT = "用户草稿-不可静默覆盖"


class StubDriver:
    """记录每次调用；get_window_state 返回给定元素。"""

    def __init__(self, elements=None, state_error: Exception | None = None):
        self.elements = elements if elements is not None else []
        self.state_error = state_error
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if tool == "get_window_state":
            if self.state_error is not None:
                raise self.state_error
            return {"elements": self.elements}
        return {"ok": True, "tool": tool}

    @property
    def written(self) -> bool:
        """是否下发了实质动作（读状态 / 列窗口这类只读调用不算）。"""
        return any(tool not in {"get_window_state", "list_windows"} for tool, _ in self.calls)


def _elements(value: str, token: str = "t1", index: int = 0) -> list[dict]:
    return [{"token": token, "index": index, "value": value, "label": value[:40]}]


def test_dry_run_does_not_even_read_state():
    """干跑不下发 → 门禁连读都不读（省一次驱动往返）。"""
    driver = StubDriver(_elements(DRAFT))
    out = run_act(driver, ActSpec(action="set_value", element_token="t1", value="x"), TARGET, None)
    assert out["ok"] is True
    assert driver.calls == []


def test_non_empty_target_is_blocked_and_nothing_is_sent():
    driver = StubDriver(_elements(DRAFT))
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == EXISTING_CONTENT_CODE
    assert out["requires_confirm"] is True
    assert out["write_gate"]["status"] == "blocked"
    assert out["write_gate"]["existing_chars"] == len(DRAFT)
    assert driver.written is False, "被拦时不得下发任何写值动作"


def test_blocked_result_carries_no_body_text():
    """被拦回执里只给长度，不回内容也不回标签（标签在真实场景常常就是内容）。"""
    driver = StubDriver(_elements(DRAFT))
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    gate = out["write_gate"]
    assert set(gate) == {"status", "blocked", "existing_chars"}
    assert DRAFT not in json.dumps(out, ensure_ascii=False)


def test_overwrite_explicitly_authorizes_replacement():
    driver = StubDriver(_elements(DRAFT))
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
        overwrite=True,
    )
    assert out["ok"] is True
    assert out["write_gate"] == GATE_OVERWRITE
    assert driver.written is True


def test_empty_target_is_allowed():
    driver = StubDriver(_elements(""))
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is True
    assert out["write_gate"] == GATE_EMPTY
    assert driver.written is True


def test_whitespace_only_target_counts_as_empty():
    driver = StubDriver(_elements("   \n\t "))
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is True
    assert out["write_gate"] == GATE_EMPTY


def test_element_missing_from_tree_is_refused():
    """R-3：读不到目标元素 → **拒绝写入**（fail-closed），不再"标记后照写"。

    原先放行的理由是会误伤正常写值；但"确认不了"时写入等于赌一把——目标可能是
    用户草稿所在的控件。要写特殊控件请显式带 allow_unverified（见下）。
    """
    driver = StubDriver([])
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == UNVERIFIED_CODE
    assert out["write_gate"]["status"] == GATE_UNVERIFIED
    assert driver.written is False, "拿不准时绝不能下发写入"


def test_state_read_failure_is_refused():
    driver = StubDriver(state_error=RuntimeError("驱动挂了"))
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == UNVERIFIED_CODE
    assert driver.written is False


def test_allow_unverified_requires_overwrite_too():
    """高级授权是**两把钥匙**：allow_unverified 单独给不够，必须同时 overwrite。"""
    driver = StubDriver([])
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
        allow_unverified=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == UNVERIFIED_CODE
    assert driver.written is False


def test_allow_unverified_with_overwrite_goes_through():
    driver = StubDriver([])
    entries: list[dict] = []
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
        allow_unverified=True,
        overwrite=True,
        audit=entries.append,
    )
    assert out["ok"] is True
    # 放行路径的 write_gate 与 GATE_EMPTY 同口径：是状态字符串；被拦时才是完整 dict
    assert out["write_gate"] == GATE_UNVERIFIED_BYPASS, "双授权路径根本没查门禁，回执要如实说"
    assert out["write_gate_override"] == "allow_unverified+overwrite"
    assert driver.written is True, "显式双授权后必须能写（不是谁都写不了）"
    assert any("unverified" in str(e.get("detail", "")) for e in entries), "双授权写入要留痕"


def test_index_fallback_matches_when_token_is_absent():
    driver = StubDriver([{"index": 3, "value": DRAFT, "label": "框"}])
    out = run_act(
        driver,
        ActSpec(action="set_value", element_index=3, value="新值"),
        TARGET,
        "snap-1",  # 按索引动作必须带同源快照（PR-3 的口径）
        execute=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == EXISTING_CONTENT_CODE


def test_type_is_not_gated():
    """type 是输入/追加语义，不是整段替换 → 不拦。"""
    driver = StubDriver(_elements(DRAFT))
    out = run_act(
        driver,
        ActSpec(action="type", element_token="t1", text="追加"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is True
    assert "write_gate" not in out
    assert driver.written is True


def test_audit_entry_records_block_without_draft_text():
    entries: list[dict] = []
    driver = StubDriver(_elements(DRAFT))
    run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
        audit=entries.append,
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["event"] == "act.blocked"
    assert entry["code"] == EXISTING_CONTENT_CODE
    assert DRAFT not in json.dumps(entry, ensure_ascii=False), "审计里不得出现草稿正文"


def test_check_write_gate_three_states():
    """门禁函数本身的三态（供其它调用方复用）。"""
    driver = StubDriver(_elements(DRAFT))
    spec = ActSpec(action="set_value", element_token="t1", value="x")
    assert check_write_gate(driver, spec, TARGET)["status"] == "blocked"

    driver = StubDriver(_elements(""))
    assert check_write_gate(driver, spec, TARGET)["status"] == GATE_EMPTY

    driver = StubDriver([])
    assert check_write_gate(driver, spec, TARGET)["status"] == GATE_UNVERIFIED


@pytest.mark.parametrize("action", ["set_value"])
def test_write_actions_constant_covers_set_value(action):
    from winuse.core.act import WRITE_ACTIONS

    assert action in WRITE_ACTIONS


def test_cli_act_exposes_overwrite_flag():
    """接线检查：CLI 真的接上了 --overwrite（不许核心有门禁、门面没开关）。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "winuse", "act", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "--overwrite" in proc.stdout
    assert "既有内容" in proc.stdout


def test_tool_catalog_exposes_overwrite_property():
    """接线检查：MCP 工具表（唯一出处）里 winuse_act 带 overwrite 且说明后果。"""
    from winuse.mcp.server import tool_catalog

    tools = {t["name"]: t for t in tool_catalog()}
    props = tools["winuse_act"]["inputSchema"]["properties"]
    assert "overwrite" in props
    assert "EXISTING_CONTENT" in props["overwrite"]["description"]


def test_stale_token_is_rewired_to_the_fresh_snapshot():
    """门禁的读会刷新驱动快照 → 写值必须改用刚读到的令牌（否则被驱动拒 stale_element_token）。"""
    from winuse.core.act import _token_index

    # 调用方手上是旧令牌 t-old:0，门禁这次读回来的是 t-new:0（同序号、新前缀）
    driver = StubDriver([{"token": "t-new:0", "index": 0, "value": "", "label": "空框"}])
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t-old:0", value="新值"),
        TARGET,
        "old-snap",
        execute=True,
    )
    assert _token_index("t-old:0") == 0
    assert out["ok"] is True
    assert out["write_gate"] == GATE_EMPTY
    assert out["element_token"] == "t-new:0"
    assert out["snapshot_id"] is None
    write = [(tool, args) for tool, args in driver.calls if tool != "get_window_state"][0]
    assert write[1]["element_token"] == "t-new:0", "写值调用必须切到新令牌"
    assert "snapshot_id" not in write[1] or write[1]["snapshot_id"] is None


def test_same_token_means_no_rewire():
    """令牌没变（快照没被换）时不要多此一举改调用。"""
    driver = StubDriver(_elements("", token="t1"))
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        "snap-1",
        execute=True,
    )
    assert "element_token" not in out
    assert out["snapshot_id"] == "snap-1"


def test_driver_refusal_turns_ok_false():
    """驱动"不抛异常但拒绝"必须反映在 ok 上（否则 agent 以为写成功了）。"""

    class RefusingDriver(StubDriver):
        def call(self, tool, args):
            self.calls.append((tool, args))
            if tool == "get_window_state":
                return {"elements": _elements("")}
            return {
                "status": "refused",
                "refusal": {"code": "stale_element_token", "message": "element_token is stale"},
            }

    driver = RefusingDriver()
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "STALE_ELEMENT_TOKEN"
    assert "stale" in out["error"]["message"]


def test_normal_driver_body_keeps_ok_true():
    driver = StubDriver(_elements(""))
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="t1", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is True
    assert "error" not in out


def test_raw_driver_element_keys_are_understood():
    """回归：驱动原始回执用 `element_token`/`element_index`（只有 core.state 才投影成短名）。

    门禁读的是**驱动原始回执**，按短名找会永远找不到 → 门禁静默失效（真机踩过，
    表现为 write_gate=unverified、写值被驱动以 stale_element_token 拒）。
    """
    driver = StubDriver(
        [{"element_token": "s00000043:0", "element_index": 0, "value": DRAFT, "role": "Edit"}]
    )
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="s00000043:0", value="新值"),
        TARGET,
        None,
        execute=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == EXISTING_CONTENT_CODE


def test_raw_shape_stale_token_points_to_fresh_token():
    """原始字段名下也要能把写值切到新令牌。"""
    driver = StubDriver([{"element_token": "s00000043:0", "element_index": 0, "value": ""}])
    out = run_act(
        driver,
        ActSpec(action="set_value", element_token="s00000001:0", value="新值"),
        TARGET,
        "old-snap",
        execute=True,
    )
    assert out["write_gate"] == GATE_EMPTY
    assert out["element_token"] == "s00000043:0"
