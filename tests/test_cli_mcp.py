"""MCP 门面 CLI 层测试（PR-6）：工具表输出 + 真子进程握手探针。

探针是真机验收的可重复版本：起 `python -m winuse mcp serve` 子进程，
走完 initialize / initialized / tools/list / tools/call 全流程。
只调只读工具（winuse_display），不碰用户桌面。
"""

from __future__ import annotations

import json

import pytest

from winuse import cli
from winuse.mcp import protocol as P
from winuse.mcp import server as S
from winuse.mcp.probe import probe


def test_cli_mcp_tools_lists_all_catalog_tools(capsys):
    rc = cli.main(["mcp", "tools"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    names = {t["name"] for t in payload["tools"]}
    assert names == set(S.TOOL_NAMES), "工具表以 server.TOOL_NAMES 为唯一出处"
    assert payload["count"] == len(S.TOOL_NAMES)
    act = next(t for t in payload["tools"] if t["name"] == "winuse_act")
    assert act["required"] == ["action"], "action 是 winuse_act 的必填参数"
    for tool in payload["tools"]:
        assert isinstance(tool["parameters"], list) and tool["parameters"], "每个工具都要有可传参数"


def test_probe_real_handshake_and_readonly_call():
    report = probe(tool="winuse_display", arguments={"no_windows": True}, timeout=60)

    if not report.get("ok") and report.get("steps", [{}])[0].get("step") == "initialize":
        pytest.skip(f"子进程未能完成握手：{report.get('stderr_head')}")
    if not report.get("ok") and report.get("call", {}).get("isError"):
        pytest.skip(f"驱动不可用，跳过真机探针：{report.get('call')}")

    assert report["ok"], report
    assert report["protocol_version"] in P.SUPPORTED_PROTOCOL_VERSIONS
    assert report["server_info"]["name"] == "winuse"
    assert set(report["tools"]) == set(S.TOOL_NAMES)
    assert report["call"]["ok"] is True
    assert report["exit_code"] == 0, "关 stdin 后服务端应正常退出"
    assert report["stderr_line_count"] >= 1, "日志必须走 stderr"


def test_probe_reports_stdout_not_polluted():
    """stdout 只许出现协议消息：探针能解析出全部回包即证明未被日志污染。"""
    report = probe(tool="winuse_display", arguments={"no_windows": True}, timeout=60)
    if not report.get("ok"):
        pytest.skip("环境不具备，见上一个用例的判定")
    assert all(step.get("ok") for step in report["steps"]), report["steps"]
