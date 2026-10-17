"""R-15：驱动回执的失败形态必须都归一成核心错误码。

原先只认 `status="refused"` 与 `refusal` 字段——驱动"正常 JSON 回执但 `ok=false`"
（以及空回执）会被当成成功，agent 以为动作做了，实际一个字节都没变。

本文件钉住三种失败形态 + 两种成功形态，并确认错误码稳定可判。
"""

from __future__ import annotations

import pytest

from winuse.core.act import ActSpec, run_act

TARGET = {"pid": 4242, "window_id": 2001, "app_name": "demo.exe", "title": "占位窗口"}


class ResultDriver:
    """回什么由用例决定；记录调用。"""

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, args: dict | None = None):
        self.calls.append((tool, dict(args or {})))
        return self.result


def _click(execute=True, driver=None):
    driver = driver or ResultDriver({"ok": True})
    out = run_act(
        driver,
        ActSpec(action="click", element_index=0),
        TARGET,
        "s00000001",
        execute=execute,
    )
    return out, driver


@pytest.mark.parametrize(
    "result,expect_code",
    [
        ({"ok": False, "code": "DRIVER_REFUSED"}, "DRIVER_REFUSED"),
        ({"ok": False}, "DRIVER_REFUSED"),
        ({"ok": False, "code": "stale_element_token"}, "STALE_ELEMENT_TOKEN"),
        ({"status": "refused", "refusal": {"code": "nope", "message": "驱动拒绝"}}, "NOPE"),
        ({}, "DRIVER_EMPTY_RESPONSE"),
        (None, "DRIVER_EMPTY_RESPONSE"),
    ],
)
def test_failure_shapes_become_core_errors(result, expect_code):
    out, _ = _click(driver=ResultDriver(result))
    assert out["ok"] is False, f"驱动回执 {result!r} 不该被当成成功"
    assert out["error"]["code"] == expect_code


def test_success_shape_stays_success():
    out, _ = _click(driver=ResultDriver({"ok": True, "clicked": True}))
    assert out["ok"] is True
    assert "error" not in out


def test_result_without_ok_field_is_still_accepted():
    """驱动有的工具回执不带 ok（只回业务字段）——不能一律判失败。"""
    out, _ = _click(driver=ResultDriver({"clicked": True}))
    assert out["ok"] is True


def test_dry_run_is_unaffected_by_driver_result():
    """干跑不下发，回执里根本没有驱动结果，不该因此判失败。"""
    out, _ = _click(execute=False, driver=ResultDriver({"ok": False}))
    assert out["ok"] is True
    assert out["dry_run"] is True
