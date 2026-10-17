"""系统前台窗口探测（**唯一出处**）。

为什么必须只有一个出处（R-2）：项目里原先有两份等价实现——
`display.foreground_window()` 与 `desktop._foreground_window_id()`——各自声明
`GetForegroundWindow` 的 argtypes/restype，将来改一处漏一处就是坐标/句柄口径漂移。
更关键的是：**谁是前台**决定了"无显式目标时动作下发给谁"，这是安全相关判定
（驱动自带的 `Cua.AgentCursorOverlay` 悬浮层 z_index 最大，会骗走"取最前"的判定），
判据不能有第二份。

为什么不是 `display`：`display` 已经 import `state`，而 `state` 需要前台提示
（`pick_window` 的默认目标）→ `state` 反向 import `display` 会成环。本模块不 import
任何 core 内其它模块，谁都能安全引用。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

#: 无前台窗口时的返回值（0 是 Win32 约定的"没有"）
NO_FOREGROUND = 0

_user32 = None


def _user32_or_none():
    """惰性取 user32（非 Windows 或加载失败一律返回 None，不抛）。"""
    global _user32
    if sys.platform != "win32":
        return None
    if _user32 is None:
        try:
            lib = ctypes.WinDLL("user32", use_last_error=True)
            # 句柄是指针宽度：不声明 restype 会被默认 c_int 截断（64 位上出错）
            lib.GetForegroundWindow.restype = wintypes.HWND
            lib.GetForegroundWindow.argtypes = []
            lib.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            lib.GetWindowThreadProcessId.restype = wintypes.DWORD
            _user32 = lib
        except Exception:  # noqa: BLE001 —— 探测失败不该让调用方崩
            return None
    return _user32


def foreground_window_id() -> int:
    """系统前台窗口句柄；没有前台窗口或探测失败返回 `NO_FOREGROUND`（0）。"""
    lib = _user32_or_none()
    if lib is None:
        return NO_FOREGROUND
    try:
        return int(lib.GetForegroundWindow() or NO_FOREGROUND)
    except Exception:  # noqa: BLE001
        return NO_FOREGROUND


def foreground_window() -> dict | None:
    """系统前台窗口 `{"window_id", "pid"}`；探测不到返回 None。

    只回这两个字段：本模块不碰窗口标题/应用名（那要再问驱动，属别的层的事）。
    """
    hwnd = foreground_window_id()
    if not hwnd:
        return None
    lib = _user32_or_none()
    pid = wintypes.DWORD()
    try:
        lib.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    except Exception:  # noqa: BLE001
        pid = None
    return {"window_id": hwnd, "pid": int(pid.value) if pid is not None else None}
