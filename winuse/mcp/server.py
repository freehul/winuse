"""MCP stdio 服务端：读一行 → 分派 → 写一行。

分层：
- `Session`：一次连接的状态（驱动、配置、是否已 initialized）
- `handle_message`：纯分派函数（可单测，注入假驱动即可）
- `serve`：主循环（默认读写真实 stdio；也可传内存流测试）

错误分档（照官方 tools 规范）：
- 参数级问题（缺 action、未知工具、非法动作名）→ JSON-RPC error（-32602）
- 运行期失败（驱动报错、窗口找不到）→ 工具执行错误：result.isError = true + 结构化 JSON
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import __version__, limit as limit_mod
from ..config import Config
from ..core.act import (
    ACTION_TOOLS,
    MOUSE_BUTTONS,
    SCROLL_GRANULARITIES,
    ActSpec,
    run_act,
)
from ..core.app import APP_ACTIONS
from ..core.screenshot import MODES as SHOT_MODES
from ..core.wait_for import KINDS as WAIT_KINDS
from ..core.desktop import WINDOW_ACTIONS
from ..core.display import describe
from ..core.params import InvalidParams, strict_bool
from ..core.registry import HIVES
from ..core.state import StateOptions, build_state, pick_window
from ..driver.errors import (
    MissingParam,
    UnknownAction,
    WinUseError,
)
from . import protocol as P

SERVER_NAME = "winuse"
SERVER_VERSION = __version__

#: 交给客户端的用法提示（规范里的可选 instructions 字段）
INSTRUCTIONS = (
    "WinUse 是本机桌面能力的文本优先门面。推荐流程：先 winuse_state 拿到元素索引与 snapshot_id，"
    "再用 winuse_act 按元素索引动作（必须带上同源的 snapshot）；winuse_act 默认只干跑，"
    "需要真正执行时显式传 execute=true。显示器与缩放用 winuse_display 取，禁止用全局缩放估算坐标。"
)

#: 参数级错误 → 走 JSON-RPC 协议错误（其余 WinUseError → 工具执行错误）
#: `InvalidParams` 在这里的意义：布尔字段收到字符串（如 execute="false"）是调用方
#: 的序列化错误，重试没用、必须改参数 → 给 -32602 让客户端当场看见（C-1）。
_PROTOCOL_LEVEL_ERRORS = (MissingParam, UnknownAction, InvalidParams, ValueError)


def _state_options(args: dict, cfg: Any | None) -> StateOptions:
    opts: dict[str, Any] = {
        "app": args.get("app"),
        "pid": args.get("pid"),
        "window_id": args.get("window_id"),
        "allow_minimized": strict_bool(args.get("allow_minimized"), field="allow_minimized"),
        "include_frames": strict_bool(args.get("frames"), field="frames"),
        "include_windows": not strict_bool(args.get("no_windows"), field="no_windows"),
        "query": args.get("query"),
        "foreground_window_id": _foreground_hint(),
    }
    # `max_elements` 只在**真给了值**时才传：显式传 None 会盖掉 `StateOptions` 的核心默认（200），
    # 而核心侧取的是 `int(opts.max_elements or 0)` → 变成「上限 0 个元素」→ 整棵元素树被裁光。
    # 触发条件不苛刻：任何没带配置对象的调用方（cfg=None）都会踩到。
    max_elements = args.get("max_elements")
    if max_elements is None and cfg is not None:
        max_elements = getattr(cfg, "state_max_elements", None)
    if max_elements is not None:
        opts["max_elements"] = max_elements
    if strict_bool(args.get("include_tree_hash"), field="include_tree_hash"):
        opts["include_tree_hash"] = True
    return StateOptions(**opts)


def _foreground_hint() -> int | None:
    """系统真前台窗口（GetForegroundWindow）；失败返回 None，不阻断主流程。"""
    try:
        from ..core.display import foreground_window

        fw = foreground_window()
        return fw.get("window_id") if fw else None
    except Exception:  # noqa: BLE001
        return None


def _error_payload(exc: Exception) -> dict:
    """运行期错误 → 对外结构（R-20：稳定 code + 短消息 + **脱敏** detail）。

    原先直接把异常文本放进响应：驱动 / PowerShell / 子进程的错误里常带本机路径，
    这些字符串会进入 agent 上下文（样例即泄露）。
    """
    from ..audit import redact_text

    if isinstance(exc, WinUseError):
        payload = exc.to_dict()
        if payload.get("message"):
            payload["message"] = redact_text(payload["message"], limit=200)
        if payload.get("detail"):
            payload["detail"] = redact_text(payload["detail"], limit=300)
        return {"ok": False, "error": payload}
    return {
        "ok": False,
        "error": {
            "code": type(exc).__name__,
            "message": redact_text(str(exc), limit=200),
        },
    }


# ------------------------------------------------------------------ 工具实现


def _tool_state(session: "Session", args: dict) -> dict:
    _require_driver(session)
    # 指纹由**核心**在裁剪/投影之前算（整窗口径）并随回执带口径；门面只把开关翻译下去。
    # 门面自己拿 out["elements"] 再算一遍是错的——那串已被 query 与元素上限改过，
    # 据此算出的指纹会把「参数变了」误报成「界面变了」。
    return build_state(session.driver, _state_options(args, session.cfg))


def _tool_display(session: "Session", args: dict) -> dict:
    _require_driver(session)
    out = describe(session.driver)
    if strict_bool(args.get("no_windows"), field="no_windows"):
        out.pop("windows", None)
    return out


def _act_modifiers(value: Any) -> list[str]:
    """组合键修饰符归一：既接受 MCP 数组（["ctrl","shift"]），也接受逗号串（"ctrl,shift"）。

    历史坑（P0-2 的隐蔽面）：这里原先只做 `str(value).split(",")`——客户端按工具表
    传数组时会被 `str()` 成字符串 `"['ctrl']"`，再按逗号切出一个**看起来像修饰符、
    实际永远匹配不上驱动按键表**的脏值，而且一路静默下发。两种形态都在这里归一；
    非法类型直接抛参数级错误（重试无用，必须改参数）。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [m.strip() for m in value.split(",") if m.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    raise InvalidParams(
        "modifiers 必须是字符串数组或逗号分隔字符串",
        f"收到类型：{type(value).__name__}",
    )


def _tool_act(session: "Session", args: dict) -> dict:
    action = args.get("action")
    if not action:
        raise MissingParam("winuse_act 必填参数：action")
    # 布尔字段先解析再动驱动：参数错就别浪费一次驱动调用，也不要让"先取状态、
    # 后判布尔"的顺序给调用方留下"已经碰过桌面"的错觉（C-1）
    allow_minimized = strict_bool(args.get("allow_minimized"), field="allow_minimized")
    execute = strict_bool(args.get("execute"), field="execute")
    confirm = strict_bool(args.get("confirm"), field="confirm")
    overwrite = strict_bool(args.get("overwrite"), field="overwrite")
    allow_unverified = strict_bool(args.get("allow_unverified"), field="allow_unverified")
    _require_driver(session)

    windows = (session.driver.call("list_windows", {}) or {}).get("windows") or []
    target = pick_window(
        windows,
        StateOptions(
            app=args.get("app"),
            pid=args.get("pid"),
            window_id=args.get("window_id"),
            allow_minimized=allow_minimized,
            foreground_window_id=_foreground_hint(),
        ),
    )
    spec = ActSpec(
        action=str(action),
        element_index=args.get("element_index"),
        element_token=args.get("element_token"),
        value=args.get("value"),
        text=args.get("text"),
        key=args.get("key"),
        modifiers=_act_modifiers(args.get("modifiers")),
        direction=args.get("direction"),
        amount=args.get("amount", 3),
        by=str(args.get("by") or "line"),
        button=str(args.get("button") or "left"),
        count=args.get("count", 1),
    )
    return run_act(
        session.driver,
        spec,
        target,
        args.get("snapshot"),
        execute=execute,
        confirm=confirm,
        overwrite=overwrite,
        allow_unverified=allow_unverified,
        audit=session.audit_sink(),
    )


def _require_driver(session: "Session") -> None:
    if session.driver is None:
        from ..driver.errors import DriverNotFound

        raise DriverNotFound("驱动不可用（配置缺失或 cua-driver 未安装）")


# ------------------------------------------------------------------ 工具表


def _object_schema(properties: dict, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


#: 动作枚举**由核心生成**（R-4）：手写一份必然与 `core.act.ACTION_TOOLS` 漂移
#: ——之前的 type_text / press_key / bring_to_front / focus 就是这么来的，
#: 客户端照 schema 生成的调用会稳定失败。工具表只翻译，不定名。
ACTIONS = tuple(sorted(ACTION_TOOLS))

#: 窗口动作同理（原先把 move/resize/topmost 写在这里，核心只认 move_resize/always_on_top）
WINDOW_ACTION_CHOICES = tuple(sorted(WINDOW_ACTIONS))

#: 注册表根键同理（原先描述里有核心不接受的 HKCC）
HIVE_CHOICES = tuple(HIVES)


def tool_catalog() -> list[dict]:
    """MCP 工具表：字段名照官方规范（name / description / inputSchema）。"""
    return [
        {
            "name": "winuse_state",
            "description": (
                "取一份文本优先的本机界面状态：目标窗口的可交互元素（带 element_index、element_token、"
                "snapshot_id）、窗口清单与真前台窗口。默认不下发截图，并已裁掉只读文本元素。"
                "元素索引只在与它同源的 snapshot_id 内有效；窗口很大时用 query 只取匹配元素（体量小一个数量级）。"
            ),
            "inputSchema": _object_schema(
                {
                    "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口"},
                    "pid": {"type": "integer", "description": "按进程号选窗口"},
                    "window_id": {"type": "integer", "description": "按窗口标识选窗口（最精确）"},
                    "query": {"type": "string", "description": "只取匹配该子串的元素＋祖先链（大窗口省钱关键）"},
                    "max_elements": {
                        "type": "integer", "minimum": 0, "maximum": limit_mod.MAX_STATE_ELEMENTS,
                        "description": (
                            f"最多返回多少元素（上限 {limit_mod.MAX_STATE_ELEMENTS}）；"
                            "窗口很大时配合 query 按需投影更省"
                        ),
                    },
                    "allow_minimized": {"type": "boolean", "description": "接受最小化窗口"},
                    "no_windows": {"type": "boolean", "description": "不返回窗口清单"},
                }
            ),
        },
        {
            "name": "winuse_act",
            "description": (
                "按元素索引下发一个界面动作（click / set_value / type / key / scroll 等，"
                "完整可选值见 action 的 enum）。"
                "用元素索引时必须带上来自 winuse_state 的同源 snapshot。默认只干跑，"
                "只有 execute=true 才真正执行。"
            ),
            "inputSchema": _object_schema(
                {
                    "action": {"type": "string", "enum": list(ACTIONS), "description": "动作名"},
                    "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口"},
                    "pid": {"type": "integer", "description": "按进程号选窗口"},
                    "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                    "element_index": {"type": "integer", "description": "元素索引（来自 winuse_state）"},
                    "element_token": {"type": "string", "description": "元素令牌（来自 winuse_state，可免快照）"},
                    "snapshot": {"type": "string", "description": "快照标识（用索引时必填）"},
                    "value": {"type": "string", "description": "set_value 的新值"},
                    "text": {"type": "string", "description": "type 动作要输入的文字"},
                    "key": {"type": "string", "description": "key 动作的按键名"},
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "组合键修饰符（ctrl/shift/alt/win），配 key 动作使用："
                            "key=\"c\" + modifiers=[\"ctrl\"] = 复制。点击动作会把它作为 modifier 下发。"
                            "也接受逗号分隔字符串（\"ctrl,shift\"）"
                        ),
                    },
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "scroll 方向"},
                    "amount": {"type": "integer", "description": "scroll 刻度数（默认 3）"},
                    "by": {
                        "type": "string",
                        "enum": list(SCROLL_GRANULARITIES),
                        "description": "scroll 粒度：line=按行（默认）、page=按页（长文档翻页）",
                    },
                    "button": {
                        "type": "string",
                        "enum": list(MOUSE_BUTTONS),
                        "description": "鼠标键：left=左键（默认）、right=右键（调出上下文菜单）、middle=中键",
                    },
                    "count": {"type": "integer", "description": "点击次数（2=双击）"},
                    "x": {
                        "type": "integer",
                        "description": "坐标寻址：窗口内截图像素横坐标（与 y 成对；canvas/自绘控件等无元素可寻址时用；与元素寻址互斥）",
                    },
                    "y": {"type": "integer", "description": "坐标寻址：窗口内截图像素纵坐标（须与 x 一起给）"},
                    "allow_minimized": {"type": "boolean", "description": "接受最小化窗口"},
                    "execute": {"type": "boolean", "description": "true 才真正执行；默认 false 只干跑"},
                    "confirm": {
                        "type": "boolean",
                        "description": "确认危险动作（结束进程／系统级窗口／关闭类动作）；不带则返回 CONFIRM_REQUIRED 且不下发",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": (
                            "显式授权覆盖目标元素的既有内容（set_value 是整段替换）；"
                            "不带时若目标非空则返回 EXISTING_CONTENT 且不下发"
                        ),
                    },
                    "allow_unverified": {
                        "type": "boolean",
                        "description": (
                            "第二把钥匙：只有与 overwrite=true 同时给才放行「读不到既有内容」的写入"
                            "（自绘/虚拟列表等特殊控件）。单独给无效——那不等于你接受覆盖内容；"
                            "不带且读不到内容时返回 WRITE_GATE_UNVERIFIED 且不下发"
                        ),
                    },
                },
                required=["action"],
            ),
        },
        {
            "name": "winuse_display",
            "description": (
                "取多显示器事实：每块屏的物理像素矩形、工作区、各屏独立缩放比与 DPI，"
                "以及每个窗口落在哪块屏。注意各屏缩放可能不同，禁止用全局缩放换算坐标。"
            ),
            "inputSchema": _object_schema({"no_windows": {"type": "boolean", "description": "只看显示器，不列窗口"}}),
        },
        *_NEW_TOOLS,
    ]


TOOL_NAMES = (
    "winuse_state",
    "winuse_act",
    "winuse_display",
    "winuse_ocr",
    "winuse_notify",
    "winuse_system",
    "winuse_registry",
    "winuse_virtual_desktop",
    "winuse_wait",
    "winuse_multi",
    "winuse_window",
    "winuse_audit",
    "winuse_screenshot",
    "winuse_drag",
    "winuse_app",
    "winuse_search",
    "winuse_menu",
    "winuse_verify",
    "winuse_find",
    "winuse_read",
    "winuse_clipboard",
    "winuse_session",
)

#: PR-8 新增 8 个能力的工具表 + T-26 的审计查询（字段名照官方规范：name / description / inputSchema）
_NEW_TOOLS: list[dict] = [
    {
        "name": "winuse_audit",
        "description": (
            "读 WinUse 自己的审计日志（**只读**，不执行任何动作）：默认返回最近 10 条动作记录"
            "（blocked / dry_run / executed，含危险判定与错误码），stats=true 时改返回"
            "日志文件与轮转概况。用于自查「刚才这一步到底下发没下发、是不是被安全层拦了」。"
        ),
        "inputSchema": _object_schema(
            {
                "recent": {"type": "integer", "description": "返回最近 N 条（默认 10，上限 200）"},
                "stats": {"type": "boolean", "description": "true 只看日志与轮转概况"},
            }
        ),
    },
    {
        "name": "winuse_ocr",
        "description": (
            "用 Windows 自带 OCR 引擎读出图片里的文字（不联网、零第三方依赖）。"
            "返回整段文本与逐行/逐词的像素边框。适合读截图、发票、扫描件。"
            "注意引擎会在汉字之间插空格，需要连贯文本时用 words 自行拼接。"
        ),
        "inputSchema": _object_schema(
            {
                "image_path": {
                    "type": "string",
                    "description": "图片路径（正斜杠或反斜杠都行，脚本内会规范化）",
                },
                "language": {"type": "string", "description": "识别语言（默认 zh-Hans）"},
            },
            required=["image_path"],
        ),
    },
    {
        "name": "winuse_notify",
        "description": (
            "发一条 Windows 通知（系统托盘气泡）。默认只干跑并回显将要执行的命令，"
            "只有 execute=true 才真发出去。"
        ),
        "inputSchema": _object_schema(
            {
                "title": {"type": "string", "description": "标题"},
                "body": {"type": "string", "description": "正文"},
                "level": {
                    "type": "string",
                    # R-19：核心的规范值是 warn；warning 由核心做别名归一（历史声明值）
                    "enum": ["info", "warn", "warning", "error"],
                    "description": "级别（warn 为规范值，warning 是它的别名）",
                },
                "broadcast": {
                    "type": "boolean",
                    "description": (
                        "广播给本机全部会话（RDP / 快速用户切换的桌面也会看到正文）。"
                        "默认 false 只发当前会话；true 时需同时 confirm=true"
                    ),
                },
                "confirm": {"type": "boolean", "description": "确认危险动作（如广播）"},
                "timeout_seconds": {"type": "integer", "description": "停留秒数（默认 10）"},
                "execute": {"type": "boolean", "description": "true 才真发；默认干跑"},
            }
        ),
    },
    {
        "name": "winuse_system",
        "description": (
            "系统盘点：操作系统版本、CPU 核数、内存占用、各磁盘用量、电源状态、内存占用前 N 的进程。"
            "注意：带 power_action 时**只返回电源动作意图**（不会真关机），且属危险动作需 confirm=true。"
        ),
        "inputSchema": _object_schema(
            {
                "top_processes": {
                    "type": "integer", "minimum": 0, "maximum": limit_mod.MAX_TOP_PROCESSES,
                    "description": f"返回内存占用前 N 个进程（默认 15，上限 {limit_mod.MAX_TOP_PROCESSES}）",
                },
                "power_action": {
                    "type": "string",
                    "enum": ["shutdown", "restart", "sleep", "lock", "logoff"],
                    "description": "只要意图、不执行；危险动作需 confirm=true",
                },
                "confirm": {"type": "boolean", "description": "确认电源意图属于危险动作"},
            }
        ),
    },
    {
        "name": "winuse_registry",
        "description": (
            "读注册表（默认只读，安全）；SAM/SECURITY 等敏感路径直接拒。"
            "带 write/delete 时**只返回写入意图**（本轮不真写），且属危险动作需 confirm=true。"
        ),
        "inputSchema": _object_schema(
            {
                "hive": {
                    "type": "string",
                    "enum": list(HIVE_CHOICES),
                    "description": "根键（可选值见 enum；默认 HKCU）",
                },
                "path": {"type": "string", "description": "子路径（如 Software\\Microsoft）"},
                "value_name": {"type": "string", "description": "只取某个值名；写时必填（默认值传空串）"},
                "recurse": {"type": "boolean", "description": "递归列子键"},
                "max_depth": {
                    "type": "integer", "minimum": 0, "maximum": limit_mod.MAX_REGISTRY_DEPTH,
                    "description": f"递归深度上限（默认 3，上限 {limit_mod.MAX_REGISTRY_DEPTH}）",
                },
                "max_subkeys": {
                    "type": "integer", "minimum": 1, "maximum": limit_mod.MAX_REGISTRY_SUBKEYS,
                    "description": f"子键条数上限（默认 200，上限 {limit_mod.MAX_REGISTRY_SUBKEYS}）",
                },
                "write": {"type": "boolean", "description": "改成写入意图（不真写）"},
                "delete": {"type": "boolean", "description": "改成删除意图（不真删）"},
                "value": {"type": "string", "description": "写入的值"},
                "value_type": {"type": "string", "description": "值类型（默认 REG_SZ）"},
                "confirm": {"type": "boolean", "description": "确认注册表写属于危险动作"},
            }
        ),
    },
    {
        "name": "winuse_virtual_desktop",
        "description": (
            "探测虚拟桌面：某个窗口属于哪个虚拟桌面、是否在当前桌面。"
            "接口按 COM 调用（IVirtualDesktopManager）；外壳窗口/隐藏窗口不参与跟踪会返回 WINDOW_NOT_TRACKED。"
        ),
        "inputSchema": _object_schema(
            {"window_id": {"type": "integer", "description": "窗口标识（省略则探测接口本身可用性）"}}
        ),
    },
    {
        "name": "winuse_wait",
        "description": (
            "条件等待：等窗口出现/消失、标题匹配、元素出现或文字出现。"
            "超时是**正常结果**（ok=true + condition_met=false + code=WAIT_TIMEOUT），不是异常；"
            "调用了 wait 之后照常需要重新取状态（元素索引会变）。"
        ),
        "inputSchema": _object_schema(
            {
                "kind": {
                    "type": "string",
                    # 从核心现读枚举（单一出处）：硬编码副本漏同步时，门面会先于核心拒掉合法 kind
                    "enum": list(WAIT_KINDS),
                    "description": (
                        "等待条件类型；element_gone = 等元素消失"
                        "（用 element_index / element_token / pattern 三选一寻址）"
                    ),
                },
                "app": {"type": "string", "description": "按应用名或标题子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "pattern": {
                    "type": "string",
                    "description": "标题/文字匹配模式；element_gone 时按 label/value 子串寻址",
                },
                "element_index": {
                    "type": "integer",
                    "description": "元素索引（element_gone 用，必须配同源 snapshot）",
                },
                "element_token": {
                    "type": "string",
                    "description": "元素令牌（element_gone 用，与 element_index 二选一）",
                },
                "snapshot": {"type": "string", "description": "element_index 的同源快照标识（用索引时必填）"},
                "timeout_seconds": {
                    "type": "number", "minimum": 0, "maximum": limit_mod.MAX_WAIT_SECONDS,
                    "description": f"超时秒数（默认 15，上限 {int(limit_mod.MAX_WAIT_SECONDS)}）",
                },
                "interval_seconds": {
                    "type": "number", "minimum": limit_mod.MIN_WAIT_INTERVAL_SECONDS,
                    "description": (
                        f"轮询间隔秒数（默认 0.5，最小 {limit_mod.MIN_WAIT_INTERVAL_SECONDS}；"
                        "小于最小值会被抬到最小值，避免忙等）"
                    ),
                },
            },
            required=["kind"],
        ),
    },
    {
        "name": "winuse_multi",
        "description": (
            "一次下发多步界面动作（批处理通道）。**批量输入一律算危险动作**，必须 confirm=true；"
            "真动桌面还要 execute=true（默认干跑）。停在失败步可用 stop_on_error 控制。"
        ),
        "inputSchema": _object_schema(
            {
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "maxItems": limit_mod.MAX_BATCH_STEPS,  # R-21：一次请求的资源上限
                    "description": '步骤数组，每步是动作对象（如 [{"action":"click","element_index":0}]）',
                },
                "app": {"type": "string", "description": "按应用名或标题子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "snapshot": {"type": "string", "description": "快照标识（用元素索引时必填）"},
                "confirm": {"type": "boolean", "description": "确认批量输入是危险动作（必填 true 才继续）"},
                "execute": {"type": "boolean", "description": "true 才真下发；默认干跑"},
                "stop_on_error": {"type": "boolean", "description": "单步失败即停（默认 true）"},
            },
            required=["steps"],
        ),
    },
    {
        "name": "winuse_window",
        "description": (
            "窗口动作：最小化／最大化／还原／置顶／移动缩放／关闭。关闭类动作会改变用户桌面状态，"
            "必须 confirm=true；真下发还要 execute=true（默认干跑回显将要调的 Win32 调用）。"
        ),
        "inputSchema": _object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": list(WINDOW_ACTION_CHOICES),
                    "description": "动作名（close 属危险动作）",
                },
                "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "x": {"type": "integer", "description": "移动/缩放的横坐标"},
                "y": {"type": "integer", "description": "移动/缩放的纵坐标"},
                "width": {"type": "integer", "description": "宽度"},
                "height": {"type": "integer", "description": "高度"},
                "always_on_top": {"type": "boolean", "description": "置顶"},
                "confirm": {"type": "boolean", "description": "确认危险动作（关闭窗口等）"},
                "execute": {"type": "boolean", "description": "true 才真下发；默认干跑"},
            },
            required=["action"],
        ),
    },
    {
        "name": "winuse_screenshot",
        "description": (
            "截一张图并**落盘**，回文件路径与尺寸（不下发 base64，避免把图灌进上下文）。"
            "mode=window 截指定窗口的真像素、desktop 截整屏、region 放大某个矩形。"
            "只读操作，不需要 confirm。"
        ),
        "inputSchema": _object_schema(
            {
                "mode": {"type": "string", "enum": list(SHOT_MODES), "description": "window / desktop / region"},
                "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口（desktop 模式不需要）"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "region": {
                    "type": "object",
                    "description": '矩形（region 模式必填）：{"x1": 0, "y1": 0, "x2": 400, "y2": 300}',
                },
                "out_dir": {"type": "string", "description": "落盘目录（默认数据目录下 screenshots/）"},
                "max_dimension": {"type": "integer", "description": "最长边上限（驱动等比缩小，用于压低体积）"},
                "allow_minimized": {"type": "boolean", "description": "接受最小化窗口"},
            },
            required=["mode"],
        ),
    },
    {
        "name": "winuse_drag",
        "description": (
            "拖拽（按住→移动→松开）：元素→元素（给两个元素索引/令牌，门面算出中心点并换算坐标）"
            "或 坐标→坐标（from_point/to_point，window-local 截图像素）。默认只干跑，"
            "execute=true 才真下发。"
        ),
        "inputSchema": _object_schema(
            {
                "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "from_element": {"type": "integer", "description": "起点元素索引（来自 winuse_state）"},
                "from_token": {"type": "string", "description": "起点元素令牌（与 from_element 二选一）"},
                "to_element": {"type": "integer", "description": "终点元素索引"},
                "to_token": {"type": "string", "description": "终点元素令牌（与 to_element 二选一）"},
                "from_point": {"type": "object", "description": '起点坐标：{"x": 10, "y": 20}（截图局部像素）'},
                "to_point": {"type": "object", "description": "终点坐标：同上"},
                "snapshot": {"type": "string", "description": "快照标识（用元素索引时必填）"},
                "execute": {"type": "boolean", "description": "true 才真下发；默认干跑"},
                "confirm": {"type": "boolean", "description": "确认危险动作（系统级窗口等）"},
            }
        ),
    },
    {
        "name": "winuse_app",
        "description": (
            "应用生命周期：launch 启动（驱动隐藏启动，不抢焦点）/ list 枚举（运行中 + 已安装）"
            "/ kill 按进程号结束。kill 属危险动作必须 confirm=true；launch 与 kill 真执行还要 "
            "execute=true（默认干跑）。list 是只读枚举。"
        ),
        "inputSchema": _object_schema(
            {
                "action": {"type": "string", "enum": sorted(APP_ACTIONS), "description": "launch / list / kill"},
                "pid": {"type": "integer", "description": "kill 的目标进程号"},
                "path": {"type": "string", "description": "launch 的可执行文件路径"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "launch 的附加参数（每项一个参数，不按空格切）",
                },
                "running": {"type": "boolean", "description": "list：只看正在运行的"},
                "kind": {"type": "string", "description": "list：desktop / uwp"},
                "query": {"type": "string", "description": "list：按名称子串过滤"},
                "limit": {"type": "integer", "description": "list：返回条数上限（默认 50，上限 80）"},
                "confirm": {"type": "boolean", "description": "确认危险动作（kill、系统级应用）"},
                "execute": {"type": "boolean", "description": "true 才真下发；默认干跑"},
            },
            required=["action"],
        ),
    },
    {
        "name": "winuse_search",
        "description": (
            "用 Everything（es.exe）全盘搜本机文件——**只读**，不需要 confirm，也不需要驱动。"
            "支持通配符与 Everything 语法（如 *.md、size:>10kb、-path D:/Projects）。"
            "找不到 es.exe 时返回 ES_NOT_FOUND（缺工具 ≠ 搜索无结果）。"
        ),
        "inputSchema": _object_schema(
            {
                "query": {"type": "string", "description": "搜索词（多个词之间是「与」）"},
                "regex": {"type": "string", "description": "正则搜索（与 query 二选一）"},
                "path": {"type": "string", "description": "限定目录"},
                "max_results": {"type": "integer", "description": "结果条数上限（默认 50，上限 200）"},
                "sort": {"type": "string", "description": "排序：name / path / size / date-modified 等"},
                "min_size": {"type": "string", "description": "最小体积（如 10kb / 1mb）"},
                "max_size": {"type": "string", "description": "最大体积"},
                "timeout_seconds": {"type": "number", "description": "超时秒数（默认 15）"},
                "es_path": {"type": "string", "description": "es.exe 路径（默认自动定位）"},
            }
        ),
    },
    {
        "name": "winuse_menu",
        "description": (
            "按路径调用应用菜单项（逐层解析，不需要坐标也不需要快捷键）。"
            "会改变应用状态 → **恒判危险**：不确认时连干跑计划都不给；"
            "confirm=true 后仍需 execute=true 才真调用。"
        ),
        "inputSchema": _object_schema(
            {
                "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "path": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": '菜单路径逐段，例如 ["文件", "另存为"]',
                },
                "execute": {"type": "boolean", "description": "true 才真调用；默认只给计划"},
                "confirm": {"type": "boolean", "description": "确认（必填 true 才会有下一步）"},
            },
            required=["path"],
        ),
    },
    {
        "name": "winuse_verify",
        "description": (
            "对窗口做**确定性断言**（谓词数组，条与条之间取与）。只读，不需要 confirm。"
            "三态返回：satisfied / unsatisfied / unknown——unknown 是「驱动看不到」，"
            "**不能当成不满足**。默认干跑，只回将要下发的参数。"
        ),
        "inputSchema": _object_schema(
            {
                "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "predicates": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": '谓词数组（1..8 条），例如 [{"window": {"exists": true}}]',
                },
                "include_screenshot": {"type": "boolean", "description": "断言时附带截图"},
                "stable_samples": {"type": "integer", "description": "稳定采样次数（默认 2，1..5）"},
                "timeout_ms": {"type": "integer", "description": "超时毫秒（默认 5000，0..10000）"},
                "execute": {"type": "boolean", "description": "true 才真断言；默认干跑"},
            },
            required=["predicates"],
        ),
    },
    {
        "name": "winuse_find",
        "description": (
            "按文本**模糊**查找元素（容错：记错一个字也能命中）。回候选的 element_index / "
            "element_token / score（1.0 = 完全相等或互为子串）+ 同源 snapshot_id，"
            "拿到索引后交给 winuse_act 操作。比精确子串更能容忍 UI 标签里的记号（如「修改日期▲」）。"
        ),
        "inputSchema": _object_schema(
            {
                "text": {"type": "string", "description": "要找的文本（必填）"},
                "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "threshold": {
                    "type": "number",
                    "description": "相似度下限（缺省用核心默认 0.7；3 字以内短标签建议显式降到 0.6）",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "参与比较的字段（缺省用核心默认；驱动真实字段是 label / value）",
                },
                "limit": {"type": "integer", "description": "候选上限（缺省 10，核心上限 50）"},
                "max_elements": {"type": "integer", "description": "先裁剪元素树的规模上限"},
                "allow_minimized": {"type": "boolean", "description": "接受最小化窗口"},
            },
            required=["text"],
        ),
    },
    {
        "name": "winuse_read",
        "description": (
            "一次读**多个**元素的值/状态（只读，只发一次 get_window_state）。每个元素用 "
            "[{index: 0}, {token: \"s00000001:2\"}] 指定，按索引寻址要配同源 snapshot；"
            "字段缺省读核心的可读字段全集。树里找不到的元素进 missing，不报错。"
        ),
        "inputSchema": _object_schema(
            {
                "elements": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": '要读的元素，如 [{"index": 0}, {"token": "s00000001:2"}]',
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要读的字段（缺省全部可读字段）",
                },
                "snapshot": {"type": "string", "description": "按索引寻址时必须给的同源快照标识"},
                "app": {"type": "string", "description": "按应用名或窗口标题子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "query": {"type": "string", "description": "只取匹配该子串的元素＋祖先链"},
                "max_elements": {"type": "integer", "description": "元素树下推上限"},
                "allow_minimized": {"type": "boolean", "description": "接受最小化窗口"},
            },
            required=["elements"],
        ),
    },
    {
        "name": "winuse_clipboard",
        "description": (
            "读/写剪贴板。read 默认**只列类型**（格式名，不含内容）；include_text=true 才是取正文，"
            "属隐私读取、要 confirm=true。write 会整段替换用户剪贴板且不可撤销 → 必须 confirm=true，"
            "真写入还要 execute=true（默认干跑）；text / image_path / file_path **三选一**。"
        ),
        "inputSchema": _object_schema(
            {
                "action": {"type": "string", "enum": ["read", "write"], "description": "read / write"},
                "include_text": {"type": "boolean", "description": "read：是否取剪贴板正文（需 confirm）"},
                "text": {"type": "string", "description": "write：写入的纯文本（原样，不 trim）"},
                "image_path": {"type": "string", "description": "write：图片的**绝对**路径"},
                "file_path": {"type": "string", "description": "write：文件的**绝对**路径（写成 file URL）"},
                "confirm": {"type": "boolean", "description": "确认（读正文 / 写剪贴板都必须）"},
                "execute": {"type": "boolean", "description": "write：true 才真写入；默认干跑"},
                "max_text_chars": {"type": "integer", "description": "read：正文回读上限（缺省 8000）"},
            },
            required=["action"],
        ),
    },
    {
        "name": "winuse_session",
        "description": (
            "查当前会话/桌面状态：是否锁屏或处于安全桌面（UAC 同意页 / 登录界面），以及各能力现在"
            "**能不能用**（读元素 / 键盘 / 鼠标 / 拖拽 / 截图 / 启动应用）。锁屏下动作会报"
            "「找不到窗口」这类误导性错误，先用它排掉这个前提。只读、**不需要驱动**；"
            "locked=null 表示**没证明**，不等于没锁。"
        ),
        "inputSchema": _object_schema(
            {"explain": {"type": "boolean", "description": "附能力可用性矩阵（默认 true）"}}
        ),
    },
]


# ------------------------------------------------------------------ PR-8 新增能力的处理函数


def _confirm_payload(verdict: dict, hint: str, **extra) -> dict:
    """危险动作未确认的统一返回（口径与核心 guard 一致）。"""
    payload = {
        "ok": False,
        "requires_confirm": True,
        "danger": verdict,
        "error": {
            "code": verdict.get("code") or "CONFIRM_REQUIRED",
            "message": verdict.get("reason") or "危险动作需要确认",
            "detail": hint,
        },
    }
    payload.update(extra)
    return payload


def _facade_blocked(session: "Session", event: str, verdict: dict, hint: str, **extra) -> dict:
    """门面**提前**拦截时也要落审计（R-1）。

    为什么门面还会提前拦：核心的闸门是自包含的（R-12/R-13 之后两维全判），门面这层
    重复判定只为两个好处——① 在解析目标之前就拒，省一次驱动往返；② 提示更贴近该工具
    的用法。既然拦在这里，审计就得在这里写，否则"被拦下的尝试"在日志里消失。
    事件名与核心保持一致（`window.blocked` / `batch.blocked` …），便于统一对账。
    """
    from ..audit import sink_event

    written = sink_event(
        session.audit_sink(),
        event=event,
        ok=False,
        code=verdict.get("code"),
        detail={"reason": verdict.get("reason"), "rule": verdict.get("rule"), "blocked_at": "facade"},
    )
    payload = _confirm_payload(verdict, hint, **extra)
    if written is not None:
        payload["audit_written"] = written
    return payload


def _tool_ocr(session: Session, args: dict) -> dict:
    from ..core import ocr as m

    return m.run_ocr(
        m.OcrSpec(
            image_path=str(args.get("image_path") or ""),
            language=str(args.get("language") or "zh-Hans"),
        )
    )


def _tool_notify(session: Session, args: dict) -> dict:
    from ..core import notify as m

    return m.run_notify(
        m.NotifySpec(
            title=str(args.get("title") or ""),
            body=str(args.get("body") or ""),
            level=str(args.get("level") or "info"),
            timeout_seconds=int(args.get("timeout_seconds") or 10),
            execute=strict_bool(args.get("execute"), field="execute"),
            broadcast=strict_bool(args.get("broadcast"), field="broadcast"),
        ),
        audit=session.audit_sink(),
        confirmed=strict_bool(args.get("confirm"), field="confirm"),
    )


def _tool_system(session: Session, args: dict) -> dict:
    from ..core import guard as g
    from ..core import system as m

    power_action = args.get("power_action")
    if power_action:
        action = str(power_action)
        confirm = strict_bool(args.get("confirm"), field="confirm")
        verdict = g.assess_power(action).to_dict()
        if verdict.get("dangerous") and not confirm:
            return _facade_blocked(
                session, "system.blocked", verdict,
                "确认方式：confirm=true（仍然只返回意图，不会真关机）",
                power=m.plan_power_action(action),
            )
        out = m.plan_power_action(action)
        out["confirmed"] = confirm
        return out
    return m.describe_system(m.SystemSpec(top_processes=int(args.get("top_processes") or 15)))


def _tool_registry(session: Session, args: dict) -> dict:
    from ..core import guard as g
    from ..core import registry as m

    hive = str(args.get("hive") or "HKCU")
    path = str(args.get("path") or "")
    write = strict_bool(args.get("write"), field="write")
    delete = strict_bool(args.get("delete"), field="delete")
    confirm = strict_bool(args.get("confirm"), field="confirm")
    if write or delete:
        spec = m.RegWriteSpec(
            hive=hive,
            path=path,
            value_name=args.get("value_name"),
            value=args.get("value"),
            value_type=str(args.get("value_type") or "REG_SZ"),
            delete=delete,
        )
        verdict = g.assess_registry_write(hive, path).to_dict()
        if verdict.get("dangerous") and not confirm:
            return _facade_blocked(
                session, "registry.blocked", verdict,
                "确认方式：confirm=true（仍然只返回意图，不会真写注册表）",
                intent=m.plan_registry_write(spec),
            )
        out = m.plan_registry_write(spec)
        out["confirmed"] = confirm
        return out
    # 敏感读要确认（R-11）：门面先拦一次（省一次驱动往返 + 提示更具体），
    # 核心也自带同一道闸门（`guard.assess_registry_read`），判据只有一份
    read_verdict = g.assess_registry_read(hive, path).to_dict()
    if read_verdict.get("dangerous") and not confirm:
        return _facade_blocked(
            session, "registry.blocked", read_verdict,
            "确认方式：confirm=true（读系统配置区不改系统，但足以摸清机器配置）",
            mode="read",
        )
    return m.read_registry(
        m.RegReadSpec(
            hive=hive,
            path=path,
            value_name=args.get("value_name"),
            recurse=strict_bool(args.get("recurse"), field="recurse"),
            max_depth=int(args.get("max_depth") or 3),
            max_subkeys=int(args.get("max_subkeys") or 200),
        ),
        confirmed=confirm,
    )


def _tool_virtual_desktop(session: Session, args: dict) -> dict:
    from ..core import desktop as m

    window_id = args.get("window_id")
    return m.virtual_desktop_info(int(window_id) if window_id is not None else None)


def _tool_wait(session: Session, args: dict) -> dict:
    _require_driver(session)
    from ..core import wait_for as m

    spec = m.WaitSpec(
        kind=str(args.get("kind") or ""),
        app=args.get("app"),
        pid=args.get("pid"),
        window_id=args.get("window_id"),
        pattern=args.get("pattern"),
        element_index=args.get("element_index"),
        element_token=args.get("element_token"),
        snapshot_id=args.get("snapshot"),
        timeout_seconds=float(args.get("timeout_seconds") or 15),
        interval_seconds=float(args.get("interval_seconds") or 0.5),
    )
    return m.wait_for(session.driver, spec)


def _resolve_target(driver, args: dict) -> dict:
    from ..core.state import StateOptions, build_state, pick_window

    opts = StateOptions(
        app=args.get("app"),
        pid=args.get("pid"),
        window_id=args.get("window_id"),
        allow_minimized=True,
    )
    state = build_state(driver, opts)
    return pick_window(state.get("windows") or [], opts)


def _tool_multi(session: Session, args: dict) -> dict:
    _require_driver(session)
    from ..core import guard as g
    from ..core import multi_input as m

    steps = args.get("steps")
    if not isinstance(steps, list) or not steps:
        raise MissingParam("steps 必须是非空数组")
    confirm = strict_bool(args.get("confirm"), field="confirm")
    verdict = g.assess_batch(steps).to_dict()
    if verdict.get("dangerous") and not confirm:
        return _facade_blocked(
            session, "batch.blocked", verdict,
            "确认方式：confirm=true（确认了也不会自动执行，仍需 execute=true）",
            steps_count=len(steps),
        )
    target = _resolve_target(session.driver, args)
    return m.run_multi(
        session.driver,
        steps,
        target,
        args.get("snapshot"),
        execute=strict_bool(args.get("execute"), field="execute"),
        stop_on_error=strict_bool(args.get("stop_on_error"), field="stop_on_error", default=True),
        confirmed=confirm,
        audit=session.audit_sink(),
    )


def _tool_window(session: Session, args: dict) -> dict:
    _require_driver(session)
    from ..core import desktop as m
    from ..core import guard as g

    action = str(args.get("action") or "")
    confirm = strict_bool(args.get("confirm"), field="confirm")
    verdict = g.assess_action(action=action, target=None).to_dict()
    if verdict.get("dangerous") and not confirm:
        return _facade_blocked(
            session, "window.blocked", verdict,
            "确认方式：confirm=true（关闭窗口等会改变用户桌面状态）",
        )
    spec = m.WindowActionSpec(
        action=action,
        pid=args.get("pid"),
        window_id=args.get("window_id"),
        app=args.get("app"),
        x=args.get("x"),
        y=args.get("y"),
        width=args.get("width"),
        height=args.get("height"),
        always_on_top=args.get("always_on_top"),
    )
    out = m.window_action(
        session.driver,
        spec,
        execute=strict_bool(args.get("execute"), field="execute"),
        # 与 core.act 的危险动作口径一致：危险 + 显式确认 → 放行
        confirmed=confirm,
        audit=session.audit_sink(),
    )
    out["confirmed"] = confirm
    return out


def _tool_screenshot(session: "Session", args: dict) -> dict:
    """截图出口（P0-4）：只读，落盘后只回路径与尺寸。"""
    _require_driver(session)
    from ..core import screenshot as m

    mode = str(args.get("mode") or "")
    if mode not in m.MODES:
        # 参数级错误要**在碰驱动之前**抛：否则一次调用会先枚举一遍窗口才失败
        raise MissingParam(f"mode 必须是 {' / '.join(m.MODES)} 之一")
    options: dict = {}
    if args.get("max_dimension") is not None:
        options["max_dimension"] = args.get("max_dimension")
    if strict_bool(args.get("allow_minimized"), field="allow_minimized"):
        options["allow_minimized"] = True
    target = None if mode == "desktop" else _resolve_target(session.driver, args)
    return m.run_screenshot(
        session.driver,
        mode=mode,
        target=target,
        out_dir=args.get("out_dir"),
        region=args.get("region"),
        options=options or None,
    )


def _tool_drag(session: "Session", args: dict) -> dict:
    """拖拽（P1-2）：元素→元素或坐标→坐标，默认干跑。"""
    _require_driver(session)
    from ..core import drag as m

    def _ref(kind: str):
        index = args.get(f"{kind}_element")
        token = args.get(f"{kind}_token")
        if index is not None and token is not None:
            raise MissingParam(f"{kind}_element 与 {kind}_token 只能给一个（索引要同源快照，令牌免快照）")
        return index if index is not None else token

    return m.run_drag(
        session.driver,
        target=_resolve_target(session.driver, args),
        from_element=_ref("from"),
        to_element=_ref("to"),
        from_point=args.get("from_point"),
        to_point=args.get("to_point"),
        snapshot_id=args.get("snapshot"),
        execute=strict_bool(args.get("execute"), field="execute"),
        confirm=strict_bool(args.get("confirm"), field="confirm"),
        audit=session.audit_sink(),
    )


def _tool_app(session: "Session", args: dict) -> dict:
    """应用生命周期（P1-3）：launch / list / kill，默认干跑、kill 需确认。"""
    _require_driver(session)
    from ..core import app as m

    options: dict = {}
    for key in ("running", "kind", "query", "limit"):
        if args.get(key) is not None:
            options[key] = args.get(key)
    return m.run_app(
        session.driver,
        action=str(args.get("action") or ""),
        pid=args.get("pid"),
        path=args.get("path"),
        args=args.get("args"),
        confirm=strict_bool(args.get("confirm"), field="confirm"),
        execute=strict_bool(args.get("execute"), field="execute"),
        options=options or None,
        audit=session.audit_sink(),
    )


def _tool_search(session: "Session", args: dict) -> dict:
    """Everything 全盘搜索：**不需要驱动**（只读本地文件索引），因此刻意不调 `_require_driver`。

    这条差异很重要：别的工具在没驱动的会话里全部不可用，而"我现在手头有哪些文件"
    这类问题跟 cua-driver 毫无关系。
    """
    from ..core import search as m

    kwargs: dict = {}
    if args.get("timeout_seconds") is not None:
        kwargs["timeout_seconds"] = args.get("timeout_seconds")
    return m.run_search(
        query=args.get("query"),
        regex=args.get("regex"),
        path=args.get("path"),
        max_results=args.get("max_results", 50),
        sort=args.get("sort"),
        min_size=args.get("min_size"),
        max_size=args.get("max_size"),
        es_path=args.get("es_path"),
        **kwargs,
    )


def _tool_menu(session: "Session", args: dict) -> dict:
    """菜单路径调用（P1-1）：恒判危险，未确认连计划都不给（判定在核心）。"""
    _require_driver(session)
    from ..core import menu as m

    return m.run_menu(
        session.driver,
        target=_resolve_target(session.driver, args),
        path=args.get("path"),
        execute=strict_bool(args.get("execute"), field="execute"),
        confirm=strict_bool(args.get("confirm"), field="confirm"),
        audit=session.audit_sink(),
    )


def _tool_verify(session: "Session", args: dict) -> dict:
    """确定性断言（P1-1）：只读、无确认门，默认干跑。"""
    _require_driver(session)
    from ..core import verify as m

    kwargs: dict = {}
    if args.get("stable_samples") is not None:
        kwargs["stable_samples"] = args.get("stable_samples")
    if args.get("timeout_ms") is not None:
        kwargs["timeout_ms"] = args.get("timeout_ms")
    return m.run_verify(
        session.driver,
        target=_resolve_target(session.driver, args),
        predicates=args.get("predicates"),
        execute=strict_bool(args.get("execute"), field="execute"),
        include_screenshot=strict_bool(args.get("include_screenshot"), field="include_screenshot"),
        audit=session.audit_sink(),
        **kwargs,
    )


def _tool_find(session: "Session", args: dict) -> dict:
    """模糊寻址：容错找元素，回候选 + 同源快照（拿到索引后交给 winuse_act）。"""
    _require_driver(session)
    from ..core import fuzzy as m
    from ..core.state import build_state

    # 不用 query 投影：模糊匹配要在**完整**元素树上比，投影会先把候选裁掉。
    # 注意别在这里塞 `query=None`——那会被当成「有 query」而投影出空集。
    state = build_state(session.driver, _state_options(args, session.cfg))
    elements = state.get("elements") or []
    kwargs: dict = {}
    if args.get("threshold") is not None:
        kwargs["threshold"] = args.get("threshold")
    if args.get("limit") is not None:
        kwargs["limit"] = args.get("limit")
    if args.get("fields"):
        kwargs["fields"] = args.get("fields")
    matches = m.find_elements(elements, text=str(args.get("text") or ""), **kwargs)
    return {
        "ok": True,
        "snapshot_id": state.get("snapshot_id"),
        "elements_count": len(elements),
        "matches": matches,
        "count": len(matches),
        "note": (
            "匹配度 1.0 = 完全相等或互为子串；element_index 只在与回执里的 snapshot_id 同源时有效。"
            "阈值/字段缺省走核心默认；3 字以内短标签建议显式调低阈值。"
        ),
    }


def _tool_read(session: "Session", args: dict) -> dict:
    """批量读值（只读）：一次 get_window_state 读多个元素的字段。"""
    _require_driver(session)
    from ..core import read as m

    options: dict = {}
    for key in ("query", "max_elements", "max_depth"):
        if args.get(key) is not None:
            options[key] = args.get(key)
    return m.run_read(
        session.driver,
        target=_resolve_target(session.driver, args),
        elements=args.get("elements"),
        fields=args.get("fields"),
        snapshot_id=args.get("snapshot"),
        options=options or None,
        audit=session.audit_sink(),
    )


def _tool_clipboard(session: "Session", args: dict) -> dict:
    """剪贴板读/写：读正文与写都过确认门（危险判定在核心 guard，门面只翻译参数）。"""
    _require_driver(session)
    from ..core import clipboard as m

    options: dict = {}
    if args.get("max_text_chars") is not None:
        options["max_text_chars"] = args.get("max_text_chars")
    return m.run_clipboard(
        session.driver,
        action=str(args.get("action") or ""),
        include_text=strict_bool(args.get("include_text"), field="include_text"),
        text=args.get("text"),
        image_path=args.get("image_path"),
        file_path=args.get("file_path"),
        confirm=strict_bool(args.get("confirm"), field="confirm"),
        execute=strict_bool(args.get("execute"), field="execute"),
        options=options or None,
        audit=session.audit_sink(),
    )


def _tool_session(session: "Session", args: dict) -> dict:
    """会话/锁屏状态（只读，**不需要驱动**）：它是「驱动还有没有用」的前提。"""
    from ..core import session_state as m

    state = m.detect_session_state()
    out: dict[str, Any] = {"ok": True, **state}
    if strict_bool(args.get("explain"), field="explain", default=True):
        out["availability"] = m.explain_action_availability(state)
    return out


#: 审计查询一次最多回多少条（防止把整份日志灌进模型上下文）
MAX_AUDIT_ROWS = 200


def _tool_audit(session: "Session", args: dict) -> dict:
    """审计查询（T-26）：只读，**不需要驱动**。

    只做参数翻译与限流，逻辑仍在 `winuse.audit`（门面不复制业务）。
    """
    from .. import audit as audit_mod

    requested = args.get("recent", 10)
    try:
        count = int(requested)
    except (TypeError, ValueError):
        raise MissingParam("winuse_audit 的 recent 必须是整数")
    if count < 1:
        raise MissingParam("winuse_audit 的 recent 至少为 1")
    clamped = min(count, MAX_AUDIT_ROWS)

    # 路径口径与写入端一致（R-1）：复用同一个 `env_path`，否则 `.env` 里配了
    # `WINUSE_AUDIT_PATH` 时会出现"查到的不是写下的"
    path = audit_mod.default_path(session.env_path)
    if strict_bool(args.get("stats"), field="stats"):
        return {"ok": True, "rotation": audit_mod.describe_log(path, env_path=session.env_path)}
    entries = audit_mod.read_recent(clamped, path=path, env_path=session.env_path)
    out: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "requested": clamped,
        "returned": len(entries),
        "entries": entries,
    }
    if clamped != count:
        out["clamped"] = True
        out["requested_before_clamp"] = count
    return out


_TOOL_HANDLERS: dict[str, Callable[["Session", dict], dict]] = {
    "winuse_state": _tool_state,
    "winuse_audit": _tool_audit,
    "winuse_act": _tool_act,
    "winuse_display": _tool_display,
    "winuse_ocr": _tool_ocr,
    "winuse_notify": _tool_notify,
    "winuse_system": _tool_system,
    "winuse_registry": _tool_registry,
    "winuse_virtual_desktop": _tool_virtual_desktop,
    "winuse_wait": _tool_wait,
    "winuse_multi": _tool_multi,
    "winuse_window": _tool_window,
    "winuse_screenshot": _tool_screenshot,
    "winuse_drag": _tool_drag,
    "winuse_app": _tool_app,
    "winuse_search": _tool_search,
    "winuse_menu": _tool_menu,
    "winuse_verify": _tool_verify,
    "winuse_find": _tool_find,
    "winuse_read": _tool_read,
    "winuse_clipboard": _tool_clipboard,
    "winuse_session": _tool_session,
}


# ------------------------------------------------------------------ 会话与分派


@dataclass
class Session:
    """一次 MCP 连接的状态。"""

    driver: Any = None
    cfg: Any = None
    initialized: bool = False
    client_info: dict = field(default_factory=dict)
    protocol_version: str = P.PREFERRED_PROTOCOL_VERSION
    #: `serve(env_path=...)` 拿到的配置路径——**必须留着**（R-1）：审计写入端与
    #: `winuse_audit` 查询端都靠它指向同一个文件，否则 `.env` 里配的
    #: `WINUSE_AUDIT_PATH` 只在写入侧生效，查询会去读另一个文件（"查到的不是写下的"）
    env_path: str | None = None
    #: 当前正在处理的 JSON-RPC 请求 id（审计里带上，便于事后把动作与请求对上）
    request_id: Any = None

    def audit_sink(self):
        """审计 sink：把核心给的事件落盘，并补上"谁调用的"上下文。

        返回 `True`/`False` 表示落盘成败（协议见 `audit.sink_event`）——
        核心会把它放进回执 `audit_written`，让"审计是否真的写了"可观察。
        """
        from .. import audit as audit_mod

        def sink(entry: dict):
            payload = dict(entry)
            payload.setdefault("source", "mcp")
            if self.request_id is not None:
                payload.setdefault("request_id", self.request_id)
            return audit_mod.record(payload, env_path=self.env_path) is not None

        return sink


def _initialize_result(session: Session, params: dict) -> dict:
    session.protocol_version = P.negotiate_version(params.get("protocolVersion"))
    session.client_info = params.get("clientInfo") or {}
    return {
        "protocolVersion": session.protocol_version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "title": "WinUse", "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def _call_tool(session: Session, params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise MissingParam("arguments 必须是对象")
    handler = _TOOL_HANDLERS.get(str(name))
    if handler is None:
        raise _UnknownTool(str(name))
    return handler(session, args)


class _UnknownTool(Exception):
    """未知工具：按规范走协议错误 -32602。"""


#: 握手完成前允许的方法（规范要求 initialize 先行；ping 是探活，随时可用）
PRE_INIT_METHODS = frozenset({"initialize", "ping", "notifications/initialized"})


def handle_message(session: Session, msg: dict) -> dict | None:
    """分派一条消息；返回 None 表示不需回包（通知/响应）。

    外层统一做**线路预算**（R-20）：限额是给"一条消息"的，不是给内层 payload 的，
    所以必须在最终序列化之后量一次；信封（`jsonrpc`/`id`）不许被裁——那是回包能被
    识别的唯一依据。
    """
    reply = _dispatch_message(session, msg)
    if reply is None:
        return None
    return limit_mod.enforce_wire_budget(reply)


def _dispatch_message(session: Session, msg: dict) -> dict | None:
    """内层分派（不含预算裁剪）。

    两道入口检查（R-6），都在分派之前：
    1. **信封校验**：`jsonrpc`/`method`/`id` 不合法 → `-32600`，且带 `id` 便于对账
    2. **生命周期**：除 initialize / ping / 通知 外，必须先完成
       `initialize` + `notifications/initialized` 才能进工具阶段——否则未握手的
       连接可以直接列工具、调工具（实测过）
    """
    reason = P.validate_envelope(msg)
    if reason is not None:
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(msg_id, (list, dict)):
            msg_id = None  # 非法 id 不能原样回显
        return P.make_error(msg_id, P.INVALID_REQUEST, f"非法请求：{reason}")

    method = msg.get("method")
    msg_id = msg.get("id")
    # 审计要能对上"哪条请求"（R-1）：每次分派前记下当前请求 id，sink 会带上它
    session.request_id = msg_id

    if method is None:
        return None  # 响应类消息（带 result/error）：本服务端不主动发请求，忽略即可

    if P.is_notification(msg):
        if method == "notifications/initialized":
            session.initialized = True
        return None

    if not session.initialized and method not in PRE_INIT_METHODS:
        return P.make_error(
            msg_id,
            P.INVALID_REQUEST,
            f"服务端尚未完成初始化，不能调用 {method}；请先 initialize 并发送 notifications/initialized",
        )

    try:
        if method == "initialize":
            return P.make_result(msg_id, _initialize_result(session, msg.get("params") or {}))
        if method == "ping":
            return P.make_result(msg_id, {})
        if method == "tools/list":
            return P.make_result(msg_id, {"tools": tool_catalog()})
        if method == "tools/call":
            return P.make_result(msg_id, _tool_result(session, msg.get("params") or {}))
    except _UnknownTool as exc:
        return P.make_error(msg_id, P.INVALID_PARAMS, f"Unknown tool: {exc}", {"tools": sorted(_TOOL_HANDLERS)})
    except _PROTOCOL_LEVEL_ERRORS as exc:
        return P.make_error(msg_id, P.INVALID_PARAMS, str(exc))
    except Exception as exc:  # noqa: BLE001
        from ..audit import redact_text

        return P.make_error(
            msg_id,
            P.INTERNAL_ERROR,
            f"{type(exc).__name__}: {redact_text(str(exc), limit=200)}",
        )

    return P.make_error(msg_id, P.METHOD_NOT_FOUND, f"method not found: {method}")


#: 单条工具结果的字符上限 + **线路消息**字节预算（R-20）。
#: 实现搬到 `winuse/limit.py`：Hermes 桥接也要用同一份（各写一份必然漂移，
#: 表现是"某个门面偶发断连"——最难查的一类问题）。
MAX_RESULT_CHARS = limit_mod.MAX_RESULT_CHARS
MAX_WIRE_BYTES = limit_mod.MAX_WIRE_BYTES

#: 兼容别名：既有调用点与测试沿用这些名字，指向**共享实现**
_bounded = limit_mod.limit_payload
_bound_result = limit_mod.limit_payload
_too_large = limit_mod.too_large
_enforce_wire_budget = limit_mod.enforce_wire_budget


def _tool_result(session: Session, params: dict) -> dict:
    """工具结果包装：文本内容 + isError 标志（执行错误不升级为协议错误）。"""
    try:
        payload = _call_tool(session, params)
    except (_UnknownTool, *_PROTOCOL_LEVEL_ERRORS):
        # 参数级/未知工具 → 交给 handle_message 回协议错误，别在这里降级成执行错误
        raise
    except Exception as exc:  # noqa: BLE001 —— 运行期失败 → 工具执行错误
        payload = _error_payload(exc)
    payload = _bounded(payload)  # 门面职责：把结果压进客户端能收下的尺寸
    text = _dumps(payload)
    return {"content": [{"type": "text", "text": text}], "isError": bool(not payload.get("ok", True))}


def _dumps(obj: Any) -> str:
    """紧凑序列化：缩进对 agent 没有价值，却会白吃线路预算（R-20）。"""
    return limit_mod.dumps(obj)


# ------------------------------------------------------------------ 主循环


def _write(out, msg: dict) -> None:
    out.write(P.encode_message(msg) + "\n")
    out.flush()


def serve(
    stdin=None,
    stdout=None,
    stderr=None,
    driver_factory: Callable[[], Any] | None = None,
    env_path: str | None = None,
) -> int:
    """stdio 主循环：逐行读，回包逐行写（stdout 只写协议消息，日志一律走 stderr）。"""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    # MCP 规范要求 UTF-8；Windows 空管道默认走本地编码（cp936）→ 中文乱码（实测 WorkBuddy）
    from ..textio import force_utf8

    for _stream in (stdin, stdout, stderr):
        force_utf8(_stream)

    def log(message: str) -> None:
        print(f"[winuse-mcp] {message}", file=stderr, flush=True)

    log(f"winuse MCP stdio 服务启动 v{SERVER_VERSION}（协议 {P.PREFERRED_PROTOCOL_VERSION}）")

    cfg = None
    try:
        cfg = Config.load(env_path=env_path)
    except Exception as exc:  # noqa: BLE001
        log(f"配置加载失败：{exc}（工具调用将返回驱动不可用）")

    driver = None
    try:
        driver = driver_factory() if driver_factory is not None else (cfg.driver() if cfg is not None else None)
    except Exception as exc:  # noqa: BLE001
        log(f"驱动初始化失败：{exc}（工具调用将返回驱动不可用）")

    # env_path 必须带进 Session（R-1）：审计写入端与 winuse_audit 查询端同源
    session = Session(driver=driver, cfg=cfg, env_path=env_path)
    log(f"驱动就绪：{'是' if driver is not None else '否'}")

    for raw in stdin:
        if not raw.strip():
            continue
        # 行长上限（R-21）：超长行不该进解析器（解析本身就要吃内存），直接拒
        if len(raw.encode("utf-8", errors="replace")) > limit_mod.MAX_STDIO_LINE_BYTES:
            log(f"拒绝超长输入行（>{limit_mod.MAX_STDIO_LINE_BYTES} 字节）")
            _write(stdout, P.make_error(
                None, P.INVALID_REQUEST,
                f"单行输入超过 {limit_mod.MAX_STDIO_LINE_BYTES} 字节上限，已拒绝",
            ))
            continue
        msg = P.decode_message(raw)
        if msg is None:
            _write(stdout, P.make_error(None, P.PARSE_ERROR, "无法解析为 JSON-RPC 消息"))
            continue
        reply = handle_message(session, msg)
        if reply is not None:
            _write(stdout, reply)

    log("stdin 关闭，正常退出")
    return 0
