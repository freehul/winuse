"""winuse_desktop：窗口状态动作 + 虚拟桌面探测（PR-8，纯 ctypes，零第三方依赖）。

为什么单独一个模块（不塞进 state/act）：驱动提供的是**元素级**动作
（click / type_text / scroll），而窗口自身的**状态类**动作（最小化 / 最大化 /
还原 / 关闭 / 置顶 / 移动缩放 / 激活）在驱动里没有稳定对口。PR-4 的
`winuse display` 已经证明"ctypes 直连 user32"这条路径可行且坐标口径可控，
本模块沿用同一套做法与同一套坐标口径（原生桌面**物理像素**）。

四条关键纪律：

1. **默认干跑**：`window_action(..., execute=False)` 只回"将要下发的 user32
   调用清单"，一行都不真发。窗口状态属于用户桌面，误改的代价远高于误读。
2. **计划与执行分离**：`build_window_calls` 是**纯函数**（可穷举断言：给什么
   spec 就产什么调用），`window_action` 只负责选目标 / 执行 / 收口错误；
   原生调用集中在 `NativeUser32` 边界类里，可整体替换（单测注入桩，
   逐字段断言参数，全程不碰真实桌面）。
3. **返回值不可全信**（实测，别把"调用成功"当"意图达成"）：
   - `PostMessageW(hwnd=0, WM_CLOSE)` 实测返回 1（"投递成功"），即
     `close` 是 fire-and-forget，**不许**拿返回值当"窗口已关"的证据；
   - `GetWindowLongPtrW` 失败时返回 0，与"样式真的是 0"无法区分，
     必须配合 `GetLastError`（错误 1400 = ERROR_INVALID_WINDOW_HANDLE）；
   - `SetForegroundWindow` 在前台进程不是自己时按系统策略直接失败（返回 0），
     这是**预期行为**而不是 bug，不许重试到成功为止。
4. **错误统一结构化**：`window_action` 不把 `WinUseError` 抛给上层
   （门面给 agent 用，返回里的 `code` 更好分支）；纯函数 `build_window_calls`
   仍然抛带稳定 code 的异常，便于单测穷举错误契约。
5. **安全判定下沉到核心（下发前）**：门面拿不到窗口动作的解析结果（target 由 core
   自己选），所以"目标是系统级窗口"这条规则在门面侧无法判——它必须在**解析出目标
   之后、下发之前**由本模块判（`assess_window_target` + `window_action` 的闸门）。
   判据一律复用 `winuse.core.guard`，本模块**不另写一份危险清单**。

坐标口径：`move_resize` 的 x/y/width/height 与 `display.describe()` 里的
`bounds` **同口径**（物理像素的原生桌面坐标，已声明 Per-Monitor v2 DPI 感知）。
左竖屏原点是负值（如 `-2160,-890`）属正常，**不当非法值拒绝**；
只有"远超屏面的极值"（<= `display.SENTINEL_LIMIT`）才是最小化哨兵坐标。
"""

from __future__ import annotations

import ctypes
import math
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from ..driver.errors import MissingParam, UnknownAction, WindowMinimized, WinUseError
from . import guard as guard_mod
from .display import SENTINEL_LIMIT, _declare_dpi_awareness
from .state import StateOptions, pick_window


class BadParam(WinUseError):
    """参数**存在但取值非法**（宽高 <= 0、坐标是哨兵值、布尔位给了非布尔）。

    为什么定义在本模块而不上提 errors.py：`winuse/driver/errors.py` 是既有共享
    文件（本 PR 禁改），而 BAD_PARAM 目前只被本模块的动作参数校验使用。就近声明
    可避免为了一个错误码去动共享文件；若后续有第二个模块需要 BAD_PARAM，
    应上提到 `winuse.driver.errors` 并同步 `__all__`。
    """

    code = "BAD_PARAM"


# --------------------------------------------------------------------------
# 常量：Win32 符号的**显式字面量**（不依赖 ctypes.wintypes 是否导出这些宏）
# --------------------------------------------------------------------------

#: ShowWindow 的 nCmdShow
SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9

#: 窗口消息
WM_CLOSE = 0x0010

#: GetWindowLongPtr / SetWindowLongPtr 的索引
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

#: SetWindowPos 的 hWndInsertAfter
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

#: SetWindowPos 的 uFlags
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

#: 本模块支持的动作全集
WINDOW_ACTIONS = frozenset(
    {
        "minimize",
        "maximize",
        "restore",
        "close",
        "activate",
        "always_on_top",
        "move_resize",
    }
)

#: 需要的坐标参数（move_resize 专用）
MOVE_RESIZE_PARAMS = ("x", "y", "width", "height")

#: 原生调用"语义上确实失败"时的错误码（R-8）
WINDOW_CALL_FAILED = "WINDOW_CALL_FAILED"

DRY_RUN_NOTE = "干跑：未下发任何窗口调用（显式 execute=True 才真实执行）"
EXECUTED_NOTE = "已真实下发（窗口状态可能已改变）"

#: 判"目标维度"时传给 guard 的动作名：它不在 `guard.DANGEROUS_ACTIONS` 里，
#: 于是判定结果**只由目标窗口**决定。仅供 `assess_window_target`（目标维度专用）使用；
#: `window_action` 主路径用 `assess_window_action`，两个维度一起判（见下）。
TARGET_ONLY_ACTION = ""

#: 调用描述里的 func → NativeUser32 方法名（执行派发用同一张表，避免两处漂移）
_FUNC_TO_METHOD = {
    "ShowWindow": "show_window",
    "SetWindowPos": "set_window_pos",
    "SetForegroundWindow": "set_foreground_window",
    "PostMessageW": "post_message",
    "GetWindowLongPtrW": "get_window_long_ptr",
}


@dataclass
class WindowActionSpec:
    """一个窗口动作的完整描述（门面把它翻译成这个，业务逻辑只在这里）。

    目标窗口三选一（优先级 window_id > pid > app，与 `state.pick_window` 一致），
    都不给时退回"系统前台窗口"。
    """

    action: str
    pid: int | None = None
    window_id: int | None = None
    app: str | None = None
    #: move_resize 专用：原生桌面物理像素（与 display 的 bounds 同口径）
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None
    #: always_on_top 专用：必须是真布尔，不接受 "true"/1 这类模糊值
    always_on_top: bool | None = None


# --------------------------------------------------------------------------
# 纯逻辑：参数校验 + 动作 → user32 调用清单
# --------------------------------------------------------------------------


def _as_int(value: Any, name: str) -> int:
    """整数校验：拒绝 bool（bool 是 int 子类，最容易蒙混过关）、拒绝非整数值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadParam(
            f"{name} 必须是整数",
            f"收到 {type(value).__name__}：{value!r}",
        )
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise BadParam(f"{name} 必须是整数", f"收到非整数值：{value!r}")
        return int(value)
    return int(value)


def _check_coordinate(value: int, name: str) -> int:
    """坐标合理性：负值是正常的（左竖屏原点在负区），只有哨兵级极值才拒绝。"""
    if value <= SENTINEL_LIMIT:
        raise BadParam(
            f"{name}={value} 疑似最小化哨兵坐标（<= {SENTINEL_LIMIT}）",
            "最小化窗口的坐标是哨兵值，不是真实位置；"
            "要挪动它请先 restore，或改用元素索引类动作",
        )
    return value


def _call(func: str, args: list[Any], labels: list[str]) -> dict[str, Any]:
    """一条待下发的原生调用（func 为 Win32 导出名，args 为位置参数，labels 供人读）。"""
    return {"func": func, "args": args, "labels": labels}


def build_window_calls(spec: WindowActionSpec, target: dict) -> list[dict]:
    """纯函数：动作 → 将要下发的 user32 调用清单（**不执行、不读状态**）。

    参数不全 / 动作未知 / 取值非法一律抛带稳定 code 的 `WinUseError`：
    - `UNKNOWN_ACTION`（动作不在 `WINDOW_ACTIONS`）
    - `MISSING_PARAM`（move_resize 缺坐标；always_on_top 缺布尔）
    - `BAD_PARAM`（宽高 <= 0、坐标是哨兵值、布尔位给了非布尔）
    - `WINDOW_MINIMIZED`（对最小化窗口做 move_resize：它的坐标是哨兵值）
    """
    action = spec.action
    if action not in WINDOW_ACTIONS:
        raise UnknownAction(
            f"不支持的窗口动作：{action}",
            "可用：" + "、".join(sorted(WINDOW_ACTIONS)),
        )

    hwnd = target.get("window_id")
    if not isinstance(hwnd, int) or isinstance(hwnd, bool):
        raise BadParam(
            "目标窗口缺少可用的 window_id",
            f"收到 {hwnd!r}；window_id 应来自 list_windows 的原始句柄值",
        )

    if action == "minimize":
        return [_call("ShowWindow", [hwnd, SW_MINIMIZE], ["hwnd", "SW_MINIMIZE"])]

    if action == "maximize":
        return [_call("ShowWindow", [hwnd, SW_MAXIMIZE], ["hwnd", "SW_MAXIMIZE"])]

    if action == "restore":
        return [_call("ShowWindow", [hwnd, SW_RESTORE], ["hwnd", "SW_RESTORE"])]

    if action == "close":
        # WM_CLOSE（不是 TerminateProcess / WM_DESTROY）：让应用有机会弹"是否保存"，
        # 也避免强杀造成数据丢失。返回值不可信，见模块 docstring 纪律 3。
        return [_call("PostMessageW", [hwnd, WM_CLOSE, 0, 0], ["hwnd", "WM_CLOSE", "wParam", "lParam"])]

    if action == "activate":
        calls: list[dict] = []
        if target.get("minimized"):
            # 最小化窗口拿不到前台，必须先还原：顺序反了这一动作会静默失效
            calls.append(_call("ShowWindow", [hwnd, SW_RESTORE], ["hwnd", "SW_RESTORE"]))
        calls.append(_call("SetForegroundWindow", [hwnd], ["hwnd"]))
        return calls

    if action == "always_on_top":
        if spec.always_on_top is None:
            raise MissingParam(
                "always_on_top 需要明确的目标状态",
                "请给 --on-top true / false（不猜，避免把用户窗口莫名置顶）",
            )
        if not isinstance(spec.always_on_top, bool):
            raise BadParam(
                f"always_on_top 必须是布尔值，收到 {type(spec.always_on_top).__name__}",
                "请用 true / false（不要用 1/0 或 \"yes\"，避免歧义）",
            )
        insert_after = HWND_TOPMOST if spec.always_on_top else HWND_NOTOPMOST
        label = "HWND_TOPMOST" if spec.always_on_top else "HWND_NOTOPMOST"
        return [
            _call(
                "SetWindowPos",
                [hwnd, insert_after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE],
                ["hwnd", label, "x", "y", "cx", "cy", "flags"],
            )
        ]

    # move_resize
    missing = [n for n in MOVE_RESIZE_PARAMS if getattr(spec, n) is None]
    if missing:
        raise MissingParam(
            "move_resize 缺少参数：" + "、".join(missing),
            "四个都要给（原生桌面物理像素）：--x --y --width --height",
        )
    if target.get("minimized"):
        raise WindowMinimized(
            "目标窗口最小化，其坐标是哨兵值，不能作为 move_resize 依据",
            "先执行 restore，再 move_resize；或加 --allow-minimized 后改用元素类动作",
        )
    x = _check_coordinate(_as_int(spec.x, "x"), "x")
    y = _check_coordinate(_as_int(spec.y, "y"), "y")
    width = _as_int(spec.width, "width")
    height = _as_int(spec.height, "height")
    if width <= 0 or height <= 0:
        raise BadParam(
            f"宽高必须为正数，收到 width={width}, height={height}",
            "0 或负数的窗口尺寸会被系统拒绝（SWP 直接失败）",
        )
    # 只移动/缩放，不动 z 序、不抢前台：改位置不该顺带改变用户的窗口层级与焦点
    return [
        _call(
            "SetWindowPos",
            [hwnd, 0, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE],
            ["hwnd", "HWND_IGNORED(因 SWP_NOZORDER)", "x", "y", "cx", "cy", "flags"],
        )
    ]


# --------------------------------------------------------------------------
# 原生调用边界（唯一直接碰 ctypes 的地方，整体可替换）
# --------------------------------------------------------------------------

_IS_64BIT = ctypes.sizeof(ctypes.c_void_p) == 8
_ERROR_INVALID_WINDOW_HANDLE = 1400


def _bind_user32(u) -> None:
    """显式声明 argtypes/restype——不声明的话 64 位句柄会被截断成 32 位。"""
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    u.SetWindowPos.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    # 64 位系统用 GetWindowLongPtrW，32 位系统只有 GetWindowLongW
    get = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
    get.argtypes = [wintypes.HWND, ctypes.c_int]
    get.restype = ctypes.c_longlong if _IS_64BIT else ctypes.c_long


_USER32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
if _USER32 is not None:  # pragma: no cover - 平台相关
    _bind_user32(_USER32)


class NativeUser32:
    """user32 调用边界：每个方法一次原生调用，返回原始返回值（不做语义包装）。

    单测注入自己的同形对象即可全程离线；`last_error()` 用于区分
    "真的返回 0" 与 "调用失败返回 0"（见模块 docstring 纪律 3）。
    """

    def __init__(self, user32: Any = None):
        self._u = user32 if user32 is not None else _USER32
        if self._u is None:
            raise WinUseError(
                "当前平台不是 Windows，无法调用 user32",
                "窗口动作是 Windows 专属能力（ctypes → ShowWindow / SetWindowPos）",
            )

    def last_error(self) -> int:
        return int(ctypes.get_last_error())

    def show_window(self, hwnd: int, cmd: int) -> bool:
        ctypes.set_last_error(0)
        return bool(self._u.ShowWindow(hwnd, cmd))

    def set_window_pos(
        self, hwnd: int, insert_after: int, x: int, y: int, cx: int, cy: int, flags: int
    ) -> bool:
        ctypes.set_last_error(0)
        return bool(self._u.SetWindowPos(hwnd, insert_after, x, y, cx, cy, flags))

    def set_foreground_window(self, hwnd: int) -> bool:
        ctypes.set_last_error(0)
        return bool(self._u.SetForegroundWindow(hwnd))

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
        ctypes.set_last_error(0)
        return bool(self._u.PostMessageW(hwnd, msg, wparam, lparam))

    def get_window_long_ptr(self, hwnd: int, index: int) -> int:
        ctypes.set_last_error(0)
        get = getattr(self._u, "GetWindowLongPtrW", None) or self._u.GetWindowLongW
        return int(get(hwnd, index))

    def set_window_long_ptr(self, hwnd: int, index: int, value: int) -> int:
        """写窗口样式位（边界能力，当前动作集**不使用**它）。

        `always_on_top` 走 `SetWindowPos(HWND_TOPMOST)`——那是微软文档给出的
        置顶方式；手工改 `GWL_EXSTYLE` 的 WS_EX_TOPMOST 位容易与窗口管理器的
        内部状态脱节。保留这个入口是为了边界完整（单测可注入并断言参数），
        而不是让上层随手改样式。
        """
        ctypes.set_last_error(0)
        setter = getattr(self._u, "SetWindowLongPtrW", None) or self._u.SetWindowLongW
        return int(setter(hwnd, index, value))

    def is_topmost(self, hwnd: int) -> bool | None:
        """读当前是否置顶。取不到（无效句柄）时返回 None 而不是 False。"""
        ex_style = self.get_window_long_ptr(hwnd, GWL_EXSTYLE)
        if ex_style == 0 and self.last_error() == _ERROR_INVALID_WINDOW_HANDLE:
            return None
        return bool(ex_style & WS_EX_TOPMOST)


#: DPI 感知声明结果（首次真实执行时声明一次，之后复用）
_DPI_STATE: str | None = None


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def _resolve_target(driver, spec: WindowActionSpec) -> dict:
    """选目标窗口：复用 `state.pick_window`（不复制一份选择语义）。

    注意 `allow_minimized=True`：窗口动作**本来**就要处理最小化窗口
    （restore / activate 的对象就是它），所以这里不沿用 state 的默认拒绝；
    "最小化窗口不能做坐标类动作"这条约束改到 `build_window_calls` 里按动作判。
    """
    resp = driver.call("list_windows", {})
    windows = resp.get("windows") if isinstance(resp, dict) else resp
    windows = windows or []
    opts = StateOptions(
        app=spec.app,
        pid=spec.pid,
        window_id=spec.window_id,
        allow_minimized=True,
    )
    return pick_window(windows, opts)


# --------------------------------------------------------------------------
# 下发前的安全判定（判据单一出处：winuse.core.guard）
# --------------------------------------------------------------------------


def assess_window_action(action: str, target: dict | None) -> guard_mod.RiskVerdict:
    """危险判定（**动作名 + 目标两个维度**，判据单一出处 `core.guard`）。

    为什么必须在核心判：窗口动作的目标由 core 自己选（`_resolve_target`），门面在
    下发前只拿到动作名、拿不到 target——所以"任务栏 / 资源管理器 / 设置页"这条规则
    在门面侧根本判不了（T-23）。判在这里，`window_action` 就能在**下发前**拦。

    为什么现在连动作名维度一起判（R-12）：只判目标维度时，任何**直接调核心**的路径
    （新增门面、内部调用、脚本）都能在未确认的情况下关掉普通窗口——核心安全边界不
    自包含。原先把动作名维度留在门面的理由是"门面没有通道把确认状态传进核心，核心
    再拦一次会让 `--confirm` 也过不去"；但这个前提已经作废：`window_action` 现在有
    `confirmed` 参数（CLI `--confirm` / MCP `confirm=true` 已透传，见
    `tests/test_window_confirm_wiring.py`），确认过得去，所以两个维度都能下沉。

    判据没有第二份：仍然调 `guard.assess_action`（危险动作清单、系统窗口清单
    `SYSTEM_APPS` / `SYSTEM_TITLE_PATTERNS` 都只存在于 `guard`，本模块不复制一份）。
    """
    return guard_mod.assess_action(action, target)


def assess_window_target(action: str, target: dict | None) -> guard_mod.RiskVerdict:
    """**只判目标维度**（`action` 不参与判定）——目标维度专用的窄口径。

    保留它有两个用途：① 只想问"这个窗口是不是系统级窗口"的调用方；
    ② 既有用例明确钉住"目标维度单独看是什么结论"。主路径请用
    `assess_window_action`（两个维度一起判，才是下发前的那道闸）。
    """
    return guard_mod.assess_action(TARGET_ONLY_ACTION, target)


def _blocked(
    spec: WindowActionSpec,
    verdict: guard_mod.RiskVerdict,
    execute: bool,
    target: dict,
    audit_written: bool | None = None,
) -> dict:
    """危险且未确认 → 结构化拒绝（形状与 `core.act.run_act` 的拦截一致）。

    拒绝时**不返回任何调用计划**：计划本身没有价值，agent 该做的是换目标或先确认，
    拿一份"会下发什么"反而容易被误当成"已下发过"。
    """
    out = {
        "ok": False,
        "dry_run": not execute,
        "action": spec.action,
        "target": _slim_target(target),
        "would_call": None,
        "code": verdict.code,
        "requires_confirm": True,
        "danger": verdict.to_dict(),
        "error": {
            "code": verdict.code,
            "message": f"窗口动作 {spec.action} 被安全层拦截：{verdict.reason}",
            "detail": (
                f"规则：{verdict.rule}；确认方式：CLI 加 --confirm / MCP 传 confirm=true"
            ),
        },
    }
    if audit_written is not None:
        out["audit_written"] = audit_written
    return out


def _write_window_audit(audit, event: str, spec: WindowActionSpec, *, ok: bool,
                        code: str | None, target: dict | None, confirmed: bool) -> bool | None:
    """窗口动作的审计（复用 `core.act._audit` 的 sink 协议，不另立一套）。"""
    from ..audit import sink_event

    return sink_event(
        audit,
        event=event,
        action=spec.action,
        ok=ok,
        code=code,
        target=target,
        detail={"confirmed": bool(confirmed), "window_action": True},
    )


#: 每个 user32 函数返回值的**语义**（R-8）。不能统一按"真/假 = 成功/失败"：
#:
#: - `ShowWindow` 返回的是**调用前的可见性**（微软文档原文：the return value
#:   specifies whether the window was previously visible）——"首次最小化可见窗口"
#:   返回 true、"恢复不可见窗口"返回 false，两者都不是成败。
#: - `SetWindowPos` / `SetForegroundWindow` 返回 BOOL，0 才是失败。
#: - `PostMessageW` 返回"消息是否投递成功"，**不代表窗口已关闭**（真机实测
#:   hwnd=0 也可能返回 1）。
#: - `GetWindowLongPtrW` 返回的是数据本身（Windows 惯例：0 时需自行查 last_error）。
_FUNC_RESULT_SEMANTICS = {
    "ShowWindow": "previously_visible",
    "SetWindowPos": "bool_success",
    "SetForegroundWindow": "bool_success",
    "PostMessageW": "posted",
    "GetWindowLongPtrW": "raw_value",
}


def _call_result(func: str, ret: Any, native: Any) -> dict:
    """把一次原生调用的返回值翻成"带语义的结果"（R-8）。

    顶层 `ok` 的判据只用这里的 `succeeded`：只有明确语义为成败的调用才会让它变 false。
    """
    semantics = _FUNC_RESULT_SEMANTICS.get(func, "bool_success")
    item: dict[str, Any] = {"result": ret, "semantics": semantics}
    if semantics == "previously_visible":
        item["previously_visible"] = bool(ret)
    elif semantics == "posted":
        item["posted"] = bool(ret)
        item["succeeded"] = bool(ret)
    elif semantics == "raw_value":
        item["value"] = ret
    else:
        item["succeeded"] = bool(ret)
    if item.get("succeeded") is False:
        # 只有"确实是失败"的语义才带 last_error——合法返回值不该被记成错误
        item["last_error"] = native.last_error()
    return item


def _run_calls(calls: list[dict], spec: WindowActionSpec, native: Any) -> list[dict]:
    """逐条下发。只有 `always_on_top` 有额外语义（已是目标状态就不发）。"""
    results: list[dict] = []
    for call in calls:
        want_top = spec.action == "always_on_top" and spec.always_on_top
        if spec.action == "always_on_top":
            current = native.is_topmost(call["args"][0])
            if current is not None and current == bool(want_top):
                results.append(
                    {
                        "func": call["func"],
                        "args": call["args"],
                        "labels": call["labels"],
                        "called": False,
                        "reason": "已是目标置顶状态，跳过",
                    }
                )
                continue

        method = getattr(native, _FUNC_TO_METHOD[call["func"]])
        ret = method(*call["args"])
        item: dict[str, Any] = {
            "func": call["func"],
            "args": call["args"],
            "labels": call["labels"],
            "called": True,
            **_call_result(call["func"], ret, native),
        }
        results.append(item)
    return results


def _failed_calls(executed: list[dict]) -> list[dict]:
    """把"语义上确实失败"的调用挑出来（ShowWindow 的 false 不算）。"""
    return [item for item in executed if item.get("succeeded") is False]


def window_action(
    driver,
    spec: WindowActionSpec,
    *,
    execute: bool = False,
    native: Any = None,
    confirmed: bool = False,
    audit=None,
) -> dict:
    """对目标窗口下发一个状态动作（**默认干跑**）。

    返回结构（成功）：

        {"ok": true, "dry_run": bool, "action": str,
         "target": {"pid": int, "window_id": int, "title": str},
         "would_call": {"calls": [...], "note": str}, "code": None}

    失败（参数校验 / 选窗口 / 驱动报错）不抛异常，统一收口成：

        {"ok": false, "dry_run": bool, "action": str, "target": None,
         "would_call": None, "code": "UNKNOWN_ACTION", "error": {...}}

    安全判定（下发前的最后一道闸，T-23）：目标窗口解析出来之后、任何调用下发之前，
    按**目标**判危险（`assess_window_target`——判据来自 `core.guard`，重点是目标是
    系统级窗口：任务栏 / 资源管理器 / 设置页，即使动作只是 minimize 也拦）。危险且
    未 `confirmed=True` 时返回（干跑与真跑一样拦，免得干跑通过、真跑被拒成陷阱）：

        {"ok": false, "requires_confirm": true, "code": "CONFIRM_REQUIRED",
         "danger": {"dangerous": true, "code": ..., "reason": ..., "rule": ...},
         "target": {...}, "would_call": null, "error": {...}}

    `confirmed` 由门面透传（CLI `--confirm` / MCP `confirm=true`），与 `core.act`
    的危险动作口径一致：危险 + 显式确认 → 放行；未确认 → 拦且不下发。

    `code` 的取值来自 `winuse.driver.errors` 的既有错误码 + 本模块的
    `BAD_PARAM`。`execute=True` 时额外带 `executed`（逐条真实返回值）与
    `dpi_awareness`；`always_on_top` 会带 `on_top_verified`（改完回读复核，
    不拿"调用没报错"当"已生效"）。
    """
    global _DPI_STATE

    target: dict | None = None
    try:
        target = _resolve_target(driver, spec)
        # 闸门在**下发前**，判的是"解析出来的目标 + 动作名"两个维度（R-12：
        # 只判目标维度时，直接调核心的路径可以未确认就关掉普通窗口）
        danger = assess_window_action(spec.action, target)
        if danger.dangerous and not confirmed:
            written = _write_window_audit(
                audit, "window.blocked", spec, ok=False, code=danger.code,
                target=target, confirmed=confirmed,
            )
            return _blocked(spec, danger, execute, target, written)
        calls = build_window_calls(spec, target)
    except WinUseError as exc:
        out = _failure(spec, exc, execute)
        if target is not None:
            out["target"] = _slim_target(target)
        return out

    out: dict[str, Any] = {
        "ok": True,
        "dry_run": not execute,
        "action": spec.action,
        "target": _slim_target(target),
        "would_call": {
            "calls": calls,
            "note": DRY_RUN_NOTE if not execute else EXECUTED_NOTE,
        },
        "code": None,
    }
    if not execute:
        written = _write_window_audit(
            audit, "window.dry_run", spec, ok=True, code=None,
            target=target, confirmed=confirmed,
        )
        if written is not None:
            out["audit_written"] = written
        return out

    # 坐标口径：首次真实执行前声明 DPI 感知，否则 move_resize 会拿到被虚拟化的逻辑坐标
    if _DPI_STATE is None:
        _DPI_STATE = _declare_dpi_awareness()
    try:
        native = native or NativeUser32()
        out["executed"] = _run_calls(calls, spec, native)
        if spec.action == "always_on_top":
            out["on_top_verified"] = native.is_topmost(target["window_id"])
        # 顶层 ok 必须反映真实失败（R-8）：原先把失败混在 executed 里、顶层仍 ok=true，
        # agent 读不出"这次动作到底成没成"
        failures = _failed_calls(out["executed"])
        if failures:
            detail = "；".join(
                f"{item['func']} 返回 {item.get('result')!r}（last_error={item.get('last_error')}）"
                for item in failures
            )
            out["ok"] = False
            out["code"] = WINDOW_CALL_FAILED
            out["error"] = {
                "code": WINDOW_CALL_FAILED,
                "message": "窗口动作有调用失败：" + "、".join(i["func"] for i in failures),
                "detail": detail,
            }
    except WinUseError as exc:
        _write_window_audit(
            audit, "window.failed", spec, ok=False, code=exc.code,
            target=target, confirmed=confirmed,
        )
        return _failure(spec, exc, execute, target)
    out["dpi_awareness"] = _DPI_STATE
    written = _write_window_audit(
        audit, "window.executed", spec, ok=True, code=None,
        target=target, confirmed=confirmed,
    )
    if written is not None:
        out["audit_written"] = written
    return out


def _slim_target(target: dict) -> dict:
    return {
        "pid": target.get("pid"),
        "window_id": target.get("window_id"),
        "title": target.get("title"),
    }


def _failure(
    spec: WindowActionSpec, exc: WinUseError, execute: bool, target: dict | None = None
) -> dict:
    return {
        "ok": False,
        "dry_run": not execute,
        "action": getattr(spec, "action", None),
        "target": _slim_target(target) if target else None,
        "would_call": None,
        "code": exc.code,
        "error": exc.to_dict(),
    }


# --------------------------------------------------------------------------
# 虚拟桌面探测（IVirtualDesktopManager）
# --------------------------------------------------------------------------

#: IVirtualDesktopManager —— Windows 10 起的**公开**接口（不是内部未文档化接口）
CLSID_VIRTUAL_DESKTOP_MANAGER = "{AA509086-5CA9-4C25-8F95-589D3C07B48A}"
IID_IVIRTUAL_DESKTOP_MANAGER = "{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}"

#: vtable 槽位（IUnknown 占 0/1/2）
VTBL_IS_WINDOW_ON_CURRENT = 3
VTBL_GET_WINDOW_DESKTOP_ID = 4
VTBL_MOVE_WINDOW_TO_DESKTOP = 5

CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2

_RPC_E_CHANGED_MODE = 0x80010106
_CO_E_NOTINITIALIZED = 0x800401F0  # CoCreateInstance 前没 CoInitialize（实测踩到）
_REGDB_E_CLASSNOTREG = 0x80040154
_E_INVALIDARG = 0x80070057
_TYPE_E_ELEMENTNOTFOUND = 0x8002802B  # 桌面跟踪不到该窗口（实测最常见）
ZERO_GUID = "{00000000-0000-0000-0000-000000000000}"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self) -> str:
        d4 = "".join(f"{b:02X}" for b in self.Data4)
        return f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-{d4[:4]}-{d4[4:]}}}"


def _guid_from_string(text: str) -> _GUID:
    ole32 = ctypes.WinDLL("ole32")
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
    ole32.CLSIDFromString.restype = ctypes.c_long
    out = _GUID()
    hr = int(ole32.CLSIDFromString(text, ctypes.byref(out)))
    if hr != 0:  # pragma: no cover - 常量写错才会走到
        raise WinUseError(f"CLSID 字符串非法：{text}", f"CLSIDFromString hr=0x{hr & 0xFFFFFFFF:08X}")
    return out


_HRESULT_REASONS = {
    _TYPE_E_ELEMENTNOTFOUND: (
        "系统未跟踪该窗口的虚拟桌面归属（TYPE_E_ELEMENTNOTFOUND）。这是**逐窗口**"
        "限制而非接口不可用：实测 350 个顶层窗口里只有 9 个可读，正常应用主窗口能读，"
        "外壳窗口 / 悬浮层 / 消息窗 / Program Manager 读不到"
    ),
    _E_INVALIDARG: "窗口句柄无效（E_INVALIDARG）",
}


def interpret_desktop_query(
    hresult: int | None,
    desktop_id: str | None,
    on_current_hresult: int | None = None,
    on_current_flag: int | None = None,
) -> dict:
    """纯函数：原生返回值 → 结构化结论（虚拟桌面探测的**全部判定规则**集中在这里）。

    抽出来的理由：这些规则（HRESULT → 错误码、全 0 GUID 不算成功、出参只在
    hr==0 时才可信）是真正的业务逻辑，必须能脱离 COM 穷举断言；`query()` 只负责
    把 ctypes 的原始返回值喂进来。

    错误码语义：
    - `WINDOW_NOT_TRACKED`：接口能用，但**这个窗口**没有桌面归属（多为外壳/悬浮窗）
    - `NO_DESKTOP_ID`：hr==0 但 GUID 全 0（实测存在），不能当成功
    """
    hr = 0 if hresult is None else int(hresult) & 0xFFFFFFFF
    if hr != 0:
        return {
            "ok": False,
            "code": "WINDOW_NOT_TRACKED",
            "hresult": hr,
            "desktop_id": None,
            "on_current_desktop": None,
            "reason": _HRESULT_REASONS.get(hr, f"GetWindowDesktopId hr=0x{hr:08X}"),
        }

    if desktop_id == ZERO_GUID:
        return {
            "ok": False,
            "code": "NO_DESKTOP_ID",
            "hresult": 0,
            "desktop_id": ZERO_GUID,
            "on_current_desktop": None,
            "reason": "接口返回成功但 GUID 全 0（实测存在）：该窗口没有可用的桌面归属",
        }

    # 出参只在 hr==0 时可信：实测 IsWindowOnCurrentVirtualDesktop(hwnd=0) 报 E_INVALIDARG
    # 且完全不写出参，所以必须预置初值 + 只看 hr
    hr_cur = None if on_current_hresult is None else int(on_current_hresult) & 0xFFFFFFFF
    return {
        "ok": True,
        "code": None,
        "hresult": 0,
        "desktop_id": desktop_id,
        "on_current_desktop": bool(on_current_flag) if hr_cur == 0 else None,
        "current_query_hresult": hr_cur,
    }


class _ComSession:
    """COM 会话上下文：**先 CoInitialize 再 CoCreateInstance**。

    实测坑（本机实测踩到的，不是理论）：对 `IVirtualDesktopManager` 直接
    `CoCreateInstance` 会返回 `CO_E_NOTINITIALIZED (0x800401F0)`——必须先
    初始化 COM，否则会误判成"本机不支持虚拟桌面"。因此实例化统一走这里。

    初始化结果三分：`0` = 本次初始化（收尾由我们 CoUninitialize）；
    `S_FALSE(1)` = 本线程早已初始化（**不能**由我们收尾，否则会提前拆掉别人的
    apartment）；`RPC_E_CHANGED_MODE` = 本线程以别的模型初始化过，COM 仍可用。
    """

    def __init__(self) -> None:
        self.ole32: Any = None
        self.inited_here = False
        self.error: tuple[str, int | None, str] | None = None

    def __enter__(self) -> "_ComSession":
        try:
            ole32 = ctypes.WinDLL("ole32")
        except OSError as exc:  # pragma: no cover - 缺 ole32 才会走到
            self.error = ("UNSUPPORTED", None, f"ole32 不可用：{exc}")
            return self
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ole32.CoCreateInstance.restype = ctypes.c_long
        self.ole32 = ole32

        hr = int(ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)) & 0xFFFFFFFF
        if hr == 0:
            self.inited_here = True
        elif hr not in (1, _RPC_E_CHANGED_MODE):
            self.error = ("UNSUPPORTED", hr, f"CoInitializeEx 失败 hr=0x{hr:08X}")
        return self

    def ok(self) -> bool:
        return self.error is None

    def __exit__(self, *exc_info) -> None:
        # 显式不抑制异常（返回 None）——异常必须继续往上走，不能被 with 吞掉
        if self.inited_here and self.ole32 is not None:
            self.ole32.CoUninitialize()

    def error_dict(self) -> dict:
        code, hresult, reason = self.error or ("UNSUPPORTED", None, "")
        return {
            "ok": False,
            "code": code,
            "hresult": hresult,
            "desktop_id": None,
            "on_current_desktop": None,
            "reason": reason,
        }


class VirtualDesktopBackend:
    """`IVirtualDesktopManager` 的 ctypes 封装（仅**读**：不调用 MoveWindowToDesktop）。

    实测结论（Windows 11 24H2 / build 26100 / 64 位 / 普通权限）：

    - **必须先 CoInitialize 再 CoCreateInstance**：跳过初始化会拿到
      `CO_E_NOTINITIALIZED (0x800401F0)`，会被误读成"接口不存在"；
    - 初始化到位后 `CoCreateInstance(CLSCTX_INPROC_SERVER)` 返回 `0x0`，无需提权；
    - 槽位 3 = `IsWindowOnCurrentVirtualDesktop`、槽位 4 = `GetWindowDesktopId`；
    - 同一窗口连读 3 次 GUID 完全一致（稳定）；
    - 正常应用主窗口返回真实 GUID；外壳/悬浮/消息窗返回 `0x8002802B`；
    - 极少数窗口 `hr=0` 但 GUID 全 0（实测 3/9）→ 视为"没有可用数据"；
    - `IsWindowOnCurrentVirtualDesktop(hwnd=0)` 返回 `0x80070057` 且**不写出参**，
      所以出参必须预置初值、且只看 hr==0 的结果；
    - 槽位 5 = MoveWindowToDesktop **故意不接**：跨桌面搬窗口会改变用户桌面状态，
      不在本模块获准的动作集内（要做得先过审批门禁）。
    """

    def __init__(self) -> None:
        self.platform_ok = sys.platform == "win32"

    def probe(self) -> dict:
        """接口可用性探测：返回 {"ok": bool, "code": str|None, "reason": str?}。"""
        if not self.platform_ok:
            return {
                "ok": False,
                "code": "UNSUPPORTED",
                "reason": f"非 Windows 平台（{sys.platform}），IVirtualDesktopManager 不存在",
            }
        with _ComSession() as com:
            if not com.ok():
                return com.error_dict()
            try:
                clsid = _guid_from_string(CLSID_VIRTUAL_DESKTOP_MANAGER)
                iid = _guid_from_string(IID_IVIRTUAL_DESKTOP_MANAGER)
            except WinUseError as exc:  # pragma: no cover - 常量写错才会走到
                return {"ok": False, "code": "UNSUPPORTED", "reason": exc.message}
            ptr = self._create(com.ole32, clsid, iid)
            if isinstance(ptr, dict):
                return ptr
            self._release(ptr)
            return {"ok": True, "code": None}

    def query(self, hwnd: int) -> dict:
        """读单个窗口的虚拟桌面归属（只读）。

        返回 {"ok": bool, "code": str|None, "hresult": int|None, "desktop_id": str|None,
              "on_current_desktop": bool|None, "reason": str?}
        - 接口级失败（非 Windows / COM 起不来 / 类未注册）→ code=UNSUPPORTED
        - 该窗口不参与虚拟桌面 → code=WINDOW_NOT_TRACKED（带 hresult 原文）
        - hr==0 但 GUID 全 0 → code=NO_DESKTOP_ID（实测存在，不能当成功）
        """
        if not self.platform_ok:
            return {
                "ok": False,
                "code": "UNSUPPORTED",
                "hresult": None,
                "desktop_id": None,
                "on_current_desktop": None,
                "reason": f"非 Windows 平台（{sys.platform}）",
            }
        if not hwnd:
            return {
                "ok": False,
                "code": "NO_TARGET",
                "hresult": None,
                "desktop_id": None,
                "on_current_desktop": None,
                "reason": "没有可探测的窗口句柄（传 window_id 或保证有前台窗口）",
            }

        with _ComSession() as com:
            if not com.ok():
                return com.error_dict()
            try:
                clsid = _guid_from_string(CLSID_VIRTUAL_DESKTOP_MANAGER)
                iid = _guid_from_string(IID_IVIRTUAL_DESKTOP_MANAGER)
            except WinUseError as exc:  # pragma: no cover
                return dict(com.error_dict(), reason=exc.message)

            ptr = self._create(com.ole32, clsid, iid)
            if isinstance(ptr, dict):
                return dict(ptr, desktop_id=None, on_current_desktop=None)

            try:
                fns = ctypes.cast(
                    ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                    ctypes.POINTER(ctypes.c_void_p),
                )
                get_id = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID)
                )(fns[VTBL_GET_WINDOW_DESKTOP_ID])
                on_current = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
                )(fns[VTBL_IS_WINDOW_ON_CURRENT])

                guid = _GUID()
                hr = int(get_id(ptr, ctypes.c_void_p(hwnd), ctypes.byref(guid))) & 0xFFFFFFFF
                if hr != 0:
                    return interpret_desktop_query(hr, None)

                # 出参预置初值：接口失败时不写出参，读到的可能是上一次的残留
                flag = ctypes.c_int(0)
                hr_cur = int(on_current(ptr, ctypes.c_void_p(hwnd), ctypes.byref(flag))) & 0xFFFFFFFF
                return interpret_desktop_query(0, str(guid), hr_cur, flag.value)
            finally:
                self._release(ptr)

    # -- 内部：COM 实例化 / 释放 --------------------------------------------

    @staticmethod
    def _create(ole32, clsid: _GUID, iid: _GUID):
        ptr = ctypes.c_void_p()
        hr = int(ole32.CoCreateInstance(
            ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(ptr)
        )) & 0xFFFFFFFF
        if hr != 0 or not ptr.value:
            if hr == _CO_E_NOTINITIALIZED:
                reason = (
                    "COM 未初始化（CO_E_NOTINITIALIZED）——本接口要求先 CoInitialize，"
                    "请看 _ComSession"
                )
            elif hr == _REGDB_E_CLASSNOTREG:
                reason = "本机未注册 IVirtualDesktopManager（虚拟桌面组件缺失 / Server Core / 老系统）"
            else:
                reason = f"CoCreateInstance hr=0x{hr:08X}"
            return {"ok": False, "code": "UNSUPPORTED", "hresult": hr, "reason": reason}
        return ptr

    @staticmethod
    def _release(ptr) -> None:
        try:
            fns = ctypes.cast(
                ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p),
            )
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(fns[2])(ptr)
        except Exception:  # noqa: BLE001 - 释放失败不应影响调用方
            pass


def virtual_desktop_info(
    window_id: int | None = None, *, backend: Any = None
) -> dict:
    """读窗口所属虚拟桌面 id（只读，不动任何窗口）。

    实现方式：ctypes + COM 调公开接口 `IVirtualDesktopManager`
    （CLSID `{AA509086-5CA9-4C25-8F95-589D3C07B48A}` /
    IID `{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}`），槽位 4 `GetWindowDesktopId`
    取桌面 GUID，槽位 3 `IsWindowOnCurrentVirtualDesktop` 取"是否在当前桌面"。

    **本机实测（Windows 11 24H2 / build 26100 / 64 位 / 普通权限）：接口可用，
    不是 UNSUPPORTED**，因此这里返回真实 GUID 而不是 UNSUPPORTED。实测到的
    真实失败点（不是猜测，是逐条打出来的）：

    - `CoCreateInstance` hr=0x0，无需提权；同一窗口连读 3 次 GUID 完全一致；
    - **实例化前必须先 `CoInitialize`**：跳过初始化直接 `CoCreateInstance` 会拿到
      `CO_E_NOTINITIALIZED (0x800401F0)`（本机实测踩到，会被误读成"不支持"）；
    - 正常应用主窗口（Hermes / ZCode / LM Studio / 任务管理器 …）能读出 GUID；
    - **逐窗口**失败是常态：全量扫 350 个顶层窗口只有 9 个可读，其余 341 个返回
      `TYPE_E_ELEMENTNOTFOUND (0x8002802B)`（外壳窗口 Cua.AgentCursorOverlay、
      `Program Manager`、消息窗、隐藏窗）→ 这种走 `WINDOW_NOT_TRACKED`；
    - 9 个可读里另有 3 个 `hr=0` 但 GUID 全 0 → 走 `NO_DESKTOP_ID`，
      **不**把全 0 当有效 id；
    - `IsWindowOnCurrentVirtualDesktop(hwnd=0)` 返回 `0x80070057` 且不写出参，
      所以出参预置 0、只在 hr==0 时取值；
    - **未能验证**的部分（如实声明，不编）：本机只探测到 1 个非零桌面 GUID
      （注册表 VirtualDesktops 键下也没有可枚举的 VirtualDesktopIDs），
      所以"多桌面下不同窗口给出不同 GUID"这一点没验证——验证它需要切换用户的
      虚拟桌面，属明确禁止的副作用，故留作未验证项。

    返回：
        成功 {"ok": true, "code": null, "window_id": int, "desktop_id": "{GUID}",
              "on_current_desktop": bool|None, "probe": {...}}
        失败 {"ok": false, "code": "UNSUPPORTED|WINDOW_NOT_TRACKED|NO_DESKTOP_ID|NO_TARGET",
              "hresult": int|None, "reason": str}
    """
    backend = backend or VirtualDesktopBackend()

    if window_id is None:
        window_id = _foreground_window_id()
        if not window_id:
            return {
                "ok": False,
                "code": "NO_TARGET",
                "hresult": None,
                "reason": "没有前台窗口可供探测；请显式传 window_id",
            }

    got = backend.query(window_id)
    if not got.get("ok"):
        return {
            "ok": False,
            "code": got.get("code", "UNSUPPORTED"),
            "hresult": got.get("hresult"),
            "reason": got.get("reason", ""),
            "window_id": window_id,
        }
    return {
        "ok": True,
        "code": None,
        "window_id": window_id,
        "desktop_id": got["desktop_id"],
        "on_current_desktop": got.get("on_current_desktop"),
        "probe": {
            "interface": "IVirtualDesktopManager",
            "clsid": CLSID_VIRTUAL_DESKTOP_MANAGER,
            "iid": IID_IVIRTUAL_DESKTOP_MANAGER,
            "vtable_get_window_desktop_id": VTBL_GET_WINDOW_DESKTOP_ID,
            "move_window_to_desktop_exposed": False,
        },
    }


def _foreground_window_id() -> int:
    """系统前台窗口句柄（只读；无前台窗口返回 0）——**转调 `core.foreground`**。

    实现只有一份（R-2）：探针分散在两处会让"谁是前台"的口径漂移。
    """
    from . import foreground as fg_mod

    return fg_mod.foreground_window_id()


