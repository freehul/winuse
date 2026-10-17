"""安全层：危险动作判定（纯函数，绝不碰系统）。

判定口径（用户 2026-09-26 批准的危险清单）：
1. `kill_app` 之类"杀掉应用"的动作
2. 系统电源类（shutdown / restart / sleep / lock / logoff）
3. 注册表写（读为安全；但 SAM / SECURITY 等敏感 hive 连读都算危险）
4. 点到**系统级窗口**（任务栏、资源管理器、系统设置、开始菜单、锁屏界面……）
5. 批量输入（多步动作批处理）

职责边界：本模块只**判定**与**要求确认**；真正的执行/拒绝在调用侧
（`core.act.run_act` 与各门面）。判定为纯函数 → 可穷举单测，不依赖真机。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..driver.errors import ConfirmRequired

#: 危险动作名（跨层统一命名，避免各门面各写一份）
#: 注：`close` 是窗口动作（关掉用户的窗口），与 `kill_app` 同级算危险。
DANGEROUS_ACTIONS = frozenset(
    {
        "kill_app",
        "kill",
        "close_app",
        "close",
        "shutdown",
        "restart",
        "reboot",
        "sleep",
        "hibernate",
        "lock",
        "logoff",
    }
)

#: 系统电源类动作
POWER_ACTIONS = frozenset({"shutdown", "restart", "sleep", "lock", "logoff"})

#: 系统级窗口的应用名（点了这些等于操作系统本体，不是"某个应用"）
SYSTEM_APPS = frozenset(
    {
        "explorer.exe",
        "systemsettings.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "searchhost.exe",
        "lockapp.exe",
        "winlogon.exe",
        "logonui.exe",
        "consent.exe",
        "sihost.exe",
        "taskmgr.exe",
    }
)

#: 系统级窗口的标题特征（兜底：某些系统窗口的进程名会变）
SYSTEM_TITLE_PATTERNS = (
    "任务栏",
    "开始菜单",
    "系统设置",
    "windows 安全",
    "taskbar",
    "start menu",
    "settings",
)

#: 敏感注册表路径前缀（连读都要确认）
SENSITIVE_REGISTRY_PREFIXES = ("sam", "security", "system\\currentcontrolset", "bcd00000000")

#: 危险动作的统一错误码（与 errors.ConfirmRequired 一致）
CONFIRM_CODE = "CONFIRM_REQUIRED"
BATCH_RULE = "batch_input"
SYSTEM_WINDOW_RULE = "system_window"
KILL_RULE = "kill_app"
POWER_RULE = "power_action"
REGISTRY_WRITE_RULE = "registry_write"
REGISTRY_SENSITIVE_RULE = "registry_sensitive"


@dataclass(frozen=True)
class RiskVerdict:
    """判定结果。dangerous=True 时必须有 code 与 rule（便于审计与上层分支）。"""

    dangerous: bool
    code: str | None = None
    reason: str = ""
    rule: str | None = None

    @classmethod
    def safe(cls, reason: str = "未命中危险规则") -> "RiskVerdict":
        return cls(dangerous=False, code=None, reason=reason, rule=None)

    @classmethod
    def danger(cls, reason: str, rule: str) -> "RiskVerdict":
        return cls(dangerous=True, code=CONFIRM_CODE, reason=reason, rule=rule)

    def to_dict(self) -> dict:
        return {"dangerous": self.dangerous, "code": self.code, "reason": self.reason, "rule": self.rule}


def _labels(target: dict | None) -> tuple[str, str]:
    if not target:
        return "", ""
    app = str(target.get("app_name") or target.get("app") or "").strip().lower()
    title = str(target.get("title") or "").strip().lower()
    return app, title


def is_system_window(target: dict | None) -> bool:
    """系统级窗口：点到它可能触发系统级行为（关机菜单、托盘、设置页……）。"""
    app, title = _labels(target)
    if app in SYSTEM_APPS:
        return True
    return any(pat in title for pat in SYSTEM_TITLE_PATTERNS)


def assess_action(action: str, target: dict | None = None, element_index: int | None = None) -> RiskVerdict:
    """判定单个界面动作。选不到窗口（target 为空）不在本层报错——那是选取层的职责。"""
    name = str(action or "").strip().lower()
    # 单一出处：动作名一律查 DANGEROUS_ACTIONS（曾在这里又写一份硬编码集合，
    # 结果清单里加了 close 却不生效——加名字必须靠这条路径真的被判危险）。
    if name in DANGEROUS_ACTIONS:
        if name in POWER_ACTIONS:
            return RiskVerdict.danger(f"动作 {name} 会改变系统电源状态", POWER_RULE)
        return RiskVerdict.danger(f"动作 {name} 会关闭窗口或结束进程", KILL_RULE)
    if is_system_window(target):
        app, title = _labels(target)
        return RiskVerdict.danger(
            f"作用是系统级窗口（应用 {app or '未知'}／标题「{title or '未知'}」），可能触发系统级行为",
            SYSTEM_WINDOW_RULE,
        )
    return RiskVerdict.safe()


def assess_batch(steps: list | tuple | None) -> RiskVerdict:
    """批量输入：一次下发多步动作，影响面与可回滚性都变差，统一要确认。"""
    count = len(list(steps or []))
    if count == 1:
        return RiskVerdict.danger("批量输入（1 步也算批处理通道），需确认后执行", BATCH_RULE)
    if count > 1:
        return RiskVerdict.danger(f"批量输入共 {count} 步，需确认后执行", BATCH_RULE)
    return RiskVerdict.safe("批量为空，无需确认")


CLIPBOARD_READ_TEXT_RULE = "clipboard_read_text"
CLIPBOARD_WRITE_RULE = "clipboard_write"


def assess_clipboard_read(include_text: bool) -> RiskVerdict:
    """读剪贴板**正文**属隐私读取；只列类型不危险（拿到的是格式名，不含内容）。"""
    if include_text:
        return RiskVerdict.danger(
            "读取剪贴板纯文本：剪贴板里可能是刚复制的口令 / 验证码 / 密钥，属隐私读取",
            CLIPBOARD_READ_TEXT_RULE,
        )
    return RiskVerdict.safe("只列剪贴板类型（格式名，不含内容），不构成隐私读取")


def assess_clipboard_write() -> RiskVerdict:
    """写剪贴板会整段替换用户当前内容，且 Windows 没有公开的「恢复上一个剪贴板」接口。"""
    return RiskVerdict.danger(
        "写剪贴板会整段替换用户当前剪贴板内容（可能覆盖他还没粘贴的东西），且不可撤销",
        CLIPBOARD_WRITE_RULE,
    )


NOTIFY_BROADCAST_RULE = "notify_broadcast"


def assess_notify_broadcast(broadcast: bool) -> RiskVerdict:
    """通知广播的判定（R-19）：`msg.exe *` 把正文送到本机**全部**会话。

    为什么算危险：通知正文会出现在 RDP 会话、快速用户切换后的另一个用户桌面上
    ——"给当前用户看的提醒"可能变成"给所有人看的信息"。默认只发当前会话，
    要广播必须显式确认。
    """
    if broadcast:
        return RiskVerdict.danger(
            "通知会广播给本机全部会话（RDP / 快速用户切换的其它桌面也会看到正文）",
            NOTIFY_BROADCAST_RULE,
        )
    return RiskVerdict.safe("仅发送给当前会话")


def assess_power(action: str) -> RiskVerdict:
    """电源类意图（真正执行由上层，本层只判定）。"""
    name = str(action or "").strip().lower()
    if name in POWER_ACTIONS:
        return RiskVerdict.danger(f"电源动作 {name} 会中断当前会话", POWER_RULE)
    return RiskVerdict.safe(f"{name or '空'} 不在电源动作清单内")


def _normalize_registry_path(hive: str, path: str) -> str:
    return f"{str(hive or '').strip().upper()}\\{str(path or '').strip()}".lower().lstrip("\\")


def _path_matches_prefix(key: str, prefix: str) -> bool:
    """`prefix` 是否作为**完整路径组件序列**出现在 `key` 里（R-11）。

    为什么不能裸子串（`prefix in key`）：`sam` 会命中 `...\\sample`、
    `system\\currentcontrolset` 会命中 `hkcu\\system\\currentcontrolset`——
    后者根本不是敏感区（HKCU 下那棵树是用户自己的）。误报会让正常读被拦，
    久了维护者就会把整条判定注释掉——判据必须精确，否则等于没有。
    """
    parts = [p for p in key.split("\\") if p]
    needle = [p for p in prefix.split("\\") if p]
    if not needle or len(needle) > len(parts):
        return False
    span = len(needle)
    return any(parts[i : i + span] == needle for i in range(len(parts) - span + 1))


def assess_registry_read(hive: str, path: str) -> RiskVerdict:
    """敏感注册表**读**的判定（读本身不改系统，但敏感区连读都要授权）。

    只在 HKLM 下生效——与结构性硬拒的 `registry.is_denied_path` 同口径：
    `HKLM\\SAM` 与 `HKLM\\SECURITY` 才是口令哈希/LSA 机密的所在地；
    HKCU / HKU 下的同名路径是用户自己的东西，不该跟着被拦。
    """
    hive_name = str(hive or "").strip().upper()
    if hive_name not in ("HKLM", "HKEY_LOCAL_MACHINE"):
        return RiskVerdict.safe()
    key = _normalize_registry_path(hive, path)
    if any(_path_matches_prefix(key, prefix) for prefix in SENSITIVE_REGISTRY_PREFIXES):
        return RiskVerdict.danger(
            f"注册表敏感区域（{hive}\\{path}），仅授权场景可读", REGISTRY_SENSITIVE_RULE
        )
    return RiskVerdict.safe()


def assess_registry_write(hive: str, path: str, value_name: str | None = None) -> RiskVerdict:
    return RiskVerdict.danger(
        f"注册表写操作（{hive}\\{path}"
        + (f" → {value_name}" if value_name else "")
        + "）会持久改变系统或应用行为",
        REGISTRY_WRITE_RULE,
    )


def require_confirm(verdict: RiskVerdict, confirmed: bool) -> bool:
    """执行前的最后一道闸：危险且未确认 → 抛 ConfirmRequired；返回"是否为危险动作"。"""
    if verdict.dangerous and not confirmed:
        raise ConfirmRequired(verdict.reason, f"确认方式：CLI 加 --confirm / MCP 传 confirm=true（规则：{verdict.rule}）")
    return verdict.dangerous
