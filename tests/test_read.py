"""read 批量读值单测：一次读多元素、缺元素入 missing、字段与快照守门、上限。

全部用 StubDriver（Python 对象）——**不接触真实驱动与桌面**。
数据形状照抄 cua-driver 0.28.2 的真实返回（2026-09-27 采样）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.read import (  # noqa: E402
    DEFAULT_READ_FIELDS,
    FIELD_MAX_CHARS,
    MAX_READ_ELEMENTS,
    run_read,
)
from winuse.driver.errors import DriverError, WinUseError  # noqa: E402

TARGET = {"pid": 100, "window_id": 200, "app_name": "Demo.exe", "title": "Demo"}
SNAP = "s0000006e"


def element(index, role="Edit", label=None, value=None, enabled=True, selected=False, frame=None):
    """造一个 get_window_state 元素（字段名与驱动实测一致）。"""
    out = {
        "actions": ["set_value"],
        "depth": 2,
        "element_index": index,
        "element_token": f"{SNAP}:{index}",
        "enabled": enabled,
        "frame": frame or {"x": 10 * index, "y": 20, "w": 90, "h": 30},
        "role": role,
        "selected": selected,
    }
    if label is not None:
        out["label"] = label
    if value is not None:
        out["value"] = value
    return out


def window_state(elements):
    """造一份 get_window_state 原始返回（含必须被丢弃的截图与 markdown）。"""
    return {
        "_note": "element bounds are in native desktop coordinates",
        "app_name": "Demo.exe",
        "element_count": len(elements),
        "elements": elements,
        "elements_complete": True,
        "pid": 100,
        "returned_element_count": len(elements),
        "screenshot_height": 600,
        "screenshot_png_b64": "iVBORw0KGgoAAAANSUhEUg" + "A" * 5000,
        "screenshot_width": 800,
        "snapshot_id": SNAP,
        "total_element_count": len(elements),
        "tree_markdown": "- Document\n  - Edit '搜索'\n" * 50,
        "window_bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        "window_id": 200,
        "window_title": "Demo",
    }


DEFAULT_ELEMENTS = [
    element(0, "Document", label="Demo", value="文档正文"),
    element(1, "Edit", label="搜索", value="待办事项"),
    element(2, "Button", label="确定"),
    element(3, "Edit", label="备注", value="", selected=True),
]


class StubDriver:
    """记录调用参数的假驱动（只认 get_window_state）。"""

    def __init__(self, state=None, boom=None):
        self.state = state if state is not None else window_state(DEFAULT_ELEMENTS)
        self.boom = boom
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if self.boom is not None:
            raise self.boom
        if tool == "get_window_state":
            return self.state
        raise AssertionError(f"StubDriver 未预期的工具：{tool}")


def state_args(driver):
    """取出唯一一次 get_window_state 的参数（顺带断言只调了一次）。"""
    assert len(driver.calls) == 1, driver.calls
    assert driver.calls[0][0] == "get_window_state", driver.calls
    return driver.calls[0][1]


class TestRunRead(unittest.TestCase):
    def test_reads_multiple_elements_in_one_call(self):
        driver = StubDriver()
        out = run_read(driver, target=TARGET, elements=[1, 2, 3], snapshot_id=SNAP)
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 3)
        self.assertEqual([item["index"] for item in out["items"]], [1, 2, 3])
        self.assertTrue(all(item["found"] for item in out["items"]))
        # 默认字段：value + label
        self.assertEqual(out["items"][0]["fields"]["value"], "待办事项")
        self.assertEqual(out["items"][0]["fields"]["label"], "搜索")
        # 唯一一次驱动调用，且是纯树读取（不要截图）
        args = state_args(driver)
        self.assertIs(args["include_screenshot"], False)
        self.assertEqual(args["pid"], 100)
        self.assertEqual(args["window_id"], 200)

    def test_returns_fresh_snapshot_and_tokens(self):
        driver = StubDriver()
        out = run_read(driver, target=TARGET, elements=[1], snapshot_id=SNAP)
        self.assertEqual(out["snapshot_id"], SNAP)
        self.assertEqual(out["items"][0]["token"], f"{SNAP}:1")

    def test_requests_every_element_field_asked_for(self):
        driver = StubDriver()
        out = run_read(
            driver,
            target=TARGET,
            elements=[3],
            fields=("value", "enabled", "selected", "role", "frame", "actions", "depth"),
            snapshot_id=SNAP,
        )
        fields = out["items"][0]["fields"]
        self.assertEqual(fields["value"], "")
        self.assertIs(fields["enabled"], True)
        self.assertIs(fields["selected"], True)
        self.assertEqual(fields["role"], "Edit")
        self.assertEqual(fields["frame"], {"x": 30, "y": 20, "w": 90, "h": 30})
        self.assertEqual(fields["actions"], ["set_value"])
        self.assertEqual(fields["depth"], 2)

    def test_field_aliases_resolve_to_real_names(self):
        """`text`/`bounds`/`name` 是调用方叫法，出口一律用驱动真实字段名。"""
        driver = StubDriver()
        out = run_read(
            driver, target=TARGET, elements=[1], fields=("text", "name", "bounds"), snapshot_id=SNAP
        )
        fields = out["items"][0]["fields"]
        self.assertEqual(sorted(fields), ["frame", "label", "value"])
        self.assertEqual(fields["value"], "待办事项")
        self.assertEqual(out["requested_fields"], ["value", "label", "frame"])

    def test_missing_element_goes_to_missing_without_error(self):
        driver = StubDriver()
        out = run_read(driver, target=TARGET, elements=[1, 99], snapshot_id=SNAP)
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["found_count"], 1)
        self.assertEqual(out["missing_count"], 1)
        self.assertFalse(out["items"][1]["found"])
        self.assertEqual(out["items"][1]["index"], 99)
        self.assertEqual(out["missing"][0]["index"], 99)
        self.assertIn("reason", out["missing"][0])

    def test_field_absent_on_element_is_none_not_fabricated(self):
        driver = StubDriver()
        out = run_read(driver, target=TARGET, elements=[2], snapshot_id=SNAP)
        item = out["items"][0]
        self.assertTrue(item["found"])
        self.assertIsNone(item["fields"]["value"])  # 按钮没有 value，不编一个空串

    def test_token_addressing_needs_no_snapshot(self):
        driver = StubDriver()
        out = run_read(driver, target=TARGET, elements=[f"{SNAP}:1", {"token": f"{SNAP}:2"}])
        self.assertEqual(out["found_count"], 2)
        self.assertEqual(out["items"][0]["index"], 1)

    def test_index_without_snapshot_is_refused_and_nothing_is_sent(self):
        driver = StubDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_read(driver, target=TARGET, elements=[1])
        self.assertEqual(ctx.exception.code, "SNAPSHOT_REQUIRED")
        self.assertEqual(driver.calls, [])

    def test_token_with_snapshot_is_fine(self):
        driver = StubDriver()
        out = run_read(driver, target=TARGET, elements=[1], snapshot_id=SNAP)
        self.assertTrue(out["ok"])

    def test_unknown_field_is_refused_before_any_call(self):
        driver = StubDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_read(driver, target=TARGET, elements=[1], fields=("display_name",), snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(driver.calls, [])

    def test_empty_field_list_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            run_read(StubDriver(), target=TARGET, elements=[1], fields=(), snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_elements_must_be_present(self):
        for bad in ([], None):
            with self.assertRaises(WinUseError, msg=repr(bad)) as ctx:
                run_read(StubDriver(), target=TARGET, elements=bad, snapshot_id=SNAP)
            self.assertEqual(ctx.exception.code, "MISSING_PARAM", repr(bad))

    def test_bad_element_ref_is_refused(self):
        for bad in (3, [True], [{"x": 1}], [None]):
            with self.assertRaises(WinUseError, msg=repr(bad)) as ctx:
                run_read(StubDriver(), target=TARGET, elements=bad, snapshot_id=SNAP)
            self.assertEqual(ctx.exception.code, "INVALID_PARAMS", repr(bad))

    def test_target_without_window_is_refused(self):
        driver = StubDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_read(driver, target={}, elements=[1], snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "MISSING_TARGET")
        self.assertEqual(driver.calls, [])

    def test_over_cap_is_refused_not_clamped(self):
        """超上限**拒**（不是静默少读几个）——否则调用方会以为整批都读到了。"""
        driver = StubDriver()
        refs = list(range(MAX_READ_ELEMENTS + 1))
        with self.assertRaises(WinUseError) as ctx:
            run_read(driver, target=TARGET, elements=refs, snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "PARAM_OUT_OF_RANGE")
        self.assertEqual(driver.calls, [])

    def test_cap_itself_is_accepted(self):
        elements = [element(i, "Edit", label=f"项{i}", value=f"值{i}") for i in range(MAX_READ_ELEMENTS)]
        driver = StubDriver(window_state(elements))
        out = run_read(driver, target=TARGET, elements=list(range(MAX_READ_ELEMENTS)), snapshot_id=SNAP)
        self.assertEqual(out["count"], MAX_READ_ELEMENTS)
        self.assertEqual(out["found_count"], MAX_READ_ELEMENTS)
        self.assertEqual(out["max_elements"], MAX_READ_ELEMENTS)

    def test_unknown_option_is_refused(self):
        driver = StubDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_read(
                driver,
                target=TARGET,
                elements=[1],
                snapshot_id=SNAP,
                options={"delivery_mode": "background"},
            )
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(driver.calls, [])

    def test_options_are_passed_to_the_driver(self):
        driver = StubDriver()
        run_read(
            driver,
            target=TARGET,
            elements=[1],
            snapshot_id=SNAP,
            options={"query": "搜索", "max_depth": 6},
        )
        args = state_args(driver)
        self.assertEqual(args["query"], "搜索")
        self.assertEqual(args["max_depth"], 6)
        self.assertIs(args["include_screenshot"], False)

    def test_long_values_are_truncated_with_a_marker(self):
        elements = [element(0, "Edit", label="备注", value="长" * (FIELD_MAX_CHARS + 500))]
        driver = StubDriver(window_state(elements))
        out = run_read(driver, target=TARGET, elements=[0], snapshot_id=SNAP)
        item = out["items"][0]
        self.assertLessEqual(len(item["fields"]["value"]), FIELD_MAX_CHARS + 1)
        self.assertEqual(item["truncated_fields"], ["value"])
        self.assertEqual(out["field_max_chars"], FIELD_MAX_CHARS)

    def test_no_truncation_marker_when_short(self):
        driver = StubDriver()
        out = run_read(driver, target=TARGET, elements=[1], snapshot_id=SNAP)
        self.assertNotIn("truncated_fields", out["items"][0])

    def test_output_carries_no_screenshot_or_markdown(self):
        driver = StubDriver()
        blob = repr(run_read(driver, target=TARGET, elements=[0, 1], snapshot_id=SNAP))
        self.assertNotIn("iVBORw0KGgo", blob)
        self.assertNotIn("tree_markdown", blob)

    def test_snapshot_refresh_is_documented_in_the_reply(self):
        """读一次会刷新驱动快照——回执必须说明"回给你的令牌才是新的"。"""
        out = run_read(StubDriver(), target=TARGET, elements=[1], snapshot_id=SNAP)
        self.assertIn("snapshot", out["note"])

    def test_driver_failure_is_returned_as_ok_false(self):
        driver = StubDriver(boom=DriverError("驱动挂了", "模拟"))
        out = run_read(driver, target=TARGET, elements=[1], snapshot_id=SNAP)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "DRIVER_ERROR")

    def test_audit_records_one_event_without_element_contents(self):
        entries: list[dict] = []
        # sink 协议是「可调用对象 entry -> Any」（见 audit.sink_event），不是容器
        run_read(StubDriver(), target=TARGET, elements=[1], snapshot_id=SNAP, audit=entries.append)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["event"], "read.executed")
        self.assertIs(entry["ok"], True)
        # 审计里**不许**出现元素内容（读到的值可能是密码/草稿——样例即泄露）
        self.assertNotIn("待办事项", str(entry))
        self.assertEqual(entry["detail"]["found"], 1)

    def test_no_audit_sink_leaves_no_such_field(self):
        out = run_read(StubDriver(), target=TARGET, elements=[1], snapshot_id=SNAP)
        self.assertNotIn("audit_written", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
