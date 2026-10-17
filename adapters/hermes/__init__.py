"""WinUse Hermes 插件（门面 1：最强入口）。

定位：**瘦门面**——只做协议翻译（工具调用 → 核心 CLI 参数），业务逻辑一律在项目核心。
换皮不换芯：Hermes 插件、MCP 服务、CLI 三个门面共享同一份核心实现。

安全默认：`winuse_act` 默认**干跑**（只回显将要下发的调用），必须显式 `execute: true`
才真动桌面——给模型一个"先看后动"的天然两步。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("winuse")

TOOLSET = "winuse"

#: 每个工具在 Hermes 界面上的图标（缺省用工具箱）
EMOJI = {
    "winuse_state": "🖥️",
    "winuse_act": "🖱️",
    "winuse_display": "🧭",
    "winuse_ocr": "🔍",
    "winuse_notify": "🔔",
    "winuse_system": "📊",
    "winuse_registry": "🗄️",
    "winuse_virtual_desktop": "🗂️",
    "winuse_wait": "⏳",
    "winuse_multi": "🧩",
    "winuse_window": "🪟",
}

#: 与核心 winuse/core/act.py 的 ACTION_TOOLS 保持一致（此处只为工具描述可读）
ACTIONS = (
    "click",
    "invoke",
    "toggle",
    "select",
    "expand",
    "set_value",
    "type",
    "key",
    "scroll",
    "activate",
)


def _call_core():
    """取核心桥接函数。

    插件以 `hermes_plugins.<slug>` 命名空间子包加载（相对导入可用），但某些测试/直跑
    场景会按普通脚本导入——故双模式，两边都能工作。
    """
    try:
        from .core_bridge import call_core  # type: ignore
    except ImportError:  # pragma: no cover - 非包加载场景
        from core_bridge import call_core  # type: ignore

    return call_core


def _state_schema() -> dict:
    return {
        "name": "winuse_state",
        "description": (
            "取一份**文本优先**的本机界面状态：目标窗口的可交互元素（带元素索引 element_index、"
            "元素令牌 element_token 与 snapshot_id）、窗口清单与真前台窗口。"
            "默认不下发截图（纯文本通道），并已裁掉只读文本元素。"
            "要点：元素索引只在与它同源的 snapshot_id 内有效，动作时要把 snapshot_id 一起带上；"
            "窗口很大时用 query 只取匹配元素（只回匹配行 + 其祖先链，体量小一个数量级）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "按应用名或窗口标题的子串选窗口（不区分大小写）"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口（最精确）"},
                "query": {"type": "string", "description": "只取匹配该子串的元素＋祖先链（大窗口省钱关键）"},
                "max_elements": {"type": "integer", "description": "最多返回多少元素（默认读项目配置）"},
                "frames": {"type": "boolean", "description": "是否附像素坐标（默认不带：元素索引优先）"},
                "allow_minimized": {"type": "boolean", "description": "是否接受最小化窗口（其坐标是哨兵值）"},
                "no_windows": {"type": "boolean", "description": "只要元素，不返回窗口清单与前台窗口"},
            },
        },
    }


def _act_schema() -> dict:
    return {
        "name": "winuse_act",
        "description": (
            "对界面元素下发一个动作（点击/写值/输入/按键/滚动/激活窗口）。"
            "**默认干跑**：只返回将要下发的调用，不碰桌面；确认无误后再传 execute=true 执行。"
            "用元素索引时必须同时给 snapshot_id（来自取状态的那一次调用），否则会被拒绝——"
            "索引只在同源快照内有效，拿过期索引可能点到别的元素。"
            "投递默认后台进行，不会抢走用户的前台焦点。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS), "description": "动作名"},
                "app": {"type": "string", "description": "按应用名或窗口标题的子串选窗口"},
                "pid": {"type": "integer", "description": "按进程号选窗口"},
                "window_id": {"type": "integer", "description": "按窗口标识选窗口"},
                "element_index": {"type": "integer", "description": "元素索引（来自 winuse_state）"},
                "element_token": {"type": "string", "description": "元素令牌（来自 winuse_state，可免快照）"},
                "snapshot": {"type": "string", "description": "快照标识（来自 winuse_state，用索引时必填）"},
                "value": {"type": "string", "description": "set_value 的新值"},
                "text": {"type": "string", "description": "type 要输入的文字"},
                "key": {"type": "string", "description": "key 的按键名（return/tab/escape/down…）"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "scroll 方向"},
                "amount": {"type": "integer", "description": "scroll 刻度数（默认 3）"},
                "count": {"type": "integer", "description": "点击次数（2=双击）"},
                "allow_minimized": {"type": "boolean", "description": "接受最小化窗口"},
                "execute": {"type": "boolean", "description": "true 才真正执行；默认 false 只干跑"},
                "confirm": {
                    "type": "boolean",
                    "description": "确认危险动作（结束进程／系统级窗口／关闭类动作）；不带则返回 CONFIRM_REQUIRED 且不下发",
                },
            },
            "required": ["action"],
        },
    }


def _display_schema() -> dict:
    return {
        "name": "winuse_display",
        "description": (
            "取多显示器事实：每块屏的物理像素矩形、工作区、**各屏独立缩放比**与 DPI，"
            "以及每个窗口落在哪块屏（含最小化/离屏标记）。"
            "注意各屏缩放不同（如 150% 与 100% 混用），禁止用全局缩放估值换算坐标。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"no_windows": {"type": "boolean", "description": "只看显示器，不列窗口"}},
        },
    }


def _catalog() -> list[dict]:
    """工具表以**核心**为唯一出处（避免门面各写一份 schema 造成口径漂移）。

    核心读不到时退回内置最小集——桥接本来就依赖核心，但让插件在核心缺失时
    仍能注册出可用的报错路径（返回 CORE_NOT_FOUND），而不是整个工具面消失。
    """
    try:
        raw = _call_core()("__catalog__", {})
        payload = json.loads(raw)
        tools = payload.get("tools") or []
        if payload.get("ok") and tools:
            return [
                {
                    "name": t["name"],
                    "description": t.get("description") or t["name"],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
    except Exception as exc:  # noqa: BLE001 - 兜底路径
        logger.warning("从核心读工具表失败，退回内置最小集：%s", exc)
    return [_state_schema(), _act_schema(), _display_schema()]


def register(ctx) -> None:
    """注册工具（工具表由**核心**提供：各门面共享同一份，不各写一份 schema）。"""
    call_core = _call_core()
    tools = _catalog()
    for schema in tools:
        name = schema["name"]
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=(lambda tool: lambda args, **kw: call_core(tool, args or {}))(name),
            description=schema.get("description") or name,
            emoji=EMOJI.get(name, "🧰"),
        )
    logger.info("winuse 已注册 %d 个工具：%s", len(tools), ", ".join(s["name"] for s in tools))
