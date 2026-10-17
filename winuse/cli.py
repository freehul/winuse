"""WinUse 命令行入口（门面 3：CLI = 兜底 + 排查）。

命令：
- `doctor` ：自检（配置 → 驱动 → 一次真实轻量调用），给"能不能干活"一个明确答案
- `state`  ：取一份文本优先的本机状态（元素索引为主路径，默认不带截图）
- `measure`：体量实测（T-17）——原始返回 / 纯元素树 / 裁剪后 / 等价截图，四者对比
- `call`   ：[排查/开发] 透传一个驱动工具调用。**绕过安全层**（guard / 写值门禁 /
             confirm），必须显式加 `--unsafe-driver-call`，且会落一条审计。

用法（仓库根目录）：
    python -m winuse doctor
    python -m winuse state --app Hermes
    python -m winuse measure --app Hermes
    python -m winuse call list_windows --unsafe-driver-call
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import __version__, cli_capabilities
from .config import Config
from .core.act import ACTION_TOOLS, ActSpec, run_act
from .core.display import describe
from .core.state import StateOptions, build_state, pick_window
from .driver.errors import WinUseError
from .tokens import size_report

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: 真正的桌面动作一律不做；doctor 只碰这些只读工具
DOCTOR_PROBE_TOOL = "list_windows"

#: T-17 的截图基线：本机实测（1920×1041 一张截图 ≈ 2125–2665 token），按面积线性缩放
VISION_BASELINE_PIXELS = 1920 * 1041
VISION_BASELINE_TOKENS = (2125, 2665)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_doctor(args: argparse.Namespace) -> int:
    """自检：配置 → 驱动解析 → 版本 → 一次真实只读调用。"""
    report: dict = {"winuse_version": __version__, "checks": {}}
    try:
        cfg = Config.load(env_path=args.env)
    except WinUseError as exc:
        print(f"配置检查失败：{exc}", file=sys.stderr)
        _print({"ok": False, "error": exc.to_dict()})
        return EXIT_FAIL

    report["config"] = cfg.describe()

    driver = cfg.driver()
    health = driver.health()
    report["checks"]["driver"] = health
    if not health.get("ok"):
        report["ok"] = False
        print("驱动检查失败。", file=sys.stderr)
        _print(report)
        return EXIT_FAIL

    if args.probe:
        try:
            data = driver.call(DOCTOR_PROBE_TOOL)
        except WinUseError as exc:
            report["ok"] = False
            report["checks"]["probe"] = {"ok": False, "error": exc.to_dict()}
            print(f"只读探针失败：{exc}", file=sys.stderr)
            _print(report)
            return EXIT_FAIL
        count = len(data) if isinstance(data, (list, dict)) else None
        report["checks"]["probe"] = {
            "ok": True,
            "tool": DOCTOR_PROBE_TOOL,
            "result_type": type(data).__name__,
            "count": count,
        }

    report["ok"] = True
    _print(report)
    return EXIT_OK


#: `winuse call` 的显式开启开关。为什么要有：这个命令把任意驱动工具与参数**直接**
#: 交给驱动层，不经过 core.guard、写值门禁，历史上也没有审计 —— 是安全层的公开旁路。
#: 排查时确实需要它（要在安全层之外看驱动真实返回），所以保留，但逼调用方显式承认，
#: 并且落一条审计，事后能查到"是谁、调了什么"。
UNSAFE_FLAG = "--unsafe-driver-call"


def cmd_call(args: argparse.Namespace) -> int:
    """透传一次驱动调用（**绕过安全层**，仅供本地排查）。

    必须显式带 `--unsafe-driver-call`：不带则拒绝，且一次驱动调用都不下发。
    """
    if not getattr(args, "unsafe_driver_call", False):
        print(
            f"`winuse call` 会绕过安全层（不经过 guard / 写值门禁 / 确认门），"
            f"仅供本地排查；确认要这么做请显式加 {UNSAFE_FLAG}。"
            f"日常取状态请用 `winuse state`，动作请用 `winuse act`（默认干跑）。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        cfg = Config.load(env_path=args.env)
        raw = args.payload if args.payload is not None else "{}"
        if args.payload_file:
            from pathlib import Path

            raw = Path(args.payload_file).read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("参数必须是 JSON 对象，例如 '{\"pid\": 123}'")
        data = cfg.driver().call(args.tool, payload)
    except json.JSONDecodeError as exc:
        print(f"参数不是合法 JSON：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except (ValueError, WinUseError) as exc:
        print(f"调用失败：{exc}", file=sys.stderr)
        return EXIT_FAIL

    _record_raw_call(args.tool, payload, ok=True, env_path=args.env)
    _print(data)
    return EXIT_OK


def _record_raw_call(tool: str, payload: dict, *, ok: bool, env_path: str | None) -> None:
    """给绕过安全层的原始调用留痕（审计失败不能影响排查本身）。"""
    try:
        from . import audit as audit_mod

        audit_mod.record(
            audit_mod.build_entry(
                "driver.raw_call",
                action=str(tool),
                ok=ok,
                detail={"bypassed_safety_layer": True, "args": payload},
            ),
            env_path=env_path,
        )
    except Exception:  # noqa: BLE001 —— 审计不可用不该挡住排查
        pass


def _foreground_hint() -> int | None:
    """系统层面的真前台窗口标识（GetForegroundWindow）。

    比"取 z_index 最大的窗口"可靠：驱动自带的悬浮层（Cua.AgentCursorOverlay）
    也是顶层窗口且 z_index 最大，实测会骗走前台判定。
    """
    try:
        from .core.display import foreground_window

        fw = foreground_window()
        return fw.get("window_id") if fw else None
    except Exception:  # noqa: BLE001
        return None


def _options_from_args(cfg: Config, args: argparse.Namespace) -> StateOptions:
    return StateOptions(
        app=args.app,
        pid=args.pid,
        window_id=args.window_id,
        allow_minimized=args.allow_minimized,
        include_frames=args.frames,
        max_elements=(args.max if args.max is not None else cfg.state_max_elements),
        include_windows=not args.no_windows,
        query=args.query,
        foreground_window_id=_foreground_hint(),
        # 只有 state 子命令声明了这个开关（measure 等共用本函数，故用 getattr 兜底）
        include_tree_hash=getattr(args, "include_tree_hash", False),
    )


def cmd_state(args: argparse.Namespace) -> int:
    """取一份文本优先的本机状态。"""
    try:
        cfg = Config.load(env_path=args.env)
        out = build_state(cfg.driver(), _options_from_args(cfg, args))
    except WinUseError as exc:
        print(f"取状态失败：{exc}", file=sys.stderr)
        _print({"ok": False, "error": exc.to_dict()})
        return EXIT_FAIL
    # 指纹由**核心**在裁剪/投影之前算（整窗口径）并随回执带口径，这里不再二次计算：
    # 拿裁剪后的 out["elements"] 再算一遍，会把「参数变了」误报成「界面变了」。
    payload = json.dumps(out, ensure_ascii=False, indent=2)
    out["size"] = dict(size_report(payload), note="先测量后附加，故不计入本字段自身")
    _print(out)
    return EXIT_OK


def cmd_measure(args: argparse.Namespace) -> int:
    """T-17 体量实测：原始返回 / 纯元素树 / 裁剪后 / 等价截图，四者对比。"""
    try:
        cfg = Config.load(env_path=args.env)
        driver = cfg.driver()
        opts = _options_from_args(cfg, args)
        windows = (driver.call("list_windows", {}) or {}).get("windows") or []
        target = pick_window(windows, opts)
        where = {"pid": target["pid"], "window_id": target["window_id"]}

        raw = driver.call("get_window_state", dict(where, include_screenshot=True)) or {}
        tree = driver.call("get_window_state", dict(where, include_screenshot=False)) or {}
        qtree = None
        if opts.query:
            qtree = driver.call("get_window_state", dict(where, include_screenshot=False, query=opts.query)) or {}
        shipped = build_state(driver, opts)
    except WinUseError as exc:
        print(f"实测失败：{exc}", file=sys.stderr)
        _print({"ok": False, "error": exc.to_dict()})
        return EXIT_FAIL

    raw_text = json.dumps(raw, ensure_ascii=False)
    tree_text = json.dumps(tree, ensure_ascii=False)
    state_text = json.dumps(shipped, ensure_ascii=False)

    width, height = raw.get("screenshot_width"), raw.get("screenshot_height")
    vision = None
    if width and height:
        lo, hi = VISION_BASELINE_TOKENS
        vision = {
            "pixels": f"{width}x{height}",
            "estimated_vision_tokens_range": [
                round(lo * width * height / VISION_BASELINE_PIXELS),
                round(hi * width * height / VISION_BASELINE_PIXELS),
            ],
            "method": "按本机实测基线（1920×1041 ≈ 2125–2665 token）按像素面积线性缩放；"
            "不同视觉模型差异较大，仅作量级参考",
        }

    report = {
        "ok": True,
        "window": {
            "app_name": target.get("app_name"),
            "pid": target.get("pid"),
            "window_id": target.get("window_id"),
            "title": target.get("title"),
        },
        "raw_driver_response": dict(
            size_report(raw_text),
            includes="base64 截图 + Markdown 树 + 全量元素（驱动默认返回）",
        ),
        "tree_only": dict(size_report(tree_text), includes="纯元素树（不下发截图，但未裁剪）"),
        "winuse_state": dict(size_report(state_text), includes="WinUse 对外输出（裁剪后，默认无 frame）"),
        "screenshot_vision_equivalent": vision,
        "ratio": {
            "winuse_vs_raw": round(len(state_text) / max(1, len(raw_text)), 4),
            "winuse_vs_tree": round(len(state_text) / max(1, len(tree_text)), 4),
        },
        "trim_stats": shipped.get("stats"),
        "note": "均为字符数口径；token 为估算（CJK≈1/字，其余≈4字符/token）。"
        "raw 里的大头是 base64 截图，其视觉成本另见 screenshot_vision_equivalent。",
    }
    if qtree is not None:
        q_text = json.dumps(qtree, ensure_ascii=False)
        report["query_projection"] = dict(
            size_report(q_text),
            query=opts.query,
            includes="驱动侧投影：仅匹配行 + 其祖先链（未裁剪）",
        )
        report["ratio"]["query_vs_tree"] = round(len(q_text) / max(1, len(tree_text)), 4)
    _print(report)
    return EXIT_OK


def _audit_sink(args: argparse.Namespace):
    """把审计落盘函数交给核心（路径由 .env 的 WINUSE_AUDIT_PATH 决定，缺省 ~/.winuse/audit.jsonl）。

    返回值遵循 `audit.sink_event` 的协议：**落盘失败必须显式返回 False**
    （`record` 失败返回 None，这里翻成 False），否则核心回执里的 `audit_written`
    永远为真、审计失败又被静默吞掉（R-1）。同时补 `source=cli`，事后能分清
    是"命令行干的"还是"MCP 客户端干的"。
    """
    from . import audit as audit_mod

    return audit_mod.env_sink(args.env, source="cli")


def cmd_act(args: argparse.Namespace) -> int:
    """下发一个元素级动作（默认干跑，只有 --execute 才真动）。"""
    try:
        cfg = Config.load(env_path=args.env)
        driver = cfg.driver()
        opts = StateOptions(
            app=args.app,
            pid=args.pid,
            window_id=args.window_id,
            allow_minimized=args.allow_minimized,
            foreground_window_id=_foreground_hint(),
        )
        windows = (driver.call("list_windows", {}) or {}).get("windows") or []
        target = pick_window(windows, opts)
        spec = ActSpec(
            action=args.do,
            element_index=args.index,
            element_token=args.token,
            value=args.value,
            text=args.text,
            key=args.key,
            modifiers=[m for m in (args.modifiers or "").split(",") if m],
            direction=args.direction,
            amount=args.amount,
            by=args.by,
            button=args.button,
            count=args.count,
            x=args.x,
            y=args.y,
        )
        out = run_act(
            driver,
            spec,
            target,
            args.snapshot,
            execute=args.execute,
            confirm=args.confirm,
            overwrite=args.overwrite,
            # 第二把钥匙（R-3）：与 --overwrite 同时给才放行"读不到目标内容"的写入
            allow_unverified=getattr(args, "allow_unverified", False),
            audit=_audit_sink(args),
        )
    except WinUseError as exc:
        print(f"动作失败：{exc}", file=sys.stderr)
        _print({"ok": False, "error": exc.to_dict()})
        return EXIT_FAIL
    _print(out)
    return EXIT_OK if out.get("ok") else EXIT_FAIL


def cmd_display(args: argparse.Namespace) -> int:
    """显示器 / DPI / 窗口归属（原生枚举，不依赖驱动的主屏信息）。"""
    try:
        cfg = Config.load(env_path=args.env)
        out = describe(cfg.driver())
    except WinUseError as exc:
        print(f"取显示信息失败：{exc}", file=sys.stderr)
        _print({"ok": False, "error": exc.to_dict()})
        return EXIT_FAIL
    if args.no_windows:
        out.pop("windows", None)
    _print(out)
    return EXIT_OK


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    """以 stdio 常驻提供 MCP 服务（由客户端拉起子进程）。"""
    from .mcp.server import serve

    return serve(env_path=args.env)


def cmd_mcp_tools(args: argparse.Namespace) -> int:
    """列出 MCP 工具表（给人看；字段名与 MCP 规范一致）。"""
    from .mcp.server import tool_catalog

    tools = tool_catalog()
    _print(
        {
            "ok": True,
            "count": len(tools),
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": sorted((t["inputSchema"].get("properties") or {}).keys()),
                    "required": t["inputSchema"].get("required", []),
                }
                for t in tools
            ],
        }
    )
    return EXIT_OK


def cmd_mcp_probe(args: argparse.Namespace) -> int:
    """自测：起真子进程走完整握手，并真实调用一次工具。"""
    from .mcp.probe import probe

    report = probe(python=args.python, tool=args.tool, timeout=args.timeout, env_path=args.env)
    if args.json:
        _print(report)
    else:
        print(f"握手：{'成功' if report.get('protocol_version') else '失败'}"
              f"（协议 {report.get('protocol_version')}，服务端 {report.get('server_info', {}).get('name')}）")
        print(f"工具：{report.get('tools')}")
        print(f"调用：{report.get('call')}")
        print(f"耗时：{report.get('elapsed_ms')} ms；退出码 {report.get('exit_code')}"
              f"（stderr {report.get('stderr_line_count')} 行，未污染 stdout）")
        for step in report.get("steps", []):
            if not step.get("ok"):
                print(f"  未通过：{step}")
    return EXIT_OK if report.get("ok") else EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="winuse",
        description="WinUse —— 桌面能力的统一门面（cua-driver 之上的方向盘）",
    )
    parser.add_argument("--version", action="version", version=f"WinUse {__version__}")
    parser.add_argument("--env", default=None, help="指定 .env 路径（默认读仓库根/.env）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="自检：配置 + 驱动 + 一次只读探针")
    p_doctor.add_argument(
        "--no-probe",
        dest="probe",
        action="store_false",
        help="跳过真实只读调用（只验驱动版本）",
    )
    p_doctor.set_defaults(probe=True, func=cmd_doctor)

    def add_state_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--app", default=None, help="按 app_name 或 title 的子串选窗口（不区分大小写）")
        p.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
        p.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口（最精确）")
        p.add_argument(
            "--allow-minimized",
            action="store_true",
            help="明确接受最小化窗口（其像素坐标是哨兵值，坐标类动作不可用）",
        )
        p.add_argument("--frames", action="store_true", help="元素附像素坐标（默认不带：元素索引优先）")
        p.add_argument(
            "--max",
            type=int,
            default=None,
            help="最多返回多少元素（默认读 .env 的 WINUSE_STATE_MAX_ELEMENTS）",
        )
        p.add_argument("--no-windows", action="store_true", help="不输出窗口清单与前台窗口")
        p.add_argument(
            "--query",
            default=None,
            help="只取匹配该子串的元素＋其祖先链（驱动侧投影，最省的取法）",
        )

    p_state = sub.add_parser("state", help="取一份文本优先的本机状态")
    add_state_args(p_state)
    p_state.add_argument(
        "--include-tree-hash",
        action="store_true",
        help="附 UI 树指纹（64 位十六进制）：判断「界面变了没」用它，窗口一移动坐标就变、指纹不变",
    )
    p_state.set_defaults(func=cmd_state)

    p_measure = sub.add_parser("measure", help="体量实测：原始 / 纯树 / 裁剪后 / 等价截图 四者对比")
    add_state_args(p_measure)
    p_measure.set_defaults(func=cmd_measure)

    p_display = sub.add_parser("display", help="显示器 / DPI / 窗口归属（ctypes 原生枚举）")
    p_display.add_argument("--no-windows", action="store_true", help="只看显示器，不列窗口")
    p_display.set_defaults(func=cmd_display)

    p_act = sub.add_parser("act", help="下发一个元素级动作（默认干跑，加 --execute 才真动）")
    p_act.add_argument("--do", required=True, help="动作名：" + "、".join(sorted(ACTION_TOOLS)))
    p_act.add_argument("--app", default=None, help="按 app_name 或 title 子串选窗口")
    p_act.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_act.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_act.add_argument("--allow-minimized", action="store_true", help="接受最小化窗口")
    p_act.add_argument("--index", type=int, default=None, help="元素索引（来自 winuse state）")
    p_act.add_argument("--token", default=None, help="元素令牌（来自 winuse state，可免快照）")
    p_act.add_argument(
        "--snapshot",
        default=None,
        help="快照标识（取自 winuse state 的 snapshot_id）——用 --index 时必须给：索引只在该快照内有效",
    )
    p_act.add_argument("--value", default=None, help="set_value 的新值")
    p_act.add_argument("--text", default=None, help="type 要输入的文字")
    p_act.add_argument("--key", default=None, help="key 的按键名（return/tab/escape/down…）")
    p_act.add_argument("--modifiers", default=None, help="逗号分隔的组合键（ctrl,shift,alt,win）")
    p_act.add_argument("--direction", default=None, help="scroll 方向：up/down/left/right")
    p_act.add_argument("--amount", type=int, default=3, help="scroll 刻度数（默认 3）")
    p_act.add_argument("--by", default="line", choices=["line", "page"], help="scroll 粒度")
    p_act.add_argument("--button", default="left", choices=["left", "right", "middle"], help="鼠标键")
    p_act.add_argument("--count", type=int, default=1, help="点击次数（2=双击）")
    p_act.add_argument(
        "--x",
        type=int,
        default=None,
        help="坐标寻址：窗口内截图像素横坐标（与 --y 成对，且与元素寻址互斥）",
    )
    p_act.add_argument(
        "--y",
        type=int,
        default=None,
        help="坐标寻址：窗口内截图像素纵坐标（canvas/自绘控件等无元素可寻址时用）",
    )
    p_act.add_argument(
        "--execute",
        action="store_true",
        help="真的下发动作（默认只干跑，回显将要发的调用）",
    )
    p_act.add_argument(
        "--confirm",
        action="store_true",
        help="确认执行危险动作（结束进程 / 系统级窗口 / 电源类）；不带则被安全层拒绝（CONFIRM_REQUIRED）",
    )
    p_act.add_argument(
        "--allow-unverified",
        dest="allow_unverified",
        action="store_true",
        help=(
            "与 --overwrite 同时给才生效：授权写入「读不到既有内容」的特殊控件"
            "（自绘/虚拟列表等）。单独给无效——那不等于你接受覆盖内容（R-3）"
        ),
    )
    p_act.add_argument(
        "--overwrite",
        action="store_true",
        help="显式授权覆盖目标元素的既有内容（set_value 会整段替换）；不带则非空即拒（EXISTING_CONTENT）",
    )
    p_act.set_defaults(func=cmd_act)

    p_call = sub.add_parser(
        "call",
        help=f"[排查/开发] 透传调用一个驱动工具（绕过安全层，需 {UNSAFE_FLAG}）",
        description=(
            "【排查/开发专用】把一个驱动工具与参数**直接**交给驱动层，绕过安全层：\n"
            "  绕过内容：core.guard 危险判定、写值门禁、execute/confirm/overwrite 语义。\n"
            "  仅有的护栏：必须显式加 "
            + UNSAFE_FLAG
            + "，且落一条 driver.raw_call 审计。\n"
            "日常请用 `state`（只读取状态）或 `act`（默认干跑）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_call.add_argument("tool", help="驱动工具名，例如 list_windows")
    p_call.add_argument("payload", nargs="?", default=None, help="JSON 对象参数")
    p_call.add_argument("--payload-file", default=None, help="从文件读 JSON 参数")
    p_call.add_argument(
        UNSAFE_FLAG,
        dest="unsafe_driver_call",
        action="store_true",
        help="显式承认：我知道这会绕过安全层，且这是本地排查行为",
    )
    p_call.set_defaults(func=cmd_call)

    p_mcp = sub.add_parser("mcp", help="MCP stdio 门面（供其它 agent 挂载）")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command", required=True)

    mcp_sub.add_parser("serve", help="以 stdio 常驻提供 MCP 服务（客户端拉起子进程用）").set_defaults(
        func=cmd_mcp_serve
    )
    mcp_sub.add_parser("tools", help="列出 MCP 工具表").set_defaults(func=cmd_mcp_tools)

    p_mcp_probe = mcp_sub.add_parser("probe", help="自测：真子进程握手 + 真调一次工具")
    p_mcp_probe.add_argument("--tool", default="winuse_display", help="要调用的工具名（默认只读的 winuse_display）")
    p_mcp_probe.add_argument("--python", default=None, help="指定解释器（默认当前解释器）")
    p_mcp_probe.add_argument("--timeout", type=float, default=90.0, help="单步等待上限（秒）")
    # 带上 --env 才能验证"审计写入端与查询端同源"（见 --env 的全局说明）
    p_mcp_probe.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    p_mcp_probe.set_defaults(func=cmd_mcp_probe)

    cli_capabilities.register(sub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except WinUseError as exc:
        # 已知错误已经在各处结构化返回；这里是**兜底**（例如子命令没自己兜）
        print(f"失败：{exc.to_dict()['message']}", file=sys.stderr)
        _print({"ok": False, "error": exc.to_dict()})
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001 —— R-10：CLI 不该往用户脸上扔 traceback
        # 意外异常也要给结构化结论；文本走脱敏（可能含本机路径）
        from .audit import redact_text

        _print(
            {
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"未预期的内部错误：{type(exc).__name__}",
                    "detail": redact_text(str(exc), limit=200),
                },
            }
        )
        return EXIT_FAIL
