"""MCP 自测探针：起一个真子进程跑 `python -m winuse mcp serve`，走完整握手并调一次工具。

为什么要它：验收标准是"MCP 探针握手成功并能调用"，而这不该依赖任何外部 MCP 客户端
（不装 Claude Desktop / WorkBuddy 也能自证）。探针读了 stderr 单独收集——顺便验证
"日志不污染 stdout"这条规范要求。

零依赖：只用一个读线程 + 队列（Windows 上 select 对管道不可用）。
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import protocol as P


def project_root() -> Path:
    """仓库根：本文件在 <root>/winuse/mcp/probe.py。"""
    return Path(__file__).resolve().parents[2]


def _reader(stream, sink: queue.Queue) -> None:
    try:
        for line in stream:
            sink.put(line)
    finally:
        sink.put(None)


def _wait_for_id(sink: queue.Queue, msg_id: int, timeout: float) -> dict | None:
    """从输出队列里等指定 id 的回包（其余消息计为噪音，一并返回给调用方判断）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = sink.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            return None
        msg = P.decode_message(line)
        if msg is not None and msg.get("id") == msg_id:
            return msg
    return None


def _serve_command(exe: str, env_path: str | None) -> list[str]:
    """拉起服务端的命令行。带 `--env` 时审计写入与查询才会同源（R-1）。"""
    # `--env` 是**全局**参数，必须写在子命令之前（`winuse --env X mcp serve`），
    # 写在后面 argparse 会直接报 unrecognized arguments
    cmd = [exe, "-m", "winuse"]
    if env_path:
        cmd += ["--env", str(env_path)]
    return cmd + ["mcp", "serve"]


def probe(
    python: str | None = None,
    tool: str = "winuse_display",
    arguments: dict | None = None,
    timeout: float = 90.0,
    cwd: Path | None = None,
    env_path: str | None = None,
) -> dict:
    """跑一次完整握手；返回结构化报告（不抛异常，失败也如实入报告）。"""
    exe = python or sys.executable
    root = cwd or project_root()
    started = time.perf_counter()
    report: dict[str, Any] = {
        "ok": False,
        "command": _serve_command(exe, env_path),
        "cwd": str(root),
        "tool": tool,
        "steps": [],
    }

    proc = subprocess.Popen(  # noqa: S603
        _serve_command(exe, env_path),
        cwd=str(root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    out_q: queue.Queue = queue.Queue()
    err_q: queue.Queue = queue.Queue()
    readers = [
        threading.Thread(target=_reader, args=(proc.stdout, out_q), name="winuse-probe-stdout", daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, err_q), name="winuse-probe-stderr", daemon=True),
    ]
    for thread in readers:
        thread.start()

    def send(msg: dict) -> float:
        t0 = time.perf_counter()
        assert proc.stdin is not None
        proc.stdin.write(P.encode_message(msg) + "\n")
        proc.stdin.flush()
        return time.perf_counter() - t0

    def step(name: str, ok: bool, detail: dict) -> None:
        report["steps"].append({"step": name, "ok": ok, **detail})

    try:
        # 1) initialize
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": P.PREFERRED_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "winuse-probe", "version": "0.1.0"},
                },
            }
        )
        init = _wait_for_id(out_q, 1, timeout)
        if not init or "result" not in init:
            step("initialize", False, {"reply": init})
            return report
        report["protocol_version"] = init["result"].get("protocolVersion")
        report["server_info"] = init["result"].get("serverInfo")
        report["capabilities"] = init["result"].get("capabilities")
        step("initialize", True, {"protocolVersion": report["protocol_version"], "serverInfo": report["server_info"]})

        # 2) initialized 通知（不期望回包）
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3) tools/list
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = _wait_for_id(out_q, 2, timeout)
        if not listed or "result" not in listed:
            step("tools/list", False, {"reply": listed})
            return report
        tools = listed["result"].get("tools") or []
        report["tools"] = [t.get("name") for t in tools]
        step("tools/list", bool(tools), {"count": len(tools), "tools": report["tools"]})

        # 4) tools/call（默认用只读工具，真机安全）
        call_args = arguments if arguments is not None else {"no_windows": True}
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": tool, "arguments": call_args}})
        called = _wait_for_id(out_q, 3, timeout)
        if not called or "result" not in called:
            step("tools/call", False, {"reply": called, "arguments": call_args})
            return report
        content = (called["result"].get("content") or [{}])[0]
        payload = json.loads(content.get("text", "{}")) if content.get("type") == "text" else {}
        report["call"] = {
            "isError": bool(called["result"].get("isError")),
            "ok": bool(payload.get("ok")),
            "summary": _summarize(tool, payload),
        }
        step("tools/call", report["call"]["ok"], {"arguments": call_args, **report["call"]})
        report["ok"] = bool(report["call"]["ok"])
    except Exception as exc:  # noqa: BLE001
        step("exception", False, {"error": f"{type(exc).__name__}: {exc}"})
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()  # 关 stdin → 服务端应正常退出（规范里的关闭姿势）
        except Exception:  # noqa: BLE001
            pass
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = None
        report["exit_code"] = code
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        lines = []
        while not err_q.empty():
            item = err_q.get()
            if item:
                lines.append(item.rstrip("\n"))
        report["stderr_head"] = lines[:8]
        report["stderr_line_count"] = len(lines)
        _release_subprocess(proc, readers)

    return report


def _release_subprocess(proc, readers: list[threading.Thread]) -> None:
    """收尾：join 读线程 + 关掉所有管道（R-9）。

    为什么必须显式关：`Popen(stdout=PIPE)` 出来的文本流是**真文件对象**，
    线程读完后不会自己关。依赖 GC 兜底意味着"什么时候回收"不可控——反复探测、
    插件重连、多 agent 并用时会积累句柄，而 `pytest -W error` 会把它直接判成失败
    （报告里唯一暴露的警告就是这个）。

    顺序：读线程先结束（进程已退出 → 管道到 EOF）→ 再关流。
    反过来关流会让读线程抛在 `for line in stream` 里，留下噪音。
    """
    for thread in readers:
        thread.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:  # noqa: BLE001 —— 收尾失败不该影响报告
            pass


def _summarize(tool: str, payload: dict) -> dict:
    """给报告一段一眼能看懂的摘要（不复制大块数据）。"""
    if not payload:
        return {}
    if tool == "winuse_display":
        monitors = payload.get("monitors") or []
        return {
            "monitor_count": payload.get("monitor_count", len(monitors)),
            "scales": [m.get("scale") for m in monitors],
            "has_windows": "windows" in payload,
        }
    if tool == "winuse_state":
        stats = payload.get("stats") or {}
        return {
            "target_title": (payload.get("target") or {}).get("title"),
            "snapshot_id": payload.get("snapshot_id"),
            "elements": len(payload.get("elements") or []),
            "elements_total": stats.get("elements_total"),
        }
    if tool == "winuse_act":
        return {"dry_run": payload.get("dry_run"), "action": payload.get("action"), "code": payload.get("code")}
    return {"keys": sorted(payload)[:8]}
