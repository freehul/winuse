"""winuse_notify：向用户发一条 Windows 通知（零第三方依赖）。

为什么要自己发通知：桌面 agent 做长任务时，"做完了/卡住了"得让用户知道，
而不是让 agent 自己截屏看有没有弹窗。通知是**单向、低打扰**的输出通道。

两条通道（按优先级）：
1. **winrt-toast**：PowerShell 5.1 内建 WinRT 投影 → `Windows.UI.Notifications`，
   现代通知中心气泡。**禁用 BurntToast 等第三方模块**（本项目零第三方依赖，
   且它只是同一套 API 的包装）。
   AUMID 用 PowerShell 自己的——未注册 AUMID 的 toast 会被系统**静默丢弃**，
   而 powershell 的 AUMID 在装机时即已注册，无需另建开始菜单快捷方式。
2. **msg**：Windows 自带 `msg.exe`，老系统的兜底。没有通知中心那么好看，
   但把"能送达"看得比"好看"重要。

三条关键做法：
1. **默认干跑**：`execute=False` 只回"将要执行的命令"，`command_preview` 做**脱敏**
   （本机绝对路径 → `<PATH>`，内嵌 base64 → `<BASE64:N 字符>`，超长截断），
   免得把用户路径/通知正文整段倒进日志或上层上下文。真发必须显式 `execute=True`。
2. **`-EncodedCommand` 杜绝转义地狱**：标题正文先做 XML 转义、再整体 base64，
   由脚本内解码——标题里带 `<`、`&`、`"`、`$` 都不会破 XML 也不会被 PowerShell 解释。
3. **真实通道要如实回填**：`channel` 字段报的是**实际用了哪条**（首选失败会兜底，
   兜底成功时 channel 必须是 msg，不能还报 winrt-toast）。
"""

from __future__ import annotations

import base64
import re
import shutil
import getpass
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from ..driver.errors import WinUseError

#: 通知级别（info 常规 / warn 警告 / error 错误）
LEVELS = ("info", "warn", "error")

#: 级别别名 → 规范值（R-19）：门面历史上对外声明的是 `warning`，
#: 上游按 schema 传它就稳定吃 NOTIFY_INVALID_LEVEL。别名归一是**兼容**，不是放水：
#: 认不出的一律照旧拒。
LEVEL_ALIASES = {"warning": "warn"}


def normalize_level(level: Any) -> str:
    """级别归一到规范值；认不出一律抛 `NotifyInvalidLevel`（纯函数）。"""
    text = str(level or "").strip().lower()
    text = LEVEL_ALIASES.get(text, text)
    if text not in LEVELS:
        raise NotifyInvalidLevel(
            f"不支持的通知级别：{level}",
            "可用：" + "、".join(LEVELS) + "（warning 是 warn 的别名）",
        )
    return text

#: 级别 → toast scenario（reminder/urgent 会在通知中心更显眼，不影响内容）
LEVEL_SCENARIOS = {"info": None, "warn": "reminder", "error": "urgent"}

#: 级别 → msg.exe 文本前缀（msg 没有现代化样式，只能靠文字标级别）
LEVEL_LABELS = {"info": "", "warn": "[警告] ", "error": "[错误] "}

#: 通道优先级：首选 winrt-toast，兜底 msg
CHANNEL_ORDER = ("winrt-toast", "msg")

#: 通道 → 可执行文件（都不写绝对路径：数据卫生 + 让 PATH 决定）
POWERSHELL_EXE = "powershell.exe"
MSG_EXE = "msg.exe"

#: `msg.exe` 的"本机全部会话"目标（**只有显式广播才用**）
ALL_SESSIONS = "*"

#: PowerShell 自带的 AUMID（装机即注册，避免 toast 被系统静默丢弃）
POWERSHELL_AUMID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

#: 发通知的上限时间（秒）——通知是顺手做的事，不该拖住主流程
NOTIFY_TIMEOUT_SECONDS = 20

#: 命令预览的最大长度（脱敏后仍要能一眼看懂）
PREVIEW_MAX_CHARS = 600

#: 预览截断时留给尾部的比例——关键动作（Show/Write-Output）在脚本末尾
PREVIEW_TAIL_RATIO = 0.4

#: `runner(argv) -> (returncode, stdout, stderr)`，可注入
Runner = Callable[[list[str]], "tuple[int, str, str]"]
Which = Callable[[str], "str | None"]


class NotifyInvalidLevel(WinUseError):
    """level 不在 info|warn|error 里。"""

    code = "NOTIFY_INVALID_LEVEL"


class NotifyMissingContent(WinUseError):
    """标题和正文都为空——发空气没有意义。"""

    code = "NOTIFY_MISSING_CONTENT"


class NotifyInvalidTimeout(WinUseError):
    """timeout_seconds 不是正整数。"""

    code = "NOTIFY_INVALID_TIMEOUT"


class NotifyNoChannel(WinUseError):
    """两条通道都不可用（没有 powershell.exe，也没有 msg.exe）。"""

    code = "NOTIFY_NO_CHANNEL"


class NotifySendFailed(WinUseError):
    """首选与兜底通道都发送失败。"""

    code = "NOTIFY_SEND_FAILED"


@dataclass
class NotifySpec:
    """一条通知的完整描述（门面把它翻译成这个，业务逻辑只在这里）。"""

    title: str = ""
    body: str = ""
    #: info | warn | error
    level: str = "info"
    #: 过期秒数：toast 设 ExpirationTime；msg 用 /TIME
    timeout_seconds: int = 10
    #: 默认干跑；只有 True 才真发
    execute: bool = False
    #: 广播给本机**全部**会话（默认 False：只发当前用户/当前会话）。R-19：
    #: `msg.exe *` 会把正文送到 RDP、快速用户切换后的其他会话屏幕上，
    #: 因此广播是危险动作，需显式确认（核心另有确认门）。
    broadcast: bool = False


# --------------------------------------------------------------------------
# 纯函数：文本处理与脱敏
# --------------------------------------------------------------------------


def xml_escape(text: str) -> str:
    """纯函数：XML 文本节点转义。`&` 必须最先换（否则会二次转义 `&lt;`）。"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def msg_escape(text: str) -> str:
    """纯函数：msg.exe 参数转义——双引号要转义，换行在命令行参数里不可靠，折成空格。"""
    return str(text).replace('"', '\\"').replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _b64_utf8(text: str) -> str:
    """纯函数：UTF-8 → base64（把外部字符串安全送进脚本的唯一入口）。"""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def encode_powershell(script: str) -> str:
    """纯函数：脚本 → `-EncodedCommand` 参数值（UTF-16LE base64）。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def decode_powershell(encoded: str) -> str | None:
    """纯函数：`-EncodedCommand` 还原成脚本（用于预览/排障），失败给 None。"""
    try:
        return base64.b64decode(encoded).decode("utf-16-le")
    except (ValueError, UnicodeDecodeError):
        return None


_ABS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"';]*")
_LONG_B64 = re.compile(r"[A-Za-z0-9+/=]{40,}")


def redact_text(text: str) -> str:
    """纯函数：命令文本脱敏（数据卫生铁律——预览里不许出现本机绝对路径/大块编码内容）。

    超长时**头尾各留一段**：命令的关键动作（展示通知、写输出）在脚本末尾，
    只留头会让预览看起来"什么也没做"。
    """
    flat = " ".join(str(text).split())
    flat = _ABS_PATH.sub("<PATH>", flat)
    flat = _LONG_B64.sub(lambda m: f"<BASE64:{len(m.group(0))}字符>", flat)
    if len(flat) > PREVIEW_MAX_CHARS:
        tail_len = int(PREVIEW_MAX_CHARS * PREVIEW_TAIL_RATIO)
        head = flat[: PREVIEW_MAX_CHARS - tail_len]
        tail = flat[-tail_len:]
        flat = head + " … " + tail
    return flat


def command_preview(argv: list[str]) -> str:
    """纯函数：argv → 人能读的命令预览。

    `-EncodedCommand` 会被**解码回脚本**再展示（否则预览就是一串 base64，等于没有预览）；
    解码后照样走 `redact_text` 脱敏与截断。
    """
    parts: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-EncodedCommand" and i + 1 < len(argv):
            script = decode_powershell(argv[i + 1])
            if script is not None:
                parts.append("-Command")
                parts.append(redact_text(script))
                i += 2
                continue
        parts.append(redact_text(arg))
        i += 1
    return redact_text(" ".join(parts))


# --------------------------------------------------------------------------
# 纯函数：构造命令
# --------------------------------------------------------------------------

_TOAST_SCRIPT_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
$null = [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
$xmlText = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__XML_B64__'))
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xmlText)
$toast = New-Object Windows.UI.Notifications.ToastNotification -ArgumentList $doc
$toast.ExpirationTime = [DateTimeOffset]::Now.AddSeconds(__TIMEOUT__)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('__AUMID__').Show($toast)
Write-Output 'WINUSE_NOTIFY_OK'
"""


def build_toast_xml(spec: NotifySpec) -> str:
    """纯函数：构造 toast XML（标题/正文分行显示，级别映射到 scenario）。"""
    scenario = LEVEL_SCENARIOS.get(spec.level)
    attr = f' scenario="{scenario}"' if scenario else ""
    texts = [f"<text>{xml_escape(spec.title)}</text>"]
    if spec.body:
        texts.append(f"<text>{xml_escape(spec.body)}</text>")
    return f'<toast{attr}><visual><binding template="ToastGeneric">{"".join(texts)}</binding></visual></toast>'


def build_toast_script(spec: NotifySpec) -> str:
    """纯函数：生成发 WinRT toast 的 PowerShell 脚本。"""
    return (
        _TOAST_SCRIPT_TEMPLATE.replace("__XML_B64__", _b64_utf8(build_toast_xml(spec)))
        .replace("__TIMEOUT__", str(int(spec.timeout_seconds)))
        .replace("__AUMID__", POWERSHELL_AUMID)
    )


def build_toast_command(spec: NotifySpec) -> list[str]:
    """纯函数：toast 通道的 argv。"""
    return [
        POWERSHELL_EXE,
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encode_powershell(build_toast_script(spec)),
    ]


def build_msg_command(spec: NotifySpec) -> list[str]:
    """纯函数：msg 通道的 argv。

    目标会话（R-19）：
    - 默认 = **当前登录用户**（`getpass.getuser()`，即当前会话身份），
      不是 `*`——`*` 会把正文送到所有会话（RDP、快速用户切换…）
    - `spec.broadcast=True` 才用 `*`；这条路径有确认门（`run_notify` 会拦）

    `/TIME:` 为消息在屏幕上停留的秒数。
    """
    label = LEVEL_LABELS.get(normalize_level(spec.level), "")
    head = msg_escape(spec.title)
    tail = msg_escape(spec.body)
    text = f"{label}{head}" + (f" - {tail}" if tail else "")
    target = ALL_SESSIONS if spec.broadcast else current_user()
    return [MSG_EXE, target, f"/TIME:{int(spec.timeout_seconds)}", text]


def current_user() -> str:
    """当前登录身份（`msg` 的用户名参数）。

    为什么用 `getpass` 而不是读环境变量：这不是"项目配置"（配置只读项目内 .env），
    而是"当前进程的登录身份"，属于运行环境事实；`getpass` 拿不到时会抛，
    那时宁可让通知失败也不猜一个用户名乱发。
    """
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 —— 拿不到身份就别发（避免退化成广播）
        raise NotifyNoChannel(
            "无法确定当前登录用户，拒绝发送 msg 通知",
            "无法定位消息目标时**不做广播兜底**：请改用 winrt-toast 通道",
        )


def channel_candidates(has_powershell: bool, has_msg: bool) -> list[str]:
    """纯函数：可用通道，按优先级排序（首选 winrt-toast，兜底 msg）。"""
    return [c for c in CHANNEL_ORDER if (c == "winrt-toast" and has_powershell) or (c == "msg" and has_msg)]


def build_command(channel: str, spec: NotifySpec) -> list[str]:
    """纯函数：通道 → argv。"""
    if channel == "winrt-toast":
        return build_toast_command(spec)
    if channel == "msg":
        return build_msg_command(spec)
    raise NotifyNoChannel(f"未知通知通道：{channel}", "可用：" + "、".join(CHANNEL_ORDER))


def validate_notify_spec(spec: NotifySpec) -> None:
    """纯函数：调用方错误直接抛。"""
    normalize_level(spec.level)  # 级别不合法就抛（含别名归一）
    if not str(spec.title).strip() and not str(spec.body).strip():
        raise NotifyMissingContent("通知的标题与正文都为空", "至少给 title 或 body 一项")
    try:
        seconds = int(spec.timeout_seconds)
    except (TypeError, ValueError):
        raise NotifyInvalidTimeout(
            f"timeout_seconds 不是整数：{spec.timeout_seconds!r}", "请给正整数秒，例如 10"
        ) from None
    if seconds <= 0:
        raise NotifyInvalidTimeout(
            f"timeout_seconds 必须为正：{seconds}", "过短的消息用户看不到，取 5~30 秒比较合适"
        )


# --------------------------------------------------------------------------
# 真跑
# --------------------------------------------------------------------------


def default_runner(argv: list[str]) -> tuple[int, str, str]:
    """默认 runner：真起进程（utf-8 读输出）。"""
    proc = subprocess.run(  # noqa: S603 - argv 由本模块构造，无 shell
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=NOTIFY_TIMEOUT_SECONDS,
    )
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def _result(
    ok: bool,
    dry_run: bool,
    channel: str | None,
    preview: str,
    code: str | None = None,
    message: str = "",
    *,
    level: str = "info",
    broadcast: bool = False,
) -> dict:
    """统一的返回结构（成功/失败同族，只是 code 有值）。"""
    out: dict[str, Any] = {
        "ok": ok,
        "dry_run": dry_run,
        "channel": channel,
        "command_preview": preview,
        "code": code,
        "level": level,
        "broadcast": bool(broadcast),
    }
    if message:
        out["message"] = message
    return out


def run_notify(
    spec: NotifySpec,
    runner: Runner | None = None,
    which: Which | None = None,
    audit=None,
    confirmed: bool = False,
) -> dict:
    """发一条通知（默认干跑）。

    - 调用方错误（级别非法 / 内容为空 / 超时非法）→ 抛 `WinUseError` 子类
    - 通道不可用、发送失败 → 返回 `ok:false` + `code`
    - 干跑只回"将要执行的命令"，且预览已脱敏；不调用任何 runner
    - 审计（R-1）：真执行才落 `notify.executed`（`msg.exe *` 会把正文发给本机
      全部会话，属对外可见动作，留痕比干跑更有意义）
    """
    validate_notify_spec(spec)
    level = normalize_level(spec.level)

    # 广播确认门（R-19）：判据在 guard 单一出处，核心自己拦（门面挡不住直接调核心的路径）
    from . import guard as guard_mod

    verdict = guard_mod.assess_notify_broadcast(bool(spec.broadcast))
    if verdict.dangerous and not confirmed:
        return {
            "ok": False,
            "dry_run": not spec.execute,
            "requires_confirm": True,
            "broadcast": True,
            "level": level,
            "danger": verdict.to_dict(),
            "error": {
                "code": verdict.code or "CONFIRM_REQUIRED",
                "message": verdict.reason,
                "detail": "确认方式：CLI 加 --confirm / MCP 传 confirm=true；不确认就只发当前会话",
            },
        }

    def result(ok, dry_run, channel, preview, code=None, message=""):
        """本函数专用的回执构造：把归一后的级别与广播标记一并带上。"""
        return _result(
            ok, dry_run, channel, preview, code, message,
            level=level, broadcast=bool(spec.broadcast),
        )

    probe = which or shutil.which
    candidates = channel_candidates(bool(probe(POWERSHELL_EXE)), bool(probe(MSG_EXE)))
    if not candidates:
        return result(
            False,
            not spec.execute,
            None,
            "",
            NotifyNoChannel.code,
            "本机既没有 powershell.exe 也没有 msg.exe，无法发送通知",
        )

    if not spec.execute:
        argv = build_command(candidates[0], spec)
        return result(True, True, candidates[0], command_preview(argv))

    run = runner or default_runner
    failures: list[str] = []
    for channel in candidates:
        argv = build_command(channel, spec)
        preview = command_preview(argv)
        try:
            code, _stdout, stderr = run(argv)
        except subprocess.TimeoutExpired:
            failures.append(f"{channel}: 超时（>{NOTIFY_TIMEOUT_SECONDS}s）")
            continue
        except OSError as exc:
            failures.append(f"{channel}: 无法启动 {exc}")
            continue
        if code == 0:
            out = result(True, False, channel, preview)
            _mark_notify_audit(audit, spec, out, channel)
            return out
        tail = " ".join((stderr or "").split())[-160:]
        failures.append(f"{channel}: 退出码 {code}" + (f" {tail}" if tail else ""))

    primary = candidates[0]
    out = result(
        False,
        False,
        primary,
        command_preview(build_command(primary, spec)),
        NotifySendFailed.code,
        "所有通道都发送失败：" + "；".join(failures),
    )
    _mark_notify_audit(audit, spec, out, primary)
    return out


def _mark_notify_audit(audit, spec: NotifySpec, out: dict, channel: str | None) -> None:
    """通知的审计（正文不进审计：只记长度与级别，避免把内容抄进日志）。"""
    from ..audit import sink_event

    written = sink_event(
        audit,
        event="notify.executed",
        action="notify",
        ok=bool(out.get("ok")),
        code=(out.get("error") or {}).get("code") if out.get("error") else None,
        detail={
            "channel": channel,
            "level": spec.level,
            "title_chars": len(spec.title or ""),
            "body_chars": len(spec.body or ""),
        },
    )
    if written is not None:
        out["audit_written"] = written
