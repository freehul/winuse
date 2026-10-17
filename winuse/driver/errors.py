"""WinUse 异常体系。

原则：错误必须带**稳定的机器可读 code** 与**人能看懂的话**——
上层 agent 靠 code 分支，人靠 message 排障，别只抛一句英文原文。
"""

from __future__ import annotations


class WinUseError(Exception):
    """所有 WinUse 错误的基类。"""

    code = "WINUSE_ERROR"

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        """对外结构。**出口统一脱敏**（R-20）。

        为什么放在这里而不是各门面各脱一次：真机踩到过一个具体例子——某个注册表键名
        本身含有本机用户目录（`AppUserModelId` 下就存着带路径的名字），错误消息原样
        带出去就泄露了用户名与目录结构。每个出口各写一遍脱敏必然漏；放在唯一的
        错误基类上，所有门面（CLI / MCP / 插件桥接）自动受益，将来加门面也不会漏。

        原始 `message` 属性保持不变，排查时仍能拿到完整信息。
        """
        from ..audit import redact_text

        out = {"code": self.code, "message": redact_text(self.message, limit=300)}
        if self.detail:
            out["detail"] = redact_text(self.detail, limit=400)
        return out

    def __str__(self) -> str:  # pragma: no cover - 直观展示
        return f"[{self.code}] {self.message}" + (f" :: {self.detail}" if self.detail else "")


class ConfigError(WinUseError):
    """配置文件缺失、字段类型不对、取值非法。"""

    code = "CONFIG_ERROR"


class ConfirmRequired(WinUseError):
    """危险动作缺少显式确认——拒绝执行（安全层返回，不是程序错误）。"""

    code = "CONFIRM_REQUIRED"


class DriverNotFound(WinUseError):
    """找不到 cua-driver 可执行文件。"""

    code = "DRIVER_NOT_FOUND"


class DriverVersionTooOld(WinUseError):
    """驱动版本低于 WinUse 要求的最低版本。"""

    code = "DRIVER_VERSION_TOO_OLD"


class DriverTimeout(WinUseError):
    """驱动调用超时。"""

    code = "DRIVER_TIMEOUT"


class DriverLaunchFailed(WinUseError):
    """驱动进程起不来（权限不足、路径过长、管道破裂等 OSError）。

    R-10：这类故障原先**没有**被归一——`_run()` 只捕获 `TimeoutExpired` 与
    `FileNotFoundError`，其余 OSError 原样上抛，CLI 直接 traceback。
    """

    code = "DRIVER_LAUNCH_FAILED"


class DriverError(WinUseError):
    """驱动调用失败：退出码非 0、输出不可解析、内容不是 JSON。"""

    code = "DRIVER_ERROR"


class WindowNotFound(WinUseError):
    """按 window_id / pid / app 名都找不到目标窗口。"""

    code = "WINDOW_NOT_FOUND"


class WindowAmbiguous(WinUseError):
    """z_index 不可用且可见窗口不唯一——绝不瞎猜，要求调用方显式指定。"""

    code = "WINDOW_AMBIGUOUS"


class WindowMinimized(WinUseError):
    """目标窗口处于最小化状态：像素坐标是不可信的哨兵值，需显式确认。"""

    code = "WINDOW_MINIMIZED"


class SnapshotRequired(WinUseError):
    """按元素索引下动作却缺快照标识——驱动不变量要求索引与快照同源。"""

    code = "SNAPSHOT_REQUIRED"


class MissingTarget(WinUseError):
    """动作没有指定作用对象（既没给元素索引，也没给元素令牌）。"""

    code = "MISSING_TARGET"


class UnknownAction(WinUseError):
    """动作名不在支持清单里。"""

    code = "UNKNOWN_ACTION"


class MissingParam(WinUseError):
    """动作缺少必需参数（如 set_value 缺 value）。"""

    code = "MISSING_PARAM"


class ParamOutOfRange(WinUseError):
    """参数值超出允许范围（R-21）。

    与 `InvalidParams`（类型不对）分开：这类值类型是对的，但**量级**不允许——
    超长超时、超大递归深度、超多批量步数。调用方该做的是改小，不是改类型。
    """

    code = "PARAM_OUT_OF_RANGE"


class InvalidParams(WinUseError):
    """参数值类型不对（布尔字段收到字符串/数字等）。

    为什么单列一个：这类错误是**调用方**的错（序列化写错、模型输出字符串），
    必须和"驱动失败了"区分开——门面把它翻成 JSON-RPC -32602，
    让客户端知道重试没用、该改参数。
    """

    code = "INVALID_PARAMS"
