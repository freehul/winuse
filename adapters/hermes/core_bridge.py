"""WinUse 核心桥接：门面只做协议翻译，业务逻辑一律回到项目核心。

为什么走子进程而不是 import：
- **单一实现**：核心只有一份（`<项目>/winuse/`），插件不 vendored 一份副本，
  避免"改了一处忘了另一处"的漂移（同类项目里多份实现漂移的教训）。
- **零安装**：插件部署副本可以随时重建，核心仍随项目 git 管理。
代价：每次调用多一次 Python 启动（约 150–300ms，T-16 有实测）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
DEFAULT_TIMEOUT_S = 120.0


def _load_limit():
    """加载核心的共享限流器（R-20）。加载失败返回 None，由调用方给出可读报错。"""
    root = project_root()
    if root is not None:
        text = str(root)
        if text not in sys.path:
            sys.path.insert(0, text)
    try:
        from winuse import limit as limit_mod  # noqa: PLC0415

        return limit_mod
    except ImportError:  # 部署位没写 _location.py / 核心被挪走
        return None


def _strict_bool(value, *, field: str, default: bool = False):
    """严格布尔校验（C-1）：桥接把参数拼成 CLI 命令行，`if args.get("execute")`
    会把 `"false"` 当真 → 干跑门/确认门被绕过。校验规则只有核心一份
    （`winuse.core.params`），这里不复制——复制必然漂移。

    核心不可达时（部署位没写 `_location.py`）直接抛：本模块后续的 `call_core`
    同样找不到核心，早失败比静默放行好。
    """
    root = project_root()
    if root is not None:
        text = str(root)
        if text not in sys.path:
            sys.path.insert(0, text)
    try:
        from winuse.core.params import strict_bool  # noqa: PLC0415
    except ImportError as exc:  # 部署位没写 _location.py / 核心被挪走
        raise RuntimeError(
            f"找不到 WinUse 核心，无法校验参数 {field}（重跑 adapters/hermes/install.py 可修复）"
        ) from exc

    return strict_bool(value, field=field, default=default)


def _discover_from_repo() -> str | None:
    """在项目内直跑（未部署）时，从本文件位置向上找核心——**不硬编码机器路径**。"""
    for base in Path(__file__).resolve().parents:
        if (base / "winuse" / "__init__.py").is_file():
            return str(base)
    return None


def project_root() -> Path | None:
    """定位核心所在的项目根。

    顺序：部署时写入的 `_location.py`（部署副本不带环境依赖）→ 环境变量 → 项目内向上探测。
    都找不到返回 None，由调用方给出可读报错。
    """
    try:
        from ._location import PROJECT_ROOT  # type: ignore

        if PROJECT_ROOT:
            return Path(PROJECT_ROOT)
    except Exception:  # noqa: BLE001 - 未部署时属正常
        pass
    for candidate in (os.environ.get("WINUSE_ROOT"), _discover_from_repo()):
        if candidate and (Path(candidate) / "winuse" / "__init__.py").is_file():
            return Path(candidate)
    return None


# _load_limit() 只在模块级执行一次；因为要用到 project_root，必须放在它定义之后
_LIMIT = _load_limit()
#: 与核心同源的预算与限流实现（核心不可达时退化为不限流，`call_core` 照常工作）
MAX_RESULT_CHARS = _LIMIT.MAX_RESULT_CHARS if _LIMIT is not None else 24000
limit_payload = _LIMIT.limit_payload if _LIMIT is not None else (lambda payload, limit=MAX_RESULT_CHARS: payload)


def _join_modifiers(value) -> str | None:
    """修饰符归一成 CLI 的逗号串：数组 → "ctrl,shift"，字符串原样，空 → None（不拼参数）。

    为什么不信任调用方：MCP 客户端按工具表会传数组，而命令行只吃逗号串。
    桥接是这两者之间的唯一翻译点——任一侧变了都得在这里收敛，
    否则参数会**静默丢失**（P0-2 的另一半）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple)):
        parts = [str(m).strip() for m in value if m is not None and str(m).strip()]
        return ",".join(parts) if parts else None
    return str(value)


def _join_point(value) -> str | None:
    """点 → CLI 的 `x,y` 文本：接受 `{"x","y"}` 或 `[x, y]`。

    缺一条边时返回 None（**不拼半截坐标**）：让核心收到"没有坐标"并报它自己的
    参数错，胜过桥接在这里造一个 `None,None` 让下游去猜。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        x, y = value.get("x"), value.get("y")
        if x is None or y is None:
            return None
        return f"{x},{y}"
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{value[0]},{value[1]}"
    return str(value)


def _element_ref_text(item) -> str:
    """`{"index": 0}` / `{"token": "s1:2"}` → CLI 的 `index:0` / `token:s1:2`（令牌优先，与核心寻址口径一致）。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        token = item.get("token")
        if token is not None:
            return f"token:{token}"
        index = item.get("index")
        if index is not None:
            return f"index:{index}"
    return str(item)


def build_args(tool: str, args: dict) -> list[str]:
    """纯函数：把工具参数翻译成核心 CLI 参数。参数名只在这里出现一次。"""
    out: list[str] = []

    def add(key: str, value, flag: str | None = None) -> None:
        """非布尔字段：原样拼值。（布尔开关一律走下面的 `flag()`，不走这里——
        按值猜"这是不是布尔"会把 `max_depth=1` 误判成开关。）"""
        if value is None:
            return
        out.extend([flag or f"--{key.replace('_', '-')}", str(value)])

    def flag(key: str) -> None:
        """布尔开关：交给核心的严格校验，非原生布尔直接拒（C-1）。"""
        if _strict_bool(args.get(key), field=key):
            out.append(f"--{key.replace('_', '-')}")

    if tool == "winuse_state":
        out.append("state")
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        add("query", args.get("query"))
        add("max_elements", args.get("max_elements"), "--max")
        flag("frames")
        flag("allow_minimized")
        flag("no_windows")
        flag("include_tree_hash")
        return out

    if tool == "winuse_act":
        out.append("act")
        add("do", args.get("action"), "--do")
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        flag("allow_minimized")
        add("index", args.get("element_index"), "--index")
        add("token", args.get("element_token"), "--token")
        add("x", args.get("x"), "--x")
        add("y", args.get("y"), "--y")
        add("snapshot", args.get("snapshot"))
        add("value", args.get("value"))
        add("text", args.get("text"))
        add("key", args.get("key"))
        add("modifiers", _join_modifiers(args.get("modifiers")), "--modifiers")
        add("direction", args.get("direction"))
        add("amount", args.get("amount"))
        add("by", args.get("by"))
        add("button", args.get("button"))
        add("count", args.get("count"))
        flag("execute")  # 默认干跑：只有显式要求才真动
        flag("confirm")  # 危险动作确认门（核心 guard 判定）
        flag("overwrite")  # 覆盖既有内容授权（核心写值门禁）
        flag("allow_unverified")  # 第二把钥匙（R-3）：与 overwrite 同时给才放行 unverified 写入
        return out

    if tool == "winuse_audit":
        out.append("audit")  # 只读查询：最近 N 条 / 轮转概况
        add("recent", args.get("recent"), "--recent")
        flag("stats")
        return out

    if tool == "winuse_display":
        out.append("display")
        flag("no_windows")
        return out

    # ---- PR-8 新增 8 个能力：工具参数 → 核心 CLI 参数（参数名只在这里出现一次）

    if tool == "winuse_ocr":
        out.append("ocr")
        add("image_path", args.get("image_path"), "--image")
        add("language", args.get("language"))
        return out

    if tool == "winuse_notify":
        out.append("notify")
        add("title", args.get("title"))
        add("body", args.get("body"))
        add("level", args.get("level"))
        add("timeout_seconds", args.get("timeout_seconds"), "--timeout")
        flag("execute")
        flag("broadcast")  # R-19：广播给全部会话（核心另有确认门）
        flag("confirm")
        return out

    if tool == "winuse_system":
        out.append("system")
        add("top_processes", args.get("top_processes"), "--top")
        add("power_action", args.get("power_action"))
        flag("confirm")
        return out

    if tool == "winuse_registry":
        out.append("registry")
        add("hive", args.get("hive"))
        add("path", args.get("path"))
        add("value_name", args.get("value_name"))
        flag("recurse")
        add("max_depth", args.get("max_depth"))
        add("max_subkeys", args.get("max_subkeys"))
        flag("write")
        flag("delete")
        add("value", args.get("value"))
        add("value_type", args.get("value_type"))
        flag("confirm")
        return out

    if tool == "winuse_virtual_desktop":
        out.append("vdesktop")
        add("window_id", args.get("window_id"))
        return out

    if tool == "winuse_wait":
        out.append("wait")
        add("kind", args.get("kind"))
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        add("pattern", args.get("pattern"))
        add("element_index", args.get("element_index"), "--element-index")
        add("element_token", args.get("element_token"), "--element-token")
        add("snapshot", args.get("snapshot"))
        add("timeout_seconds", args.get("timeout_seconds"), "--timeout")
        add("interval_seconds", args.get("interval_seconds"), "--interval")
        return out

    if tool == "winuse_multi":
        out.append("multi")
        steps = args.get("steps")
        if steps is not None:
            add("steps", steps if isinstance(steps, str) else json.dumps(steps, ensure_ascii=False))
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        add("snapshot", args.get("snapshot"))
        flag("confirm")
        flag("execute")
        if not _strict_bool(args.get("stop_on_error"), field="stop_on_error", default=True):
            out.append("--no-stop-on-error")
        return out

    if tool == "winuse_window":
        out.append("window")
        add("action", args.get("action"), "--action")
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        add("x", args.get("x"))
        add("y", args.get("y"))
        add("width", args.get("width"))
        add("height", args.get("height"))
        flag("always_on_top")
        flag("confirm")
        flag("execute")
        return out

    # ---- 新增能力：截图 / 拖拽 / 应用生命周期 / 全盘搜索 / 菜单 / 断言

    if tool == "winuse_screenshot":
        out.append("screenshot")
        add("mode", args.get("mode"))
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        region = args.get("region")
        if isinstance(region, str) or region is None:
            add("region", region)
        else:
            add("region", json.dumps(region, ensure_ascii=False))
        add("out_dir", args.get("out_dir"))
        add("max_dimension", args.get("max_dimension"))
        flag("allow_minimized")
        return out

    if tool == "winuse_drag":
        out.append("drag")
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        add("from_element", args.get("from_element"), "--from-element")
        add("to_element", args.get("to_element"), "--to-element")
        add("from_token", args.get("from_token"), "--from-token")
        add("to_token", args.get("to_token"), "--to-token")
        add("from_point", _join_point(args.get("from_point")), "--from-point")
        add("to_point", _join_point(args.get("to_point")), "--to-point")
        add("snapshot", args.get("snapshot"))
        flag("execute")
        flag("confirm")
        return out

    if tool == "winuse_app":
        out.append("app")
        add("action", args.get("action"), "--action")
        add("pid", args.get("pid"))
        add("path", args.get("path"))
        extra = args.get("args")
        if isinstance(extra, (list, tuple)) and extra:
            # 逐项走 --arg=<值>（等号形式）：--args 是 nargs="*"，把以 "-" 开头的开关
            # 交给它时 argparse 会当成选项、直接报 unrecognized arguments；--arg 用空格
            # 分隔也一样被拒（argument --arg: expected one argument）。等号形式则不会
            # 被当成选项解析，是唯一两种写法都安全的路子（T-60）。
            for item in extra:
                out.append(f"--arg={item}")
        elif isinstance(extra, str) and extra:
            out.append(f"--arg={extra}")
        add("running", args.get("running"))
        add("kind", args.get("kind"))
        add("query", args.get("query"))
        add("limit", args.get("limit"))
        flag("confirm")
        flag("execute")
        return out

    if tool == "winuse_search":
        out.append("search")
        add("query", args.get("query"))
        add("regex", args.get("regex"))
        add("path", args.get("path"))
        add("max_results", args.get("max_results"), "--max-results")
        add("sort", args.get("sort"))
        add("min_size", args.get("min_size"), "--min-size")
        add("max_size", args.get("max_size"), "--max-size")
        add("timeout_seconds", args.get("timeout_seconds"), "--timeout")
        add("es_path", args.get("es_path"), "--es-path")
        return out

    if tool == "winuse_menu":
        out.append("menu")
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        path = args.get("path")
        if isinstance(path, str):
            out.extend(["--path", path])
        elif isinstance(path, (list, tuple)) and path:
            out.append("--path")
            out.extend(str(segment) for segment in path)
        flag("execute")
        flag("confirm")
        return out

    if tool == "winuse_verify":
        out.append("verify")
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        predicates = args.get("predicates")
        if isinstance(predicates, str):
            out.extend(["--predicate", predicates])
        elif isinstance(predicates, (list, tuple)):
            for item in predicates:
                out.extend(
                    ["--predicate", item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)]
                )
        flag("include_screenshot")
        add("stable_samples", args.get("stable_samples"), "--stable-samples")
        add("timeout_ms", args.get("timeout_ms"), "--timeout-ms")
        flag("execute")
        return out

    # ---- P2 新能力：模糊查找 / 批量读值 / 剪贴板 / 会话状态

    if tool == "winuse_find":
        out.append("find")
        add("text", args.get("text"), "--text")
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        add("threshold", args.get("threshold"))
        add("limit", args.get("limit"))
        flag("allow_minimized")
        return out

    if tool == "winuse_read":
        out.append("read")
        elements = args.get("elements")
        if isinstance(elements, (list, tuple)):
            for item in elements:
                out.extend(["--element", _element_ref_text(item)])
        elif isinstance(elements, str) and elements:
            out.extend(["--element", elements])
        fields = args.get("fields")
        if isinstance(fields, (list, tuple)):
            for name in fields:
                out.extend(["--field", str(name)])
        elif isinstance(fields, str) and fields:
            out.extend(["--field", fields])
        add("snapshot", args.get("snapshot"))
        add("app", args.get("app"))
        add("pid", args.get("pid"))
        add("window_id", args.get("window_id"))
        add("query", args.get("query"))
        flag("allow_minimized")
        return out

    if tool == "winuse_clipboard":
        out.append("clipboard")
        add("action", args.get("action"), "--action")
        flag("include_text")
        add("text", args.get("text"))
        add("image_path", args.get("image_path"))
        add("file_path", args.get("file_path"))
        flag("confirm")
        flag("execute")
        add("max_text_chars", args.get("max_text_chars"), "--max-text-chars")
        return out

    if tool == "winuse_session":
        out.append("session")
        # explain 缺省是 true（附可用性矩阵）→ 只有显式 false 才加 --no-explain
        if args.get("explain") is False:
            out.append("--no-explain")
        return out

    if tool == "__catalog__":
        # 专用通道：让门面读核心工具表（单一出处，不复制 schema）
        return ["tools"]

    raise ValueError(f"未知的 WinUse 工具：{tool}")


def catalog_json(timeout_s: float = 30.0) -> str:
    """取核心工具表 JSON（各门面共享同一份，避免口径漂移）。"""
    return call_core("__catalog__", {}, timeout_s=timeout_s)


def call_core(tool: str, args: dict, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """执行核心 CLI 并把它输出的 JSON 原样返回（作为工具的 JSON 字符串结果）。

    失败也返回结构化 JSON（带稳定 code），**不抛异常**——让上层 agent 能读 code 决策。
    """
    try:
        argv = build_args(tool, args)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": str(exc)}},
                          ensure_ascii=False)

    root = project_root()
    if root is None:
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "CORE_NOT_FOUND",
                    "message": "找不到 WinUse 核心目录",
                    "detail": "重跑 adapters/hermes/install.py 写入部署位置，或设置 WINUSE_ROOT",
                },
            },
            ensure_ascii=False,
        )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    # 子进程输出按 UTF-8 写（下面也按 UTF-8 解），别让 Windows 本地编码插手
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        cp = subprocess.run(
            [sys.executable, "-m", "winuse", *argv],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout_s),
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"ok": False, "error": {"code": "CORE_TIMEOUT", "message": f"核心调用超时（{timeout_s}s）"}},
            ensure_ascii=False,
        )
    except OSError as exc:
        return json.dumps(
            {"ok": False, "error": {"code": "CORE_SPAWN_FAILED", "message": str(exc)}},
            ensure_ascii=False,
        )

    text = (cp.stdout or "").strip()
    if not text:
        detail = (cp.stderr or "").strip()[:400]
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "CORE_EMPTY_OUTPUT",
                    "message": f"核心没有输出（退出码 {cp.returncode}）",
                    "detail": detail,
                },
            },
            ensure_ascii=False,
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "CORE_BAD_JSON",
                    "message": f"核心输出不是合法 JSON：{exc}",
                    "detail": text[:400],
                },
            },
            ensure_ascii=False,
        )
    payload.setdefault("_bridge", {"exit_code": cp.returncode, "via": "subprocess"})
    # 复用**同一份**限流器（R-20）：插件的回包同样进 agent 上下文，超长会让客户端出问题；
    # 各门面各写一份预算必然漂移，所以直接用核心 `winuse/limit.py` 的实现。
    if isinstance(payload, dict):
        payload = limit_payload(payload)
    return json.dumps(payload, ensure_ascii=False)
