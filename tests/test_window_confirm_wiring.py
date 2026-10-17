"""接线：窗口动作的确认必须**真的**透传到核心（T-23 遗留修复）。

背景：两个门面原先只在回执里写 `confirmed: true`，却没把确认传进核心 →
系统级窗口经门面**连显式确认都被拒**，而且回执与事实不符（写着"已确认"）。
口径对齐：危险 + 显式确认 → 放行，未确认 → 拦且不下发（与 core.act 一致）。
"""

from __future__ import annotations

from winuse import cli
from winuse import cli_capabilities as CC
from winuse.mcp import server as S
import winuse.core.desktop as D


def _capture(monkeypatch) -> dict:
    """拦下核心入口，记录门面到底传了什么。"""
    seen: dict = {}

    def fake(driver, spec, *, execute=False, native=None, confirmed=False, audit=None):
        seen.update(action=spec.action, execute=execute, confirmed=confirmed, audit=audit)
        return {"ok": True, "dry_run": not execute, "action": spec.action}

    monkeypatch.setattr(D, "window_action", fake)
    return seen


class _WindowsDriver:
    """极简假驱动：只回一个系统级窗口。"""

    def __init__(self, app: str = "explorer.exe", title: str = "Program Manager"):
        self.app = app
        self.title = title
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, payload: dict | None = None):
        self.calls.append((tool, payload or {}))
        if tool == "list_windows":
            return {
                "windows": [
                    {
                        "pid": 4242,
                        "window_id": 99,
                        "app_name": self.app,
                        "title": self.title,
                        "minimized": False,
                        "on_screen": True,
                        "bounds": {"x": 0, "y": 0, "width": 100, "height": 40},
                    }
                ]
            }
        return {}


def test_cli_window_parser_exposes_confirm():
    args = cli.build_parser().parse_args(
        ["window", "--action", "minimize", "--app", "explorer", "--confirm"]
    )
    assert args.confirm is True
    assert args.action == "minimize"


def test_cli_window_passes_confirm_to_core(monkeypatch):
    seen = _capture(monkeypatch)
    args = cli.build_parser().parse_args(
        ["window", "--action", "minimize", "--app", "explorer", "--confirm"]
    )
    assert CC.cmd_window(args) == CC.EXIT_OK
    assert seen["confirmed"] is True, "CLI 必须把 --confirm 透传进核心"


def test_cli_window_without_confirm_does_not_reach_core(monkeypatch):
    """关闭类动作名在门面就被拦，根本不下发到核心。"""
    seen = _capture(monkeypatch)
    args = cli.build_parser().parse_args(["window", "--action", "close", "--app", "notepad"])
    assert CC.cmd_window(args) == CC.EXIT_FAIL
    assert seen == {}, "未确认时不得下发到核心"


def test_mcp_window_passes_confirm_to_core(monkeypatch):
    seen = _capture(monkeypatch)
    session = S.Session(driver=_WindowsDriver(), initialized=True)
    out = S._tool_window(session, {"action": "minimize", "app": "explorer", "confirm": True})
    assert seen["confirmed"] is True, "MCP 必须把 confirm 透传进核心"
    assert out["confirmed"] is True


def test_mcp_window_without_confirm_reports_false(monkeypatch):
    seen = _capture(monkeypatch)
    session = S.Session(driver=_WindowsDriver(), initialized=True)
    out = S._tool_window(session, {"action": "minimize", "app": "explorer"})
    assert seen["confirmed"] is False
    assert out["confirmed"] is False
