"""winuse_session_state：会话 / 锁屏状态探测（纯 stdlib + ctypes，零第三方依赖）。

**为什么需要这个模块**：一切动作最终都落到用户桌面，而"现在有没有锁屏 / 安全桌面"
是这个前提里最容易漏掉的一条。锁屏或安全桌面（Winlogon 桌面：锁屏界面、登录界面、
Ctrl+Alt+Del、UAC 同意页）接管输入桌面后，输入类与元素读取类动作会失败或读到空树，
而调用方看到的只是"找不到窗口"这种误导性错误。驱动侧的 `get_session` /
`check_permissions` / `health_report` 都从驱动自身视角出发（生命周期、完整性级别、
UIA 可达性），**不直接回答"现在是不是锁屏 / 安全桌面"**，所以判据要落在这里。

四条关键纪律：

1. **不确定就回 None，绝不猜 False**。这是本模块存在的理由：`locked=False` 会被上层
   读成"可以操作"，假阴性比"不知道"危险得多。拿不到输入桌面名、探测失败、桌面名不在
   已知集合里 —— 一律 `None`，并把原因写进 `errors` / `probe`，让调用方能自己判断。
2. **主判据唯一，兜底有顺序**：先读**输入桌面名**（`OpenInputDesktop` +
   `GetUserObjectInformationW(UOI_NAME)`）；只有在它给不出结论时才问 `WTS` 会话标志
   （`WTSQuerySessionInformationW(WTSSessionInfoEx)`）。多探一次就多一次口径冲突的机会，
   所以主判据赢就不多问。`secure_desktop` **只由桌面名判**：WTS 回答不了"是不是安全桌面"，
   于是它在 WTS 路径上恒为 `None`（不许用"没锁"冒充"没有安全桌面"）。
3. **纯逻辑与原生调用分离**：`classify_input_desktop` / `parse_session_info_ex` /
   `flags_to_locked` / `build_availability` 是纯函数（喂名字、喂字节、喂字典就能穷举断言），
   原生调用集中在 `NativeSessionApi` 边界类里，可整体注入替换——单测全程不碰真实桌面，
   更不锁真实屏幕（锁屏是明确的用户副作用）。
4. **只读**：只开句柄、只读名字（拿不到就按 122 补偿后重读一次）、读完立刻 `CloseDesktop`。
   本模块**不调用** `SetThreadDesktop` / `SwitchDesktop` 之类会改变桌面状态的东西。

口径与实测依据（Windows 11 24H2 / build 26200 / 64 位 / 普通权限，本机实测）：

- `OpenInputDesktop(0, FALSE, DESKTOP_READOBJECTS|DESKTOP_SWITCHDESKTOP)` 成功，
  `UOI_NAME` 读回 `Default`（16 字节，含结尾 NUL）→ 正常交互桌面；任意权限掩码
  （含 0）实测都能开，故本模块取"读得到名字"最稳的那组。
- **锁屏时输入桌面是 `Winlogon` 家族的桌面**：`OpenInputDesktop` 很可能**打不开**
  并回 `ERROR_ACCESS_DENIED(5)`——本机实测普通权限进程 `OpenDesktopW("Winlogon")`
  直接 `ERROR_ACCESS_DENIED`（该桌面 ACL 只给 SYSTEM / 管理员），而
  `OpenInputDesktop` 内部就是按名字去开当前输入桌面。所以"打不开 + 错误 5"是锁屏的
  典型症状，**但它也可能是别的权限问题**，因此该路径下 `locked` 交给 WTS 判定，
  而不是硬认成 True；`secure_desktop` 仍为 `None`。
- 名字认不出来（比如 `Disconnected`：RDP 断开时会话桌面）→ 不猜，`None`。
  实测本机窗口站里当前只枚举到 `Default`（`EnumDesktopsW`），`Winlogon` /
  `Disconnected` / `Screen-saver` 都不存在或打不开（错误 5 / 2）。

**未验证项（如实声明，不编）**：本机无法在不锁屏的前提下制造"锁屏"现场（锁屏是用户
副作用，本模块拒绝触发），所以 `locked=True` 的两条分支（名字 = Winlogon、错误 5 + WTS
报锁）**未在真机验证**，只验证了解析与判定逻辑（单测）与未锁屏路径（真机冒烟）。
同理，`WTS_SESSIONSTATE_LOCK/UNLOCK` 在 Windows 7 上被文档声明为**取反**
（本模块不特判老系统，只按文档语义解释 Win8+）。
"""

from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes
from typing import Any, Callable

# --------------------------------------------------------------------------
# Win32 常量（显式字面量，不依赖 ctypes.wintypes 是否导出这些宏）
# --------------------------------------------------------------------------

#: 桌面访问权限：只要读得到对象名与"能切到它"这两项，最小够用
DESKTOP_READOBJECTS = 0x0001
DESKTOP_SWITCHDESKTOP = 0x0100
INPUT_DESKTOP_ACCESS = DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP

#: GetUserObjectInformation 的信息类：UOI_NAME = 2
UOI_NAME = 2

#: 常见 Win32 错误码（只列本模块会区分的那几个，其余按数字原样给出）
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122

WIN32_ERROR_NAMES: dict[int, str] = {
    ERROR_FILE_NOT_FOUND: "ERROR_FILE_NOT_FOUND",
    ERROR_ACCESS_DENIED: "ERROR_ACCESS_DENIED",
    ERROR_INSUFFICIENT_BUFFER: "ERROR_INSUFFICIENT_BUFFER",
}

#: WTS 相关常量：HANDLE 用 None 表示"本机"；WTS_CURRENT_SESSION = -1（DWORD 全 1）
WTS_CURRENT_SERVER_HANDLE = None
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_INFO_CLASS_SESSION_INFO_EX = 25

#: WTSSessionInfoEx 的 SessionFlags（MSDN WTSINFOEX_LEVEL1_W）
WTS_SESSIONSTATE_LOCK = 0x00000000
WTS_SESSIONSTATE_UNLOCK = 0x00000001
WTS_SESSIONSTATE_UNKNOWN = 0xFFFFFFFF

#: 解释 SessionFlags 至少要读到的字节数：Level(4) + 对齐(4) + SessionId(4) +
#: SessionState(4) + SessionFlags(4)。**不够就回 None**——硬按偏移读会读到垃圾。
SESSION_INFO_EX_MIN_BYTES = 20

#: 正常交互桌面的名字（拿不到这个名字就不敢说"没锁"）
INTERACTIVE_DESKTOP_NAME = "Default"

#: 安全/登录桌面的名字族（大小写不敏感按前缀匹配：`Winlogon` 及其同族）
SECURE_DESKTOP_NAMES = frozenset({"Winlogon"})
_SECURE_DESKTOP_PREFIXES = tuple(name.lower() for name in SECURE_DESKTOP_NAMES)

#: 对外能力清单（`build_availability` 的键集合，顺序即输出顺序）
CAPABILITIES: tuple[str, ...] = (
    "read_elements",
    "keyboard_input",
    "mouse_click",
    "drag",
    "screenshot",
    "launch_app",
)

#: 正常交互桌面下的统一原因
INTERACTIVE_AVAILABILITY_REASON = (
    "输入桌面是 Default（无锁屏 / 安全桌面），该能力可用"
)

#: 证据不足时的统一原因（两个判据都是 None）
UNKNOWN_AVAILABILITY_REASON = (
    "会话状态未证明（locked 与 secure_desktop 都是 null）：不按可用上报，避免假阴性"
)

#: 只拿到"没锁"、但输入桌面未知时的原因（可能是 UAC 安全桌面）
PARTIAL_UNKNOWN_AVAILABILITY_REASON = (
    "会话未锁定但输入桌面未知（未能证明没有安全桌面，也可能是权限问题）：不按可用上报"
)

#: 锁屏 / 安全桌面下的逐项原因（一句话说清"为什么不能用了"）
LOCKED_AVAILABILITY: dict[str, bool | None] = {
    "read_elements": False,
    "keyboard_input": False,
    "mouse_click": False,
    "drag": False,
    # 逐窗口捕获（PrintWindow / WGC）在锁屏下可能仍然描出窗口内容 —— 没有实测依据就不下结论
    "screenshot": None,
    "launch_app": False,
}

LOCKED_AVAILABILITY_REASONS: dict[str, str] = {
    "read_elements": (
        "锁屏 / 安全桌面接管输入桌面：窗口按桌面隔离，本进程只能在 Default 桌面上枚举，"
        "桌面级快照会回空树，按元素索引下发的动作也会打空"
    ),
    "keyboard_input": (
        "键盘输入以焦点窗口（或目标窗口消息队列）为前提，安全桌面下不落到用户应用"
    ),
    "mouse_click": (
        "鼠标点击以输入桌面上的可见窗口为锚点，锁屏时锚点不存在（像素点击会报没有在屏窗口）"
    ),
    "drag": "拖拽是鼠标类动作，与鼠标点击同因：输入桌面被安全桌面接管",
    "screenshot": (
        "未验证：逐窗口捕获在锁屏下可能仍可用，而整屏捕获此时拿到的是锁屏画面、不代表用户桌面"
        "（本模块不下结论，需要时请自行实测）"
    ),
    "launch_app": (
        "进程本身可能仍被隐藏启动，但窗口落在解锁前不可交互的桌面上、也读不到元素，"
        "启动后驱动它的主用途不可用"
    ),
}

#: 会话状态字段的口径说明（随输出一起下发，避免上层把"未知"读成"没锁"）
SESSION_NOTE = (
    "locked / secure_desktop 为 null 表示**未证明**（不是\"没锁\"），调用方不得把 null 当成可以操作；"
    "locked 由输入桌面名判定（Default = 未锁，Winlogon 家族 = 锁屏 / 安全桌面），"
    "桌面名读不到或打不开时才退回 WTS 会话标志，此时 secure_desktop 恒为 null（WTS 回答不了这个问题）；"
    "input_desktop 是输入桌面的原始名字；foreground_pid 为 null 只说明没读到前台窗口（锁屏时通常如此），"
    "**不单独作为锁屏判据**。本模块只读：不开线程桌面、不切桌面。"
)

#: 非 Windows 平台的说明（含"平台"字样，便于调用方按关键字分流）
PLATFORM_NOTE = (
    "非 Windows 平台：会话 / 锁屏探测不适用（locked / secure_desktop / input_desktop 全为 null，"
    "不代表已解锁）"
)

#: 探测没拿到确定信号时的追加说明
PROBE_FAILED_SUFFIX = " 本次探测没拿到确定信号（原因见 errors / probe）。"

#: explain_action_availability 的口径说明
AVAILABILITY_NOTE = (
    "available=true 表示该能力当前可用；false 表示会失败（原因是锁屏 / 安全桌面接管了输入桌面）；"
    "null 表示**未证明**（不是\"不可用\"）——证据不足时不下结论，调用方应按\"先别做\"处理。"
    "unavailable / unknown 分别是 false 与 null 的项名清单。"
)


# --------------------------------------------------------------------------
# 纯逻辑：桌面名 → 判定
# --------------------------------------------------------------------------


def normalize_desktop_name(name: Any) -> str | None:
    """桌面名归一：去空白；空/非字符串一律 None（不拿空串当名字）。"""
    if not isinstance(name, str):
        return None
    text = name.strip()
    return text or None


def classify_input_desktop(name: Any) -> dict:
    """纯函数：输入桌面名 → 会话判定。

    判据（**只有这三个分支**，认识之外一律不下结论）：

    - `Default` → 正常交互桌面：`locked=False` / `secure_desktop=False`
    - `Winlogon` 家族 → 锁屏 / 安全桌面：`locked=True` / `secure_desktop=True`
    - 其它（含 None、空串）→ `locked=None` / `secure_desktop=None`，`desktop_class` 如实标记

    `desktop_class` 取值：`interactive` / `secure` / `other`（认得出不是 Default 但不属于
    Winlogon 家族，如 RDP 断开时的 `Disconnected`）/ `unknown`（根本没读到名字）。
    """
    normalized = normalize_desktop_name(name)
    if normalized is None:
        return {
            "desktop_class": "unknown",
            "locked": None,
            "secure_desktop": None,
            "reason": "没有拿到输入桌面名：无法判断锁屏 / 安全桌面（不猜）",
        }
    lowered = normalized.lower()
    if lowered == INTERACTIVE_DESKTOP_NAME.lower():
        return {
            "desktop_class": "interactive",
            "locked": False,
            "secure_desktop": False,
            "reason": f"输入桌面是 {INTERACTIVE_DESKTOP_NAME}（正常交互桌面）",
        }
    if lowered.startswith(_SECURE_DESKTOP_PREFIXES):
        return {
            "desktop_class": "secure",
            "locked": True,
            "secure_desktop": True,
            "reason": (
                f"输入桌面是安全桌面 {normalized}（锁屏 / 登录界面 / Ctrl+Alt+Del / UAC 同意页"
                "都会占它）：输入与读取类动作不可用"
            ),
        }
    return {
        "desktop_class": "other",
        "locked": None,
        "secure_desktop": None,
        "reason": f"输入桌面名 {normalized!r} 不在已知集合里（Default / Winlogon 家族）：不猜判定",
    }


# --------------------------------------------------------------------------
# 纯逻辑：WTSSessionInfoEx 缓冲区 → SessionFlags
# --------------------------------------------------------------------------


def parse_session_info_ex(buffer: bytes | None) -> dict:
    """纯函数：`WTSSessionInfoEx` 原始缓冲区 → `{level, session_flags}`。

    偏移按 MSDN 的 `WTSINFOEXW` 布局（**不是猜的**）：`DWORD Level` 在 0，联合体
    `Data` 因含 `LARGE_INTEGER` 而按 8 字节对齐 → 从 8 开始；只取前三项
    `SessionId`(8) / `SessionState`(12) / `SessionFlags`(16)。

    缓冲区短于 :data:`SESSION_INFO_EX_MIN_BYTES`、`Level != 1`、或根本不是 bytes
    → 一律回 `None`：宁可不判，也不按别的层级布局硬读。

    `SessionFlags` 按**无符号**解出：0xFFFFFFFF（MSDN 的"状态未知"）要原样出现在
    输出里，解成 -1 会让人看不懂。
    """
    if not isinstance(buffer, (bytes, bytearray)) or len(buffer) < SESSION_INFO_EX_MIN_BYTES:
        return {"level": None, "session_flags": None}
    level, _pad, _session_id, _session_state, session_flags = struct.unpack(
        "<IIIiI", bytes(buffer[:SESSION_INFO_EX_MIN_BYTES])
    )
    if level != 1:
        # 只有 Level==1 的布局是我们认识的那套
        return {"level": int(level), "session_flags": None}
    return {"level": int(level), "session_flags": int(session_flags)}


def flags_to_locked(session_flags: Any) -> bool | None:
    """纯函数：`SessionFlags` → `locked`。

    MSDN：`WTS_SESSIONSTATE_LOCK(0)` = 已锁、`WTS_SESSIONSTATE_UNLOCK(1)` = 未锁、
    `WTS_SESSIONSTATE_UNKNOWN(0xFFFFFFFF)` = 状态未知；其余取值未知 → 一律 None。

    注意（文档声明）：Windows 7 / Server 2008 R2 上这两个值**取反**，本模块不特判老系统。
    """
    if isinstance(session_flags, bool) or not isinstance(session_flags, int):
        return None
    if session_flags == WTS_SESSIONSTATE_LOCK:
        return True
    if session_flags == WTS_SESSIONSTATE_UNLOCK:
        return False
    return None


# --------------------------------------------------------------------------
# 纯逻辑：会话状态 → 能力可用性
# --------------------------------------------------------------------------


def _tri_state(state: Any, key: str) -> bool | None:
    """取三态布尔字段：只有真布尔才算数，其它（缺键、字符串、数字）一律 None。"""
    if not isinstance(state, dict):
        return None
    value = state.get(key)
    return value if isinstance(value, bool) else None


def build_availability(state: Any = None) -> dict[str, dict]:
    """纯函数：会话状态 → 逐项 `{available: bool | None, reason: str}`。

    三档判定（**只有"输入桌面是 Default"才敢说可用**）：

    | 状态 | 结论 |
    |---|---|
    | `locked=False` 且 `secure_desktop=False` | 全部 `True` |
    | `locked=True` 或 `secure_desktop=True` | 按 :data:`LOCKED_AVAILABILITY`：输入 / 读取 / 鼠标 / 拖拽 / 启动 = `False`，截图 = `None` |
    | 其余（含状态缺失、字段非法） | 全部 `None` + 原因 |

    为什么"`locked=False` 但输入桌面未知"也按 `None`：那可能是 UAC 安全桌面（会话并未
    锁定，输入同样不落到用户应用），说可用就是假阴性。
    """
    locked = _tri_state(state, "locked")
    secure = _tri_state(state, "secure_desktop")

    if locked is False and secure is False:
        return {
            name: {"available": True, "reason": INTERACTIVE_AVAILABILITY_REASON}
            for name in CAPABILITIES
        }
    if locked is True or secure is True:
        return {
            name: {"available": LOCKED_AVAILABILITY[name], "reason": LOCKED_AVAILABILITY_REASONS[name]}
            for name in CAPABILITIES
        }

    reason = (
        UNKNOWN_AVAILABILITY_REASON
        if locked is None and secure is None
        else PARTIAL_UNKNOWN_AVAILABILITY_REASON
    )
    return {name: {"available": None, "reason": reason} for name in CAPABILITIES}


# --------------------------------------------------------------------------
# 原生调用边界（唯一直接碰 ctypes 的地方，可整体注入替换）
# --------------------------------------------------------------------------

_user32_dll: Any = None
_wtsapi32_dll: Any = None


def _bind_user32(lib: Any) -> None:
    """显式声明 argtypes/restype——句柄是指针宽度，不声明会被默认 c_int 截断。"""
    lib.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    lib.OpenInputDesktop.restype = wintypes.HANDLE
    lib.CloseDesktop.argtypes = [wintypes.HANDLE]
    lib.CloseDesktop.restype = wintypes.BOOL
    lib.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    lib.GetUserObjectInformationW.restype = wintypes.BOOL
    lib.GetForegroundWindow.argtypes = []
    lib.GetForegroundWindow.restype = wintypes.HWND
    lib.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    lib.GetWindowThreadProcessId.restype = wintypes.DWORD


def _bind_wtsapi32(lib: Any) -> None:
    lib.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    lib.WTSQuerySessionInformationW.restype = wintypes.BOOL
    lib.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    lib.WTSFreeMemory.restype = None


def _user32_or_none() -> Any:
    """惰性加载 user32（非 Windows 或加载失败返回 None，不抛）。"""
    global _user32_dll
    if sys.platform != "win32":
        return None
    if _user32_dll is None:
        try:
            lib = ctypes.WinDLL("user32", use_last_error=True)
            _bind_user32(lib)
            _user32_dll = lib
        except Exception:  # noqa: BLE001 - 探测失败不该让调用方崩
            return None
    return _user32_dll


def _wtsapi32_or_none() -> Any:
    """惰性加载 wtsapi32（缺这个 DLL 只影响兜底判据，不影响主判据）。"""
    global _wtsapi32_dll
    if sys.platform != "win32":
        return None
    if _wtsapi32_dll is None:
        try:
            lib = ctypes.WinDLL("wtsapi32", use_last_error=True)
            _bind_wtsapi32(lib)
            _wtsapi32_dll = lib
        except Exception:  # noqa: BLE001
            return None
    return _wtsapi32_dll


class NativeSessionApi:
    """会话探测的原生边界：每个方法一次原生调用，返回**原始**值（不做语义包装）。

    单测注入同形对象即可全程离线（见 `tests/test_session_state.py` 的 FakeSessionApi）。
    这里**只有读**：`OpenInputDesktop` / `GetUserObjectInformationW` / `CloseDesktop` /
    `WTSQuerySessionInformationW` / `GetForegroundWindow` / `GetWindowThreadProcessId`。
    """

    #: 桌面名缓冲的初始字节数（实测 256 足够；不够时按 API 报的所需字节补偿一次）
    NAME_BUFFER_BYTES = 256

    def __init__(self, user32: Any = None, wtsapi32: Any = None) -> None:
        self._u = user32 if user32 is not None else _user32_or_none()
        self._w = wtsapi32 if wtsapi32 is not None else _wtsapi32_or_none()

    # -- 能力与错误 --------------------------------------------------------

    def platform_ok(self) -> bool:
        """user32 可用（即真正的 Windows 且 DLL 加载成功）。"""
        return sys.platform == "win32" and self._u is not None

    def last_error(self) -> int:
        return int(ctypes.get_last_error())

    # -- 输入桌面 ----------------------------------------------------------

    def open_input_desktop(self) -> int:
        """打开"接收用户输入的桌面"；失败返回 0（错误码用 `last_error()` 读）。"""
        ctypes.set_last_error(0)
        return int(self._u.OpenInputDesktop(0, False, INPUT_DESKTOP_ACCESS) or 0)

    def desktop_name(self, handle: int) -> str | None:
        """读桌面对象名（`UOI_NAME`）。读不到返回 None。

        缓冲区不够时（`ERROR_INSUFFICIENT_BUFFER`）按 API 回报的所需字节**再读一次**：
        该 API 在缓冲区过小时会写出所需长度，直接用第一次的结果就是空名。
        """
        size = int(self.NAME_BUFFER_BYTES)
        for _ in range(2):
            buf = ctypes.create_unicode_buffer(size)
            needed = wintypes.DWORD(0)
            ok = self._u.GetUserObjectInformationW(
                wintypes.HANDLE(handle),
                UOI_NAME,
                buf,
                ctypes.sizeof(buf),
                ctypes.byref(needed),
            )
            if ok:
                return normalize_desktop_name(buf.value)
            if int(ctypes.get_last_error()) != ERROR_INSUFFICIENT_BUFFER or not needed.value:
                return None
            size = max(size, int(needed.value))
        return None

    def close_desktop(self, handle: int) -> bool:
        """关句柄（只读探测也必须关，否则每探一次泄一个句柄）。"""
        return bool(self._u.CloseDesktop(wintypes.HANDLE(handle)))

    # -- WTS 兜底判据 ------------------------------------------------------

    def session_info_ex(self) -> bytes | None:
        """读本会话的 `WTSSessionInfoEx` 原始缓冲区；失败返回 None。

        返回值是**拷贝出来的 bytes**，原生缓冲区在这一步就 `WTSFreeMemory` 掉，
        不让调用方接触需要手工释放的内存。
        """
        if self._w is None:
            return None
        buffer = ctypes.c_void_p()
        size = wintypes.DWORD(0)
        ok = self._w.WTSQuerySessionInformationW(
            WTS_CURRENT_SERVER_HANDLE,
            WTS_CURRENT_SESSION,
            WTS_INFO_CLASS_SESSION_INFO_EX,
            ctypes.byref(buffer),
            ctypes.byref(size),
        )
        if not ok or not buffer.value or not size.value:
            return None
        try:
            return ctypes.string_at(buffer, int(size.value))
        finally:
            self._w.WTSFreeMemory(buffer)

    # -- 辅助信号 ----------------------------------------------------------

    def foreground_pid(self) -> int | None:
        """系统前台窗口的进程号；没有前台窗口（锁屏时通常如此）返回 None。"""
        hwnd = int(self._u.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        self._u.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value) or None


# --------------------------------------------------------------------------
# 探测编排
# --------------------------------------------------------------------------


def _new_probe() -> dict:
    return {
        "platform": sys.platform,
        "input_desktop_opened": False,
        "name_read": False,
        "win32_error": None,
        "win32_error_name": None,
        "handle_closed": None,
        "wts_session_flags": None,
        "wts_error": None,
    }


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"section": "session_state", "code": code, "message": message, **extra}


def _call(func: Callable[[], Any], errors: list[dict], code: str, message: str) -> Any:
    """跑一次原生调用：异常归一成错误条目并回 None（探测失败不该让调用方崩）。"""
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - 归一化成结构，不让异常穿层
        errors.append(_err(code, message, detail=str(exc)[:200]))
        return None


def _error_name(code: int | None) -> str | None:
    if code is None:
        return None
    return WIN32_ERROR_NAMES.get(int(code), f"win32_error={int(code)}")


def _record_win32_error(probe: dict, code: int | None) -> None:
    probe["win32_error"] = code
    probe["win32_error_name"] = _error_name(code)


def _read_input_desktop_name(api: Any, probe: dict, errors: list[dict]) -> str | None:
    """打开输入桌面并读它的名字；无论成败都关句柄。返回名字或 None。"""
    handle = 0
    try:
        handle = int(
            _call(
                api.open_input_desktop,
                errors,
                "INPUT_DESKTOP_PROBE_FAILED",
                "OpenInputDesktop 调用抛异常",
            )
            or 0
        )
        if not handle:
            code = _call(api.last_error, errors, "LAST_ERROR_UNAVAILABLE", "GetLastError 读不到")
            _record_win32_error(probe, int(code) if isinstance(code, int) else None)
            # 真机实测：普通权限进程开 Winlogon 桌面直接 ACCESS_DENIED，
            # 所以"打不开"既可能是锁屏也可能是别的权限问题 —— 不当成锁屏证据，只如实记录
            errors.append(
                _err(
                    "INPUT_DESKTOP_UNAVAILABLE",
                    "OpenInputDesktop 未能打开输入桌面（拿不到输入桌面名）",
                    win32_error=probe["win32_error"],
                    win32_error_name=probe["win32_error_name"],
                    hint=(
                        "ERROR_ACCESS_DENIED 常见于锁屏 / 安全桌面（输入桌面是 Winlogon 家族），"
                        "也可能是权限不足；本模块不据此认定 locked，需要时看 wts_session_flags"
                    ),
                )
            )
            return None

        probe["input_desktop_opened"] = True
        name = normalize_desktop_name(
            _call(
                lambda: api.desktop_name(handle),
                errors,
                "INPUT_DESKTOP_NAME_FAILED",
                "GetUserObjectInformationW(UOI_NAME) 调用抛异常",
            )
        )
        probe["name_read"] = name is not None
        if name is None:
            code = _call(api.last_error, errors, "LAST_ERROR_UNAVAILABLE", "GetLastError 读不到")
            _record_win32_error(probe, int(code) if isinstance(code, int) else None)
            errors.append(
                _err(
                    "INPUT_DESKTOP_NAME_UNREADABLE",
                    "输入桌面句柄已打开但读不到名字（UOI_NAME）",
                    win32_error=probe["win32_error"],
                    win32_error_name=probe["win32_error_name"],
                )
            )
        return name
    finally:
        if handle:
            closed = _call(
                lambda: api.close_desktop(handle),
                errors,
                "INPUT_DESKTOP_CLOSE_FAILED",
                "CloseDesktop 调用抛异常（句柄可能未释放）",
            )
            probe["handle_closed"] = bool(closed)


def _read_wts_flags(api: Any, probe: dict, errors: list[dict]) -> int | None:
    """兜底判据：问 WTS 本会话的锁屏标志。拿不到返回 None。"""
    raw = _call(
        api.session_info_ex,
        errors,
        "WTS_PROBE_FAILED",
        "WTSQuerySessionInformationW(WTSSessionInfoEx) 调用抛异常",
    )
    parsed = parse_session_info_ex(raw)
    probe["wts_session_flags"] = parsed["session_flags"]
    if parsed["session_flags"] is None:
        probe["wts_error"] = (
            "没有可用的 WTSSessionInfoEx 缓冲区" if parsed["level"] is None else f"Level={parsed['level']}"
        )
        errors.append(
            _err(
                "WTS_PROBE_UNAVAILABLE",
                "WTS 会话信息不可用（拿不到 SessionFlags）",
                level=parsed["level"],
            )
        )
        return None
    if flags_to_locked(parsed["session_flags"]) is None:
        errors.append(
            _err(
                "WTS_SESSION_FLAGS_UNKNOWN",
                f"SessionFlags={parsed['session_flags']} 不在已知取值里（0=锁、1=未锁）",
            )
        )
        probe["wts_error"] = f"SessionFlags={parsed['session_flags']} 未知"
    return parsed["session_flags"]


def _read_foreground_pid(api: Any, errors: list[dict]) -> int | None:
    pid = _call(
        api.foreground_pid,
        errors,
        "FOREGROUND_PROBE_FAILED",
        "前台窗口探测抛异常",
    )
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def detect_session_state(api: Any = None) -> dict:
    """探测当前会话状态：是否锁屏 / 安全桌面。**只读，绝不切桌面或锁屏。**

    `api` 可注入（单测用假原生边界，全程不碰真实桌面）。

    返回结构::

        {"ok": bool,                  # 探测是否拿到确定信号（见下）
         "locked": bool | None,       # True/False = 已判定；None = 未证明（**不是"没锁"**）
         "input_desktop": str | None, # 输入桌面原始名字（Default / Winlogon / …）
         "secure_desktop": bool | None,
         "foreground_pid": int | None,
         "note": str,                 # 字段口径说明
         "desktop_class": "interactive" | "secure" | "other" | "unknown",
         "locked_basis": "input_desktop_name" | "wts_session_flags" | None,
         "probe": {...},              # 原始信号（含 win32 错误码与 WTS 标志）
         "errors": [...]}             # 探测过程中的失败/异常，逐条结构化

    `ok=False` 的含义是"这一轮没拿到确定信号"（平台不支持、句柄打不开且 WTS 也答不上、
    原生调用抛异常）——此时 `locked` 必为 `None`，调用方**不得**当成可以操作。

    判定顺序（主判据唯一，兜底有顺序）：输入桌面名 → 认不出来才问 WTS；
    `secure_desktop` 只由桌面名判，走 WTS 路径时恒为 `None`。
    """
    api = api if api is not None else NativeSessionApi()
    errors: list[dict] = []
    probe = _new_probe()

    platform_ok = _call(
        api.platform_ok, errors, "PLATFORM_PROBE_FAILED", "平台探测抛异常"
    )
    if not platform_ok:
        return {
            "ok": False,
            "locked": None,
            "input_desktop": None,
            "secure_desktop": None,
            "foreground_pid": None,
            "note": PLATFORM_NOTE,
            "desktop_class": "unknown",
            "locked_basis": None,
            "probe": probe,
            "errors": errors,
        }

    name = _read_input_desktop_name(api, probe, errors)
    verdict = classify_input_desktop(name)
    locked: bool | None = verdict["locked"]
    secure: bool | None = verdict["secure_desktop"]
    basis: str | None = "input_desktop_name" if locked is not None else None

    if locked is None:
        # 只有在主判据给不出结论时才问 WTS（多探一次就多一次口径冲突的机会）
        flags = _read_wts_flags(api, probe, errors)
        wts_locked = flags_to_locked(flags)
        if wts_locked is not None:
            locked = wts_locked
            basis = "wts_session_flags"

    foreground_pid = _read_foreground_pid(api, errors)
    ok = locked is not None or name is not None

    note = SESSION_NOTE if ok else SESSION_NOTE + PROBE_FAILED_SUFFIX
    return {
        "ok": ok,
        "locked": locked,
        "input_desktop": name,
        "secure_desktop": secure,
        "foreground_pid": foreground_pid,
        "note": note,
        "desktop_class": verdict["desktop_class"],
        "locked_basis": basis,
        "probe": probe,
        "errors": errors,
    }


def explain_action_availability(state: Any = None, *, api: Any = None) -> dict:
    """把会话状态翻译成"哪些能力现在会失败"。

    `state` 不传（默认 `None`）就先自己调 :func:`detect_session_state`（`api` 可注入，
    便于单测）；传了就**直接用它**，不再碰任何原生接口（显式给了状态就不该偷偷再探测一次）。

    返回结构::

        {"ok": bool,                  # 翻译是否跑通（等价于 state.ok）
         "locked": bool | None,
         "secure_desktop": bool | None,
         "input_desktop": str | None,
         "foreground_pid": int | None,
         "drivable": bool | None,     # True 仅当已证明输入桌面是 Default
         "capabilities": {name: {"available": bool | None, "reason": str}},
         "unavailable": [name...],    # available 为 False 的项
         "unknown": [name...],        # available 为 None 的项
         "note": str,                 # available 三态的口径说明
         "session_note": str}         # 会话状态自己的口径说明（含"未证明"的含义）

    `drivable` 的判据与逐项一致：`locked` 与 `secure_desktop` 都是 `False` 才为 `True`；
    任一为 `True` 则为 `False`；其余为 `None`。
    """
    if state is None:
        current: Any = detect_session_state(api) if api is not None else detect_session_state()
    else:
        current = state if isinstance(state, dict) else {}

    locked = _tri_state(current, "locked")
    secure = _tri_state(current, "secure_desktop")
    if locked is False and secure is False:
        drivable: bool | None = True
    elif locked is True or secure is True:
        drivable = False
    else:
        drivable = None

    capabilities = build_availability(current)
    return {
        "ok": bool(current.get("ok")),
        "locked": locked,
        "secure_desktop": secure,
        "input_desktop": current.get("input_desktop"),
        "foreground_pid": current.get("foreground_pid"),
        "drivable": drivable,
        "capabilities": capabilities,
        "unavailable": [n for n in CAPABILITIES if capabilities[n]["available"] is False],
        "unknown": [n for n in CAPABILITIES if capabilities[n]["available"] is None],
        "note": AVAILABILITY_NOTE,
        "session_note": current.get("note"),
    }
