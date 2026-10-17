"""门面 3 的命令实现：PR-8 新增的 8 个能力 + PR-9 的审计查询（CLI = 兜底 + 排查）。

分工：本文件**只做参数翻译与结果打印**，业务逻辑一律在 `winuse/core/`。
安全：需要确认的能力（电源意图、注册表写意图、批量输入、关窗口）在这里走 `core.guard`
统一判定；未确认一律返回 `CONFIRM_REQUIRED`。注意两条独立门：
- **确认门**：危险动作要 `--confirm`
- **干跑门**：真动桌面要 `--execute`
两者不互相替代（确认了也不等于执行）。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import audit as audit_mod
from .config import Config
from .core import desktop as desktop_mod
from .core import guard as guard_mod
from .core import multi_input as multi_mod
from .core import notify as notify_mod
from .core import ocr as ocr_mod
from .core import registry as registry_mod
from .core import system as system_mod
from .core import wait_for as wait_mod
from .core import app as app_mod
from .core import drag as drag_mod
from .core import menu as menu_mod
from .core import screenshot as screenshot_mod
from .core import search as search_mod
from .core import verify as verify_mod
from .driver.errors import WinUseError

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _finish(fn, *args, **kwargs) -> int:
    """统一收口：业务异常 → 结构化 JSON；结果 ok=false → 非零退出码。"""
    try:
        payload = fn(*args, **kwargs)
    except WinUseError as exc:
        # 用脱敏后的 message（R-20）：str(exc) 是原始文本，可能带本机路径
        print(f"失败：{exc.to_dict()['message']}", file=sys.stderr)
        _print({"ok": False, "error": exc.to_dict()})
        return EXIT_FAIL
    _print(payload)
    return EXIT_OK if payload.get("ok", True) else EXIT_FAIL


def _mark_blocked_audit(args: argparse.Namespace, event: str, verdict: dict, **detail) -> None:
    """门面**提前**拦截时补一条审计（R-1）。

    门面这层重复判定只为两个好处：在解析目标之前就拒（省一次驱动往返）、提示更具体；
    核心的闸门仍然自包含（R-12/R-13）。既然拦在这里，就得在这里留痕，否则
    "被拦下的尝试"在日志里彻底消失。事件名与核心一致，便于统一对账。
    """
    payload = {"reason": verdict.get("reason"), "rule": verdict.get("rule"), "blocked_at": "facade"}
    payload.update(detail)
    audit_mod.sink_event(
        audit_mod.env_sink(args.env, source="cli"),
        event=event,
        ok=False,
        code=verdict.get("code"),
        detail=payload,
    )


def _confirm_required(verdict: dict, hint: str) -> dict:
    return {
        "ok": False,
        "requires_confirm": True,
        "danger": verdict,
        "error": {
            "code": verdict.get("code") or "CONFIRM_REQUIRED",
            "message": verdict.get("reason") or "危险动作需要确认",
            "detail": hint,
        },
    }


# ------------------------------------------------------------------ 目标窗口 / 参数解析辅助


def _pick_target(driver, args: argparse.Namespace) -> dict:
    """按 --app / --pid / --window-id 选目标窗口（复用 core.state 的选择语义，不另写一份）。"""
    from .core.state import StateOptions, pick_window

    opts = StateOptions(
        app=getattr(args, "app", None),
        pid=getattr(args, "pid", None),
        window_id=getattr(args, "window_id", None),
        allow_minimized=True,
    )
    windows = (driver.call("list_windows", {}) or {}).get("windows") or []
    return pick_window(windows, opts)


def _parse_point(text: str, flag: str) -> dict:
    """`--from-point 10,20` → {"x": 10, "y": 20}（只认两段数字，不猜顺序）。"""
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 2 or not all(parts):
        raise WinUseError(f"{flag} 要写成 x,y 两段数字")
    try:
        x, y = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise WinUseError(f"{flag} 的 x/y 必须是数字：{exc}") from exc
    return {"x": x, "y": y}


def _parse_json_flag(text: str, flag: str):
    """JSON 参数解析；失败给用法级错误（不吞、不猜）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WinUseError(f"{flag} 不是合法 JSON：{exc}") from exc


# ------------------------------------------------------------------ 只读能力


def cmd_ocr(args: argparse.Namespace) -> int:
    """原生 OCR：把图片里的文字读成结构化文本（Windows 自带引擎，不联网）。"""
    return _finish(ocr_mod.run_ocr, ocr_mod.OcrSpec(image_path=args.image, language=args.language))


def cmd_system(args: argparse.Namespace) -> int:
    """系统盘点；带 --power-action 时只返回电源动作意图（不执行）。"""
    if args.power_action:

        def _run_power() -> dict:
            verdict = guard_mod.assess_power(args.power_action).to_dict()
            if verdict.get("dangerous") and not args.confirm:
                payload = _confirm_required(verdict, "确认方式：加 --confirm（仍然只返回意图，不会真关机）")
                payload["power"] = system_mod.plan_power_action(args.power_action)
                _mark_blocked_audit(args, "system.blocked", verdict)
                return payload
            out = system_mod.plan_power_action(args.power_action)
            out["confirmed"] = bool(args.confirm)
            return out

        return _finish(_run_power)
    return _finish(system_mod.describe_system, system_mod.SystemSpec(top_processes=args.top))


def cmd_registry(args: argparse.Namespace) -> int:
    """注册表：读为安全；**写只返回意图**（本轮不含真实写入）。"""
    if args.write or args.delete:

        def _run_write() -> dict:
            spec = registry_mod.RegWriteSpec(
                hive=args.hive,
                path=args.path,
                value_name=args.value_name,
                value=args.value,
                value_type=args.value_type or "REG_SZ",
                delete=bool(args.delete),
            )
            verdict = guard_mod.assess_registry_write(args.hive, args.path).to_dict()
            if verdict.get("dangerous") and not args.confirm:
                payload = _confirm_required(verdict, "确认方式：加 --confirm（仍然只返回意图，不会真写注册表）")
                payload["intent"] = registry_mod.plan_registry_write(spec)
                _mark_blocked_audit(args, "registry.blocked", verdict)
                return payload
            out = registry_mod.plan_registry_write(spec)
            out["confirmed"] = bool(args.confirm)
            return out

        return _finish(_run_write)
    spec = registry_mod.RegReadSpec(
        hive=args.hive,
        path=args.path,
        value_name=args.value_name,
        recurse=bool(args.recurse),
        max_depth=args.max_depth,
        max_subkeys=args.max_subkeys,
    )
    # 敏感读（HKLM\SYSTEM\CurrentControlSet / BCD00000000 等）要 --confirm（R-11）；
    # 判据在核心（guard.assess_registry_read），门面只透传确认状态
    if guard_mod.assess_registry_read(args.hive, args.path).dangerous and not args.confirm:
        verdict = guard_mod.assess_registry_read(args.hive, args.path).to_dict()
        payload = _confirm_required(
            verdict,
            "确认方式：加 --confirm（读系统配置区不改系统，但足以摸清机器配置）",
        )
        _mark_blocked_audit(args, "registry.blocked", verdict, mode="read")
        _print(payload)
        return EXIT_FAIL
    return _finish(registry_mod.read_registry, spec, confirmed=bool(args.confirm))


def cmd_vdesktop(args: argparse.Namespace) -> int:
    """虚拟桌面探测：窗口属于哪个桌面、是否在当前桌面。"""
    return _finish(desktop_mod.virtual_desktop_info, args.window_id)


def cmd_wait(args: argparse.Namespace) -> int:
    """条件等待：等窗口出现/消失、标题匹配、元素或文字出现（超时是结果不是异常）。"""
    spec = wait_mod.WaitSpec(
        kind=args.kind,
        app=args.app,
        pid=args.pid,
        window_id=args.window_id,
        pattern=args.pattern,
        element_index=args.element_index,
        element_token=args.element_token,  # datascan:ok：转发命令行解析出的参数，非凭据字面量
        snapshot_id=args.snapshot,
        timeout_seconds=args.timeout,
        interval_seconds=args.interval,
        seconds=args.seconds,
    )

    def _run() -> dict:
        return wait_mod.wait_for(Config.load(env_path=args.env).driver(), spec)

    return _finish(_run)


# ------------------------------------------------------------------ 有副作用能力


def cmd_notify(args: argparse.Namespace) -> int:
    """发一条 Windows 通知（默认干跑，--execute 才真发）。"""
    spec = notify_mod.NotifySpec(
        title=args.title,
        body=args.body,
        level=args.level,
        timeout_seconds=args.timeout,
        execute=bool(args.execute),
        broadcast=bool(getattr(args, "broadcast", False)),
    )
    return _finish(
        notify_mod.run_notify,
        spec,
        audit=audit_mod.env_sink(args.env, source="cli"),
        confirmed=bool(args.confirm),
    )


def cmd_multi(args: argparse.Namespace) -> int:
    """多步动作批处理：批量输入永远算危险动作，需 --confirm；真动桌面另需 --execute。"""
    try:
        steps = json.loads(args.steps)
    except json.JSONDecodeError as exc:
        _print({"ok": False, "error": {"code": "BAD_PARAM", "message": f"--steps 不是合法 JSON：{exc}"}})
        return EXIT_USAGE
    if not isinstance(steps, list) or not steps:
        _print({"ok": False, "error": {"code": "BAD_PARAM", "message": "--steps 必须是非空 JSON 数组"}})
        return EXIT_USAGE

    verdict = guard_mod.assess_batch(steps).to_dict()
    if verdict.get("dangerous") and not args.confirm:
        payload = _confirm_required(verdict, "确认方式：加 --confirm（批量输入不因确认而自动执行，仍需 --execute）")
        payload["steps_count"] = len(steps)
        _mark_blocked_audit(args, "batch.blocked", verdict, steps=len(steps))
        _print(payload)
        return EXIT_FAIL

    def _run() -> dict:
        cfg = Config.load(env_path=args.env)
        driver = cfg.driver()
        from .core.state import StateOptions, build_state, pick_window

        opts = StateOptions(app=args.app, pid=args.pid, window_id=args.window_id, allow_minimized=True)
        state = build_state(driver, opts)
        target = pick_window(state.get("windows") or [], opts)
        return multi_mod.run_multi(
            driver,
            steps,
            target,
            args.snapshot,
            execute=bool(args.execute),
            stop_on_error=not args.no_stop_on_error,
            # 门面已判过 assess_batch，这里把确认状态交给核心，让核心也自包含（R-13）
            confirmed=bool(args.confirm),
            audit=audit_mod.env_sink(args.env, source="cli"),
        )

    return _finish(_run)


def cmd_window(args: argparse.Namespace) -> int:
    """窗口动作（最小化/最大化/还原/置顶/移动缩放/关闭）。关闭与系统级窗口需 --confirm。"""
    verdict = guard_mod.assess_action(action=args.action, target=None).to_dict()
    if verdict.get("dangerous") and not args.confirm:
        _mark_blocked_audit(args, "window.blocked", verdict)
        _print(_confirm_required(verdict, "确认方式：加 --confirm（关闭类动作会改变用户桌面状态）"))
        return EXIT_FAIL

    spec = desktop_mod.WindowActionSpec(
        action=args.action,
        pid=args.pid,
        window_id=args.window_id,
        app=args.app,
        x=args.x,
        y=args.y,
        width=args.width,
        height=args.height,
        always_on_top=args.always_on_top,
    )

    def _run() -> dict:
        out = desktop_mod.window_action(
            Config.load(env_path=args.env).driver(),
            spec,
            execute=bool(args.execute),
            # 与 core.act 的危险动作口径一致：危险 + 显式确认 → 放行
            confirmed=bool(args.confirm),
            audit=audit_mod.env_sink(args.env, source="cli"),
        )
        out["confirmed"] = bool(args.confirm)
        return out

    return _finish(_run)


# ------------------------------------------------------------------ 接线


def cmd_tools(args: argparse.Namespace) -> int:
    """导出核心工具表（门面 1 从这里读，避免各门面各写一份 schema 导致漂移）。"""
    from .mcp.server import tool_catalog

    catalog = tool_catalog()
    _print({"ok": True, "count": len(catalog), "tools": catalog})
    return EXIT_OK


def cmd_audit(args: argparse.Namespace) -> int:
    """审计日志查询（只读，绝不写盘）：最近 N 条，或日志与轮转概况。

    路径口径与写入端**完全一致**（`cli._audit_sink` 同款）：`--env` 指向的 .env 里
    `WINUSE_AUDIT_PATH`，缺省 `~/.winuse/audit.jsonl`；刻意不读进程环境变量。
    `--path` 只给临时排查用——口径不一致会让人"查到 0 条"，误判成审计丢了。
    """
    path = args.path or audit_mod.default_path(args.env)

    if args.stats:

        def _stats() -> dict:
            return {"ok": True, "rotation": audit_mod.describe_log(path, env_path=args.env)}

        return _finish(_stats)

    if args.recent < 1:
        _print({"ok": False, "error": {"code": "BAD_PARAM", "message": "--recent 必须是 >= 1 的整数"}})
        return EXIT_USAGE

    def _recent() -> dict:
        entries = audit_mod.read_recent(args.recent, path=path, env_path=args.env)
        return {
            "ok": True,
            "path": str(path),
            "requested": args.recent,
            "returned": len(entries),
            "entries": entries,
        }

    return _finish(_recent)


# ------------------------------------------------------------------ 新增能力（P0-4 / P1-1 / P1-2 / P1-3 / 全盘搜索）


def cmd_screenshot(args: argparse.Namespace) -> int:
    """截一张图并落盘（window / desktop / region），只回路径与尺寸、不吐 base64。"""

    def _run() -> dict:
        driver = Config.load(env_path=args.env).driver()
        target = None if args.mode == "desktop" else _pick_target(driver, args)
        region = _parse_json_flag(args.region, "--region") if args.region else None
        options: dict = {}
        if args.max_dimension:
            options["max_dimension"] = args.max_dimension
        if args.allow_minimized:
            options["allow_minimized"] = True
        return screenshot_mod.run_screenshot(
            driver,
            mode=args.mode,
            target=target,
            out_dir=args.out_dir,
            region=region,
            options=options or None,
        )

    return _finish(_run)


def cmd_drag(args: argparse.Namespace) -> int:
    """拖拽：元素→元素 或 坐标→坐标（默认干跑，--execute 才真下发）。"""

    def _run() -> dict:
        driver = Config.load(env_path=args.env).driver()
        if args.from_element is not None and args.from_token is not None:
            raise WinUseError("--from-element 与 --from-token 只能给一个")
        if args.to_element is not None and args.to_token is not None:
            raise WinUseError("--to-element 与 --to-token 只能给一个")
        return drag_mod.run_drag(
            driver,
            target=_pick_target(driver, args),
            from_element=args.from_element if args.from_element is not None else args.from_token,
            to_element=args.to_element if args.to_element is not None else args.to_token,
            from_point=_parse_point(args.from_point, "--from-point") if args.from_point else None,
            to_point=_parse_point(args.to_point, "--to-point") if args.to_point else None,
            snapshot_id=args.snapshot,
            execute=bool(args.execute),
            confirm=bool(args.confirm),
            audit=audit_mod.env_sink(args.env, source="cli"),
        )

    return _finish(_run)


def cmd_app(args: argparse.Namespace) -> int:
    """应用生命周期：launch 启动 / list 枚举 / kill 结束（kill 需 --confirm；默认干跑）。"""

    def _run() -> dict:
        driver = Config.load(env_path=args.env).driver()
        options: dict = {}
        for key in ("running", "kind", "query", "limit"):
            value = getattr(args, key, None)
            if value is not None:
                options[key] = value
        # list 是只读枚举，核心按"所有动作默认干跑"的统一口径处理；门面这一层替用户
        # 补上 execute —— 否则跑一次 `winuse app --action list` 拿不到任何清单，
        # 而它本来就不改桌面状态。（真要干跑清单仍可反过来传 --execute false 语义；
        # kill/launch 不受此影响：仍必须显式 --execute。）
        execute = bool(args.execute) or args.action == "list"
        # --arg（可重复单值）与 --args（一次给多个）合并；前者专为以 "-" 开头的开关
        # 而设：argparse 会把 --args 之后以 "-" 开头的 token 当成选项，直接报
        # unrecognized arguments（T-60）。两者混用时 --arg 的值排在前面。
        launch_args = [*(args.arg or []), *(args.args or [])] or None
        return app_mod.run_app(
            driver,
            action=args.action,
            pid=args.pid,
            path=args.path,
            args=launch_args,
            confirm=bool(args.confirm),
            execute=execute,
            options=options or None,
            audit=audit_mod.env_sink(args.env, source="cli"),
        )

    return _finish(_run)


def cmd_search(args: argparse.Namespace) -> int:
    """Everything 全盘搜索（只读：不需要确认，不写危险动作审计）。"""
    kwargs: dict = {}
    if args.timeout is not None:
        kwargs["timeout_seconds"] = args.timeout
    return _finish(
        search_mod.run_search,
        query=args.query,
        regex=args.regex,
        path=args.path,
        max_results=args.max_results,
        sort=args.sort,
        min_size=args.min_size,
        max_size=args.max_size,
        es_path=args.es_path,
        **kwargs,
    )


def cmd_menu(args: argparse.Namespace) -> int:
    """调用应用菜单路径（恒判危险：未 --confirm 时连干跑计划都不给）。"""

    def _run() -> dict:
        driver = Config.load(env_path=args.env).driver()
        return menu_mod.run_menu(
            driver,
            target=_pick_target(driver, args),
            path=args.path,
            execute=bool(args.execute),
            confirm=bool(args.confirm),
            audit=audit_mod.env_sink(args.env, source="cli"),
        )

    return _finish(_run)


def cmd_verify(args: argparse.Namespace) -> int:
    """断言一组谓词（只读、无确认门；默认干跑只给将下发的参数）。"""

    def _run() -> dict:
        driver = Config.load(env_path=args.env).driver()
        predicates = [_parse_json_flag(item, "--predicate") for item in (args.predicate or [])]
        kwargs: dict = {}
        if args.stable_samples is not None:
            kwargs["stable_samples"] = args.stable_samples
        if args.timeout_ms is not None:
            kwargs["timeout_ms"] = args.timeout_ms
        return verify_mod.run_verify(
            driver,
            target=_pick_target(driver, args),
            predicates=predicates,
            execute=bool(args.execute),
            include_screenshot=bool(args.include_screenshot),
            audit=audit_mod.env_sink(args.env, source="cli"),
            **kwargs,
        )

    return _finish(_run)


# ------------------------------------------------------------------ P2 新能力：模糊查找 / 批量读值 / 剪贴板 / 会话状态


def _parse_element_ref(text: str) -> dict:
    """`index:0` / `token:s00000001:2` → `{"index": 0}` / `{"token": "s00000001:2"}`（只认这两种写法）。"""
    raw = str(text or "").strip()
    kind, _, rest = raw.partition(":")
    kind = kind.strip().lower()
    if kind in ("index", "i"):
        if not rest.strip().isdigit():
            raise WinUseError(f"--element {raw} 的索引必须是整数")
        return {"index": int(rest.strip())}
    if kind in ("token", "t"):
        if not rest.strip():
            raise WinUseError(f"--element {raw} 缺令牌值")
        return {"token": rest.strip()}
    raise WinUseError(f"--element {raw} 要写成 index:<序号> 或 token:<令牌>")


def cmd_find(args: argparse.Namespace) -> int:
    """模糊查找元素（容错）：回候选的索引/令牌/相似度 + 同源快照。"""

    def _run() -> dict:
        from .core import fuzzy as fuzzy_mod
        from .core.state import StateOptions, build_state

        driver = Config.load(env_path=args.env).driver()
        state = build_state(
            driver,
            StateOptions(
                app=args.app,
                pid=args.pid,
                window_id=args.window_id,
                allow_minimized=bool(args.allow_minimized),
            ),
        )
        elements = state.get("elements") or []
        kwargs: dict = {}
        if args.threshold is not None:
            kwargs["threshold"] = args.threshold
        if args.limit is not None:
            kwargs["limit"] = args.limit
        matches = fuzzy_mod.find_elements(elements, text=args.text, **kwargs)
        return {
            "ok": True,
            "snapshot_id": state.get("snapshot_id"),
            "elements_count": len(elements),
            "matches": matches,
            "count": len(matches),
        }

    return _finish(_run)


def cmd_read(args: argparse.Namespace) -> int:
    """批量读值（只读）：一次读多个元素的字段；`--element` 可重复。"""

    def _run() -> dict:
        from .core import read as read_mod

        driver = Config.load(env_path=args.env).driver()
        return read_mod.run_read(
            driver,
            target=_pick_target(driver, args),
            elements=[_parse_element_ref(item) for item in (args.element or [])],
            fields=args.field or None,
            snapshot_id=args.snapshot,
            options={"query": args.query} if args.query else None,
            audit=audit_mod.env_sink(args.env, source="cli"),
        )

    return _finish(_run)


def cmd_clipboard(args: argparse.Namespace) -> int:
    """读/写剪贴板（读正文与写都要 --confirm；write 真写入还要 --execute）。"""

    def _run() -> dict:
        from .core import clipboard as clip_mod

        driver = Config.load(env_path=args.env).driver()
        return clip_mod.run_clipboard(
            driver,
            action=args.action,
            include_text=bool(args.include_text),
            text=args.text,
            image_path=args.image_path,
            file_path=args.file_path,
            confirm=bool(args.confirm),
            execute=bool(args.execute),
            audit=audit_mod.env_sink(args.env, source="cli"),
        )

    return _finish(_run)


def cmd_session(args: argparse.Namespace) -> int:
    """会话/锁屏状态（只读，不需要驱动）：默认附各能力可用性矩阵。"""

    def _run() -> dict:
        from .core import session_state as ss_mod

        state = ss_mod.detect_session_state()
        out: dict = {"ok": True, **state}
        if not args.no_explain:
            out["availability"] = ss_mod.explain_action_availability(state)
        return out

    return _finish(_run)


def register(sub) -> None:
    """把能力挂到 CLI 子命令表上（由 cli.build_parser 调用；新增能力必须登记，否则门面漂移）。"""

    p_tools = sub.add_parser("tools", help="导出工具表（供各门面读取，单一出处）")
    p_tools.set_defaults(func=cmd_tools)

    p_audit = sub.add_parser("audit", help="审计日志查询：最近 N 条 / 轮转概况（只读）")
    p_audit.add_argument("--recent", type=int, default=20, help="返回最近 N 条（默认 20，连续跨轮转归档）")
    p_audit.add_argument("--path", default=None, help="覆盖审计文件路径（默认按 .env 的 WINUSE_AUDIT_PATH）")
    p_audit.add_argument("--stats", action="store_true", help="只看日志与轮转概况（大小/条数/归档）")
    p_audit.set_defaults(func=cmd_audit)

    p_ocr = sub.add_parser("ocr", help="原生 OCR：读出图片里的文字（Windows 自带引擎）")
    p_ocr.add_argument("--image", required=True, help="图片路径")
    p_ocr.add_argument("--language", default="zh-Hans", help="识别语言（默认 zh-Hans）")
    p_ocr.set_defaults(func=cmd_ocr)

    p_notify = sub.add_parser("notify", help="发一条 Windows 通知（默认干跑）")
    p_notify.add_argument("--title", default="", help="标题")
    p_notify.add_argument("--body", default="", help="正文")
    p_notify.add_argument(
        "--level",
        default="info",
        choices=["info", "warn", "warning", "error"],
        help="级别（warn 为规范值，warning 是它的别名）",
    )
    p_notify.add_argument(
        "--broadcast",
        action="store_true",
        help="广播给本机全部会话（默认只发当前会话）；需同时加 --confirm",
    )
    p_notify.add_argument(
        "--confirm",
        action="store_true",
        help="确认危险动作（当前只有 --broadcast 需要它，见 R-19）",
    )
    p_notify.add_argument("--timeout", type=int, default=10, help="停留秒数")
    p_notify.add_argument("--execute", action="store_true", help="真的发出去（默认只干跑）")
    p_notify.set_defaults(func=cmd_notify)

    p_system = sub.add_parser("system", help="系统盘点（CPU/内存/磁盘/电源/进程）")
    p_system.add_argument("--top", type=int, default=15, help="返回内存占用前 N 个进程")
    p_system.add_argument("--power-action", default=None, help="只返回电源动作意图：shutdown/restart/sleep/lock/logoff")
    p_system.add_argument("--confirm", action="store_true", help="确认（仅表示知情，仍只返回意图）")
    p_system.set_defaults(func=cmd_system)

    p_reg = sub.add_parser("registry", help="注册表只读；写只返回意图")
    p_reg.add_argument("--hive", default="HKCU", help="HKCU/HKLM 等根键")
    p_reg.add_argument("--path", default="", help=r"子路径（如 Software\Microsoft）")
    p_reg.add_argument("--value-name", default=None, help="只取某个值名")
    p_reg.add_argument("--recurse", action="store_true", help="递归列子键")
    p_reg.add_argument("--max-depth", type=int, default=3, help="递归深度上限")
    p_reg.add_argument("--max-subkeys", type=int, default=200, help="子键条数上限")
    p_reg.add_argument("--write", action="store_true", help="改成写入意图（不真写）")
    p_reg.add_argument("--delete", action="store_true", help="改成删除意图（不真删）")
    p_reg.add_argument("--value", default=None, help="写入的值")
    p_reg.add_argument("--value-type", default="REG_SZ", help="写入的值类型")
    p_reg.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "确认：写操作里仅表示知情（仍只返回意图，不真写）；"
            "读敏感区（HKLM\\SYSTEM\\CurrentControlSet、BCD00000000 等）时必须带才放行（R-11）"
        ),
    )
    p_reg.set_defaults(func=cmd_registry)

    p_vd = sub.add_parser("vdesktop", help="虚拟桌面探测（哪个桌面、是否当前桌面）")
    p_vd.add_argument("--window-id", type=int, default=None, help="窗口标识")
    p_vd.set_defaults(func=cmd_vdesktop)

    p_wait = sub.add_parser("wait", help="条件等待（超时是结果，不抛异常）")
    p_wait.add_argument("--kind", required=True, help="window_appear/window_gone/title_match/element_appear/text_appear")
    p_wait.add_argument("--app", default=None, help="按应用名或标题子串选窗口")
    p_wait.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_wait.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_wait.add_argument("--pattern", default=None, help="标题/文字匹配模式（element_gone 时按 label/value 子串寻址）")
    p_wait.add_argument("--element-index", type=int, default=None, help="元素索引（element_gone 用，须配 --snapshot）")
    p_wait.add_argument("--element-token", default=None, help="元素令牌（element_gone 用，与索引二选一）")
    p_wait.add_argument("--snapshot", default=None, help="元素索引的同源快照标识（用索引时必填）")
    p_wait.add_argument("--timeout", type=float, default=15.0, help="超时秒数")
    p_wait.add_argument("--interval", type=float, default=0.5, help="轮询间隔秒数")
    p_wait.add_argument("--seconds", type=float, default=None, help="纯延时的秒数（--kind sleep 用，例如 1.5；其余 kind 忽略）")
    p_wait.set_defaults(func=cmd_wait)

    p_multi = sub.add_parser("multi", help="多步动作批处理（批量输入需 --confirm；默认干跑）")
    p_multi.add_argument("--steps", required=True, help="步骤 JSON 数组")
    p_multi.add_argument("--app", default=None, help="按应用名或标题子串选窗口")
    p_multi.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_multi.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_multi.add_argument("--snapshot", default=None, help="快照标识")
    p_multi.add_argument("--execute", action="store_true", help="真的下发（默认只干跑）")
    p_multi.add_argument("--no-stop-on-error", action="store_true", help="单步失败后继续")
    p_multi.add_argument("--confirm", action="store_true", help="确认批量输入是危险动作")
    p_multi.set_defaults(func=cmd_multi)

    p_window = sub.add_parser("window", help="窗口动作（关闭类需 --confirm；默认干跑）")
    p_window.add_argument("--action", required=True, help="minimize/maximize/restore/close/move/resize/topmost")
    p_window.add_argument("--app", default=None, help="按应用名或标题子串选窗口")
    p_window.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_window.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_window.add_argument("--x", type=int, default=None, help="移动/缩放的横坐标")
    p_window.add_argument("--y", type=int, default=None, help="移动/缩放的纵坐标")
    p_window.add_argument("--width", type=int, default=None, help="宽度")
    p_window.add_argument("--height", type=int, default=None, help="高度")
    p_window.add_argument("--always-on-top", action="store_true", help="置顶")
    p_window.add_argument("--execute", action="store_true", help="真的下发（默认只干跑）")
    p_window.add_argument("--confirm", action="store_true", help="确认危险动作（关闭窗口等）")
    p_window.set_defaults(func=cmd_window)

    # ---- 新增能力：截图 / 拖拽 / 应用生命周期 / 全盘搜索 / 菜单 / 断言

    p_shot = sub.add_parser("screenshot", help="截图落盘（window/desktop/region），只读")
    p_shot.add_argument("--mode", required=True, choices=list(screenshot_mod.MODES), help="window / desktop / region")
    p_shot.add_argument("--app", default=None, help="按应用名或标题子串选窗口（desktop 模式不需要）")
    p_shot.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_shot.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_shot.add_argument("--region", default=None, help='矩形 JSON，例如 {"x1":0,"y1":0,"x2":400,"y2":300}')
    p_shot.add_argument("--out-dir", default=None, help="落盘目录（默认数据目录下 screenshots/）")
    p_shot.add_argument("--max-dimension", type=int, default=None, help="最长边上限（驱动等比缩小）")
    p_shot.add_argument("--allow-minimized", action="store_true", help="接受最小化窗口")
    p_shot.set_defaults(func=cmd_screenshot)

    p_drag = sub.add_parser("drag", help="拖拽：元素→元素 或 坐标→坐标（默认干跑）")
    p_drag.add_argument("--app", default=None, help="按应用名或标题子串选窗口")
    p_drag.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_drag.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_drag.add_argument("--from-element", type=int, default=None, help="起点元素索引（来自 state）")
    p_drag.add_argument("--to-element", type=int, default=None, help="终点元素索引（来自 state）")
    p_drag.add_argument("--from-token", default=None, help="起点元素令牌（来自 state，可免快照）")
    p_drag.add_argument("--to-token", default=None, help="终点元素令牌")
    p_drag.add_argument("--from-point", default=None, help="起点坐标 x,y（截图局部像素）")
    p_drag.add_argument("--to-point", default=None, help="终点坐标 x,y（截图局部像素）")
    p_drag.add_argument("--snapshot", default=None, help="快照标识（用元素索引时必填）")
    p_drag.add_argument("--execute", action="store_true", help="真的下发（默认只干跑）")
    p_drag.add_argument("--confirm", action="store_true", help="确认危险动作（系统级窗口等）")
    p_drag.set_defaults(func=cmd_drag)

    p_app = sub.add_parser("app", help="应用生命周期：launch / list / kill（默认干跑，kill 需 --confirm）")
    p_app.add_argument("--action", required=True, choices=sorted(app_mod.APP_ACTIONS), help="launch / list / kill")
    p_app.add_argument("--pid", type=int, default=None, help="kill 的目标进程号")
    p_app.add_argument("--path", default=None, help="launch 的可执行文件路径")
    p_app.add_argument("--args", nargs="*", default=None, help="launch 的附加参数（每个参数一项，不按空格切；以 - 开头的开关请改用 --arg）")
    p_app.add_argument("--arg", action="append", default=None, help="launch 的单个附加参数（可重复；给以 - 开头的开关时必须用等号形式：--arg=-NoProfile --arg=-File）")
    p_app.add_argument("--running", action="store_true", default=None, help="list：只看运行中的")
    p_app.add_argument("--kind", default=None, help="list：desktop / uwp")
    p_app.add_argument("--query", default=None, help="list：按名称子串过滤")
    p_app.add_argument("--limit", type=int, default=None, help="list：返回条数上限")
    p_app.add_argument("--execute", action="store_true", help="真的下发（list 自动真跑；launch/kill 必须显式给）")
    p_app.add_argument("--confirm", action="store_true", help="确认危险动作（kill、系统级应用）")
    p_app.set_defaults(func=cmd_app)

    p_search = sub.add_parser("search", help="Everything 全盘搜索（只读）")
    p_search.add_argument("--query", default=None, help="搜索词（支持通配符与 size:、ext: 等 Everything 语法）")
    p_search.add_argument("--regex", default=None, help="正则搜索（与 --query 二选一）")
    p_search.add_argument("--path", default=None, help="限定目录")
    p_search.add_argument("--max-results", type=int, default=50, help="结果条数上限（默认 50）")
    p_search.add_argument("--sort", default=None, help="排序（name/path/size/date-modified 等）")
    p_search.add_argument("--min-size", default=None, help="最小体积（如 10kb / 1mb）")
    p_search.add_argument("--max-size", default=None, help="最大体积")
    p_search.add_argument("--timeout", type=float, default=None, help="超时秒数（默认 15）")
    p_search.add_argument("--es-path", default=None, help="es.exe 路径（默认自动定位）")
    p_search.set_defaults(func=cmd_search)

    p_menu = sub.add_parser("menu", help="调用应用菜单路径（恒判危险：需 --confirm）")
    p_menu.add_argument("--app", default=None, help="按应用名或标题子串选窗口")
    p_menu.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_menu.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_menu.add_argument("--path", nargs="+", required=True, help="菜单路径，逐段给出（如：--path 文件 保存）")
    p_menu.add_argument("--execute", action="store_true", help="真的调用（默认只给计划）")
    p_menu.add_argument("--confirm", action="store_true", help="确认（不确认连计划都不给）")
    p_menu.set_defaults(func=cmd_menu)

    p_verify = sub.add_parser("verify", help="断言一组谓词（只读；默认干跑）")
    p_verify.add_argument("--app", default=None, help="按应用名或标题子串选窗口")
    p_verify.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_verify.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_verify.add_argument("--predicate", action="append", required=True, help='谓词 JSON，可重复（如 \'{"window":{"exists":true}}\'）')
    p_verify.add_argument("--include-screenshot", action="store_true", help="断言时附带截图")
    p_verify.add_argument("--stable-samples", type=int, default=None, help="稳定采样次数（默认 2）")
    p_verify.add_argument("--timeout-ms", type=int, default=None, help="超时毫秒（默认 5000）")
    p_verify.add_argument("--execute", action="store_true", help="真的断言（默认只给计划）")
    p_verify.set_defaults(func=cmd_verify)

    # ---- P2 新能力：模糊查找 / 批量读值 / 剪贴板 / 会话状态

    p_find = sub.add_parser("find", help="模糊查找元素（容错），回索引/令牌/相似度 + 同源快照")
    p_find.add_argument("--text", required=True, help="要找的文本")
    p_find.add_argument("--app", default=None, help="按应用名或标题子串选窗口")
    p_find.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_find.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_find.add_argument("--threshold", type=float, default=None, help="相似度下限（缺省用核心默认）")
    p_find.add_argument("--limit", type=int, default=None, help="候选上限（缺省 10）")
    p_find.add_argument("--allow-minimized", action="store_true", help="接受最小化窗口")
    p_find.set_defaults(func=cmd_find)

    p_read = sub.add_parser("read", help="批量读值：一次读多个元素的字段（--element 可重复）")
    p_read.add_argument("--element", action="append", default=[], help="要读的元素 index:0 或 token:<令牌>（可重复）")
    p_read.add_argument("--field", action="append", default=[], help="要读的字段（可重复，缺省全部可读字段）")
    p_read.add_argument("--snapshot", default=None, help="按索引寻址时的同源快照标识")
    p_read.add_argument("--app", default=None, help="按应用名或标题子串选窗口")
    p_read.add_argument("--pid", type=int, default=None, help="按进程号选窗口")
    p_read.add_argument("--window-id", type=int, default=None, help="按窗口标识选窗口")
    p_read.add_argument("--query", default=None, help="只取匹配该子串的元素＋祖先链")
    p_read.set_defaults(func=cmd_read)

    p_clip = sub.add_parser("clipboard", help="读/写剪贴板（读正文与写都需 --confirm）")
    p_clip.add_argument("--action", required=True, choices=["read", "write"], help="read / write")
    p_clip.add_argument("--include-text", action="store_true", help="read：取剪贴板正文（需 --confirm）")
    p_clip.add_argument("--text", default=None, help="write：写入的纯文本")
    p_clip.add_argument("--image-path", default=None, help="write：图片的绝对路径")
    p_clip.add_argument("--file-path", default=None, help="write：文件的绝对路径")
    p_clip.add_argument("--confirm", action="store_true", help="确认（读正文 / 写剪贴板都必须）")
    p_clip.add_argument("--execute", action="store_true", help="write：真写入（否则干跑）")
    p_clip.set_defaults(func=cmd_clipboard)

    p_sess = sub.add_parser("session", help="会话/锁屏状态（只读，不需要驱动）")
    p_sess.add_argument("--no-explain", action="store_true", help="不附各能力可用性矩阵")
    p_sess.set_defaults(func=cmd_session)
