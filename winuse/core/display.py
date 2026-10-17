"""winuse_display：显示器 / DPI / 窗口归属（PR-4，纯 ctypes，零第三方依赖）。

为什么要自己枚举：驱动只提供 `get_screen_size`（**只报主屏**），而本机是三屏。
且必须避开 .NET 在三屏下报"混合坐标"的坑（起点用物理值、宽高用逻辑值）。

三条关键做法：
1. **先声明 DPI 感知**（Per-Monitor v2）——不声明的话，系统会返回被虚拟化的逻辑坐标，
   与驱动的物理坐标混在一起就对不上。
2. **纯逻辑与原生调用分离**：`scale_from_dpi` / `pick_monitor` / `assign_windows_to_monitors`
   是纯函数（可穷举断言），`enum_monitors` 才是 ctypes 调用。
3. **最小化窗口不报坐标**：其 bounds 是哨兵值（实测 `-32000,-32000`），一律记为
   `placement: minimized`，不让上层拿到假坐标。注意左竖屏原点在负区（`-2160,-890`）
   属正常，哨兵判定用"远超屏面的极值"而不是"负坐标"。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

from .state import WINDOW_ORDER_NOTE, describe_focus

#: DPI 基准值与最小化哨兵阈值
BASE_DPI = 96
SENTINEL_LIMIT = -30000

#: 坐标口径说明（随 display 输出一起给上层，避免各处口径不一致）
COORDINATE_NOTE = (
    "bounds 为**物理像素**的原生桌面坐标（已声明 Per-Monitor v2 DPI 感知）；"
    "scale 是各屏独立缩放（1.5 表示 150%），不同屏不同值，禁止用全局估值换算；"
    "placement=minimized 的窗口没有可信坐标（哨兵值），坐标类动作不可用。"
)

#: 未拿到 Per-Monitor v2 时的**警示**：此时矩形可能已被系统虚拟化成逻辑坐标
VIRTUALIZED_NOTE = (
    " ⚠️ 本进程未拿到 Per-Monitor v2 DPI 感知（实际：{awareness}）："
    "上面的 bounds **可能是被系统虚拟化的逻辑坐标**，不能当物理像素直接用于点击/换算。"
)


def coordinate_note_for(awareness: str | None) -> str:
    """按**实际** awareness 给出坐标口径说明（R-7）。

    原先把"已是物理坐标"写死在常量里，无论声明成功与否都这么宣称——声明失败
    （`unchanged`）时那句话就是假的，而调用方会照着它去换算坐标。
    """
    if awareness == "per-monitor-v2":
        return COORDINATE_NOTE
    return COORDINATE_NOTE + VIRTUALIZED_NOTE.format(awareness=awareness or "unknown")


def bounds_are_physical(awareness: str | None) -> bool:
    """只有 Per-Monitor v2 才敢说"这些矩形就是物理像素"。"""
    return awareness == "per-monitor-v2"


def scale_from_dpi(dpi: int) -> float:
    """DPI → 缩放比（96 → 1.0）。取不到 DPI 时按 1.0，不用猜测值。"""
    if not dpi:
        return 1.0
    return round(dpi / BASE_DPI, 4)


def is_sentinel_bounds(bounds: dict | None) -> bool:
    """是否最小化哨兵坐标（远超所有屏面的极值）。负坐标本身不算哨兵。"""
    if not bounds:
        return False
    return bounds.get("x", 0) <= SENTINEL_LIMIT or bounds.get("y", 0) <= SENTINEL_LIMIT


def pick_monitor(monitors: list[dict], x: float, y: float) -> dict:
    """点落在哪个屏（左闭右开）。不在任何屏上时退回主屏。"""
    for m in monitors:
        b = m["bounds"]
        if b["x"] <= x < b["x"] + b["width"] and b["y"] <= y < b["y"] + b["height"]:
            return m
    primary = next((m for m in monitors if m.get("primary")), None)
    return primary or (monitors[0] if monitors else {})


def assign_windows_to_monitors(windows: list[dict], monitors: list[dict]) -> list[dict]:
    """给每个窗口判定：属于哪个屏、该屏的缩放比、placement 状态。

    - `minimized`：最小化（或坐标是哨兵值）→ 不给屏与缩放
    - `offscreen`：坐标有效但不在任何屏上 → 不给屏与缩放（避免"退回主屏"造成误判）
    - `normal`：按窗口**中心点**判屏（跨屏窗口取中心）
    """
    out: list[dict] = []
    for w in windows:
        bounds = w.get("bounds") or {}
        item = {
            "app_name": w.get("app_name"),
            "pid": w.get("pid"),
            "window_id": w.get("window_id"),
            "title": w.get("title"),
            "bounds": bounds,
            "minimized": w.get("minimized"),
            "on_screen": w.get("is_on_screen"),
            "z_index": w.get("z_index"),
            "monitor": None,
            "scale": None,
        }
        if w.get("minimized") or is_sentinel_bounds(bounds):
            item["placement"] = "minimized"
            out.append(item)
            continue

        cx = bounds.get("x", 0) + bounds.get("width", 0) / 2
        cy = bounds.get("y", 0) + bounds.get("height", 0) / 2
        hit = next(
            (
                m
                for m in monitors
                if m["bounds"]["x"] <= cx < m["bounds"]["x"] + m["bounds"]["width"]
                and m["bounds"]["y"] <= cy < m["bounds"]["y"] + m["bounds"]["height"]
            ),
            None,
        )
        if hit is None:
            item["placement"] = "offscreen"
        else:
            item["placement"] = "normal"
            item["monitor"] = {"index": hit["index"], "device": hit.get("device"), "scale": hit["scale"]}
            item["scale"] = hit["scale"]
        out.append(item)
    return out


# --------------------------------------------------------------------------
# 原生枚举（ctypes）
# --------------------------------------------------------------------------


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT), wintypes.LPARAM
)
_MONITORINFOF_PRIMARY = 0x1
_MDT_EFFECTIVE_DPI = 0

_user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
try:
    _shcore = ctypes.WinDLL("shcore", use_last_error=True) if sys.platform == "win32" else None
except OSError:  # 老系统没有 shcore
    _shcore = None


def _declare_prototypes(user32, shcore) -> None:
    """一次性声明所有原生原型（R-7）。

    为什么必须声明：ctypes 默认把**没声明**的返回值当 `c_int`——在 64 位上句柄
    （HWND/HMONITOR）会被截断，而这里恰好全是句柄与指针参数。声明也要用**指针宽度**
    的类型（`wintypes.HWND`/`HMONITOR`/`c_void_p`），不能图省事写 `c_int`。

    `desktop.py` 早就这么做了，`display.py` 原先一条都没声明——同一个项目两套口径，
    等于把"能不能正确拿到多屏 DPI"押在运气上。
    """
    if user32 is not None:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.c_void_p, _MONITORENUMPROC, wintypes.LPARAM,
        ]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        # 结构体指针用 c_void_p：真类型不符时 ctypes 会直接抛，反而更难排查
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
    if shcore is not None:
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HMONITOR, ctypes.c_int,
            ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long


if sys.platform == "win32":
    _declare_prototypes(_user32, _shcore)


#: 最近一次声明的 DPI 感知状态（`enum_monitors` 会刷新）
_DPI_AWARENESS: str | None = None


def current_dpi_awareness() -> str | None:
    """本进程实际拿到的 DPI 感知状态（没声明过则为 None）。"""
    return _DPI_AWARENESS


def dpi_for_window(hwnd: int | None) -> int | None:
    """窗口所在显示器的 DPI —— 官方推荐的 per-monitor 口径（R-7）。

    微软文档明确：`GetDpiForMonitor` **不是** DPI-aware 版本，per-monitor DPI
    aware 的线程不应使用它；窗口相关计算应改用 `GetDpiForWindow`。
    """
    if _user32 is None or not hwnd:
        return None
    try:
        value = int(_user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 0)
    except Exception:  # noqa: BLE001
        return None
    return value or None


def _declare_dpi_awareness() -> str:
    """声明 Per-Monitor v2 DPI 感知。不声明就会拿到被虚拟化的逻辑坐标。"""
    global _DPI_AWARENESS
    if _user32 is None:
        _DPI_AWARENESS = "unsupported"
        return _DPI_AWARENESS
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_AWARENESS = "per-monitor-v2"
            return _DPI_AWARENESS
    except AttributeError:
        pass
    if _shcore is not None:
        try:
            # 2 = PROCESS_PER_MONITOR_DPI_AWARE
            if _shcore.SetProcessDpiAwareness(2) == 0:
                _DPI_AWARENESS = "per-monitor"
                return _DPI_AWARENESS
        except Exception:  # noqa: BLE001
            pass
    _DPI_AWARENESS = "unchanged"
    return _DPI_AWARENESS


def _monitor_dpi(hmon) -> int:
    if _shcore is None:
        return BASE_DPI
    x, y = wintypes.UINT(), wintypes.UINT()
    try:
        if _shcore.GetDpiForMonitor(hmon, _MDT_EFFECTIVE_DPI, ctypes.byref(x), ctypes.byref(y)) == 0:
            return int(x.value) or BASE_DPI
    except Exception:  # noqa: BLE001
        pass
    return BASE_DPI


def enum_monitors() -> list[dict]:
    """原生枚举所有显示器（物理像素矩形 + 每屏 DPI）。"""
    if _user32 is None:
        return []
    _declare_dpi_awareness()
    found: list[dict] = []

    def _cb(hmon, _hdc, _lprc, _lparam):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            dpi = _monitor_dpi(hmon)
            found.append(
                {
                    "index": len(found),
                    "device": info.szDevice,
                    "bounds": {
                        "x": info.rcMonitor.left,
                        "y": info.rcMonitor.top,
                        "width": info.rcMonitor.right - info.rcMonitor.left,
                        "height": info.rcMonitor.bottom - info.rcMonitor.top,
                    },
                    "work": {
                        "x": info.rcWork.left,
                        "y": info.rcWork.top,
                        "width": info.rcWork.right - info.rcWork.left,
                        "height": info.rcWork.bottom - info.rcWork.top,
                    },
                    "primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                    "dpi": dpi,
                    "scale": scale_from_dpi(dpi),
                }
            )
        return 1

    _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    return found


def foreground_window() -> dict | None:
    """操作系统层面的真前台窗口（GetForegroundWindow）——**转调 `core.foreground`**。

    实现只有一份（R-2）：这里原先自己声明了一组 user32 原型，与
    `desktop._foreground_window_id` 重复。探针分散会让"谁是前台"的口径漂移，
    而它决定了无显式目标时动作下发给谁，属安全相关判据。
    """
    from . import foreground as fg_mod

    return fg_mod.foreground_window()


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def cursor_position() -> dict[str, int] | None:
    """真实鼠标光标的屏幕坐标（物理像素，原点左上；读不到回 None）。

    与 `enum_monitors().bounds` **同口径**：本进程已声明 DPI 感知，`GetCursorPos`
    回的就是物理像素，可直接与 bounds / frame 比较，不需要换算。

    与驱动的 `move_cursor` 区分：那个动的是 **agent 光标覆盖层**（画给用户看的指示器），
    不是用户的鼠标指针，两个概念不能混用。

    锁屏 / 安全桌面下 `GetCursorPos` 会失败，此时回 None——显式「读不到」，
    不编一个假坐标出来。
    """
    pt = _POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return {"x": int(pt.x), "y": int(pt.y)}


def describe(driver) -> dict[str, Any]:
    """汇总当前显示与窗口归属（给上层 agent 一份可依赖的屏幕事实）。"""
    monitors = enum_monitors()
    resp = driver.call("list_windows", {})
    windows = (resp.get("windows") if isinstance(resp, dict) else resp) or []
    assigned = assign_windows_to_monitors(windows, monitors)
    # 两个口径分开（R-2）：`focus` = 系统前台，`z_order_top` = z_index 最大。
    # 原先只给一个 focus 且装的是 z_index 最大——名字与语义不符，上层会选错目标。
    focus, z_order_top = describe_focus(windows)

    def _placement_of(window: dict | None) -> dict | None:
        if not window:
            return None
        return next((a for a in assigned if a["window_id"] == window.get("window_id")), None)

    def _pick_view(window: dict | None) -> dict | None:
        if not window:
            return None
        placement = _placement_of(window) or {}
        monitor = placement.get("monitor") or None
        return {
            "app_name": window.get("app_name"),
            "pid": window.get("pid"),
            "window_id": window.get("window_id"),
            "title": window.get("title"),
            "monitor_index": monitor.get("index") if monitor else None,
            "scale": placement.get("scale"),
        }

    per_monitor = []
    for m in monitors:
        on_it = [a for a in assigned if a["placement"] == "normal" and a["monitor"]["index"] == m["index"]]
        per_monitor.append(
            {
                "index": m["index"],
                "device": m.get("device"),
                "bounds": m["bounds"],
                "work": m["work"],
                "scale": m["scale"],
                "dpi": m["dpi"],
                "primary": m["primary"],
                "windows": len(on_it),
            }
        )

    return {
        "ok": True,
        "monitor_count": len(monitors),
        # 真实鼠标光标（T-65）：与 monitors[].bounds 同口径的物理像素；读不到为 null
        "cursor": cursor_position(),
        "monitors": per_monitor,
        "windows": [
            {
                "app_name": a["app_name"],
                "pid": a["pid"],
                "window_id": a["window_id"],
                "title": a["title"],
                "placement": a["placement"],
                "monitor_index": a["monitor"]["index"] if a["monitor"] else None,
                "scale": a["scale"],
                "minimized": a["minimized"],
                "z_index": a["z_index"],
            }
            for a in assigned
        ],
        "focus": _pick_view(focus),
        "z_order_top": _pick_view(z_order_top),
        # 坐标口径按**实际** awareness 给（R-7）：没声明成功就不能宣称物理坐标
        "dpi_awareness": current_dpi_awareness(),
        "bounds_are_physical": bounds_are_physical(current_dpi_awareness()),
        "coordinate_note": coordinate_note_for(current_dpi_awareness()),
        "window_order_note": WINDOW_ORDER_NOTE,
    }
