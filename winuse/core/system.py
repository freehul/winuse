"""winuse_system：系统只读盘点 + 电源动作意图（PR-8）。

设计落点：
1. **只读盘点**：全部数据来自 Windows 原生命令——PowerShell CIM
   （`Get-CimInstance Win32_*`，**不用 wmic**：微软已弃用并逐步移除）与
   原生 `GetSystemPowerStatus`（ctypes）。本模块**不做任何写入/系统修改**。
2. **纯逻辑与进程调用分离**：`parse_*` / `normalize_power_status` / `decode_output`
   都是纯函数（喂样例字符串可穷举断言）；只有 `_powershell` 与 `read_power_status`
   真正碰系统，且二者都可**注入替换**（`describe_system(..., runner=..., power_reader=...)`），
   单测因此完全不接触真实机器。
3. **缺字段不编值**：命令返回缺项/异常时如实给 `None` 并把错误收进 `errors`
   列表（`partial: true`），绝不拿猜测值凑字段。
4. **电源动作默认干跑**：`plan_power_action` **只返回意图对象**——真正的关机/重启
   会打断用户正在做的事，必须由用户在门禁处确认后由别处执行，本模块绝不调用。

命令口径（实测要点）：
- PowerShell 的 `ConvertTo-Json` 在**只有一条记录时返回对象而不是数组**，
  解析函数必须统一成列表，否则取"第一个元素"会静默丢字段。
- 进程/磁盘的 `Size`/`WorkingSetSize` 是 uint64，PowerShell 会输出**字符串**，
  所以取整数时统一走 `_as_int`（兼容 int/float/字符串）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from .. import limit as limit_mod
from ..driver.errors import UnknownAction, WinUseError

#: 电源动作白名单：动作名 → (意图标识, 将执行的命令参数)
POWER_ACTIONS: dict[str, dict[str, Any]] = {
    "shutdown": {"intent": "power.shutdown", "would_run": ["shutdown", "/s", "/t", "60"]},
    "restart": {"intent": "power.restart", "would_run": ["shutdown", "/r", "/t", "60"]},
    "sleep": {
        "intent": "power.sleep",
        # 注意：SetSuspendState 的第一个参数 0=休眠关闭、第二个 1=强制；
        # 若系统开了休眠，同一条命令可能变成"休眠"而不是"睡眠"，执行方需自行确认。
        "would_run": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
    },
    "lock": {"intent": "power.lock", "would_run": ["rundll32.exe", "user32.dll,LockWorkStation"]},
    "logoff": {"intent": "power.logoff", "would_run": ["shutdown", "/l"]},
}

#: 干跑统一说明（写入意图对象，防止调用方误以为已经执行）
POWER_NOTE = "需用户确认后才执行，本模块不执行"

#: 只读盘点的口径声明（随输出一起下发，避免上层把"权限不足"当成"没有硬件"）
SYSTEM_NOTE = (
    "只读盘点：数据来自 PowerShell CIM 与 GetSystemPowerStatus；"
    "某分区为空即该分区命令失败（见 errors），不代表硬件不存在；"
    "电源类动作只出意图对象（plan_power_action），本模块不执行任何关机/重启/锁屏。"
)

#: 进程 Top N 的默认值与上限（防止把整机几百个进程灌进上下文）
DEFAULT_TOP_PROCESSES = 15
MAX_TOP_PROCESSES = limit_mod.MAX_TOP_PROCESSES

#: 单条盘点命令的超时（实测 WMI 偶发卡顿会把 20s 耗光，故留够余量）
DEFAULT_TIMEOUT_SECONDS = 30.0

#: 命令级失败（超时/退出码非 0）的额外重试次数：WMI 冷启动与仓库占用是**偶发**的，
#: 一次重试能把"这次没有磁盘数据"变成"下一次有"，而不掩盖持续性的真故障
DEFAULT_RETRIES = 1

#: PowerShell 输出编码前缀：默认控制台编码在中文机器上是 GBK，先声明 UTF-8 更稳
_PS_UTF8_PREFIX = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "

_PS_OS = (
    "Get-CimInstance Win32_OperatingSystem | "
    "Select-Object Caption,Version,BuildNumber,OSArchitecture,"
    "TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json -Compress"
)
_PS_CPU = (
    "Get-CimInstance Win32_Processor | "
    "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress"
)
_PS_DISKS = (
    'Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | '
    "Select-Object DeviceID,VolumeName,FileSystem,Size,FreeSpace | ConvertTo-Json -Compress"
)
_PS_PROCS = (
    "Get-CimInstance Win32_Process | Sort-Object WorkingSetSize -Descending | "
    "Select-Object -First {n} ProcessId,Name,WorkingSetSize | ConvertTo-Json -Compress"
)


class SystemProbeError(WinUseError):
    """系统盘点命令拿不到可用输出（命令缺失、超时、退出码非 0、输出不是 JSON）。"""

    code = "SYSTEM_PROBE_ERROR"


@dataclass
class SystemSpec:
    """winuse_system 的可选项（门面翻译成它，业务逻辑只在这里）。"""

    #: 按内存排序返回的进程条数（0 表示不要进程列表）
    top_processes: int = DEFAULT_TOP_PROCESSES
    #: 单条命令超时（秒）
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    #: 命令级失败（超时/退出码非 0）的额外重试次数
    retries: int = DEFAULT_RETRIES


# --------------------------------------------------------------------------
# 纯函数：输出解码 / JSON 解析 / 逐段解析
# --------------------------------------------------------------------------


def decode_output(raw: bytes | None) -> str:
    """把命令的原始字节解码成文本。

    顺序有讲究：BOM → UTF-8 → GBK → 带替换的 UTF-8。
    不能只用 UTF-8（中文机器上控制台是 GBK，直接解会乱码或抛错），
    也不能只用 GBK（会把 UTF-8 的中文解成乱码而不是报错，静默污染字段）。
    """
    if not raw:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def parse_json_payload(text: str) -> Any:
    """把命令输出解析成 JSON 对象/数组。

    真实命令输出前可能夹着 WARN/进度行（如首行 `WRN` 提示），整段 `json.loads`
    会失败——此时退化为"截取首个 { 或 [ 到对应的最后一个闭合符"。
    """
    if not text or not text.strip():
        raise SystemProbeError("命令没有输出", "空输出通常是命令未找到或 PowerShell 启动失败")
    cleaned = text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    best: int | None = None
    end = -1
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start == -1:
            continue
        tail = cleaned.rfind(closer)
        if tail <= start:
            continue
        if best is None or start < best:
            best, end = start, tail
    if best is None:
        raise SystemProbeError("输出里找不到 JSON", f"片段={cleaned[:120]!r}")
    try:
        return json.loads(cleaned[best : end + 1])
    except json.JSONDecodeError as exc:
        raise SystemProbeError("输出不是合法 JSON", f"{exc} :: 片段={cleaned[best : best + 120]!r}") from exc


def as_records(payload: Any) -> list[Any]:
    """统一成记录列表：`ConvertTo-Json` 对单条记录返回对象而不是数组。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    return [payload]


def _as_int(value: Any, default: int | None = None) -> int | None:
    """尽力取整数（CIM 的 uint64 会以字符串到达），取不到给默认值，**不猜 0**。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return default
    return default


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def used_percent(total_bytes: int | None, free_bytes: int | None) -> float | None:
    """已用占比（%）。总量未知或为 0 时给 None——不做除零，也不编 0%。"""
    if not total_bytes or free_bytes is None:
        return None
    used = total_bytes - free_bytes
    return round(used * 100.0 / total_bytes, 1)


def parse_os_info(text: str) -> dict:
    """解析 Win32_OperatingSystem 输出。

    返回扁平字典（含内存字段，便于与 `parse_cpu_info` 一样单测）：
    caption / version / build / arch / memory_total_bytes / memory_available_bytes。
    CIM 报的是 **KB**，这里统一换算成字节，避免上层再猜单位。
    """
    records = as_records(parse_json_payload(text))
    record = next((r for r in records if isinstance(r, dict)), None)
    if record is None:
        raise SystemProbeError("Win32_OperatingSystem 输出里没有对象", f"片段={str(text)[:120]!r}")
    total_kb = _as_int(record.get("TotalVisibleMemorySize"))
    free_kb = _as_int(record.get("FreePhysicalMemory"))
    return {
        "caption": _as_text(record.get("Caption")),
        "version": _as_text(record.get("Version")),
        "build": _as_text(record.get("BuildNumber")),
        "arch": _as_text(record.get("OSArchitecture")),
        "memory_total_bytes": total_kb * 1024 if total_kb is not None else None,
        "memory_available_bytes": free_kb * 1024 if free_kb is not None else None,
    }


def parse_cpu_info(text: str) -> dict:
    """解析 Win32_Processor 输出（多路 CPU 会返回多条，按和汇总）。"""
    records = [r for r in as_records(parse_json_payload(text)) if isinstance(r, dict)]
    if not records:
        raise SystemProbeError("Win32_Processor 输出里没有对象", f"片段={str(text)[:120]!r}")
    names = [_as_text(r.get("Name")) for r in records]
    cores = [_as_int(r.get("NumberOfCores")) for r in records]
    logical = [_as_int(r.get("NumberOfLogicalProcessors")) for r in records]
    return {
        "name": next((n for n in names if n), None),
        "physical_cores": sum(c for c in cores if c) or None,
        "logical_cores": sum(c for c in logical if c) or None,
        "socket_count": len(records),
    }


def parse_disks(text: str) -> list[dict]:
    """解析 Win32_LogicalDisk 输出（DriveType=3 即本地固定盘）。"""
    out: list[dict] = []
    for record in as_records(parse_json_payload(text)):
        if not isinstance(record, dict):
            continue
        total = _as_int(record.get("Size"))
        free = _as_int(record.get("FreeSpace"))
        out.append(
            {
                "drive": _as_text(record.get("DeviceID")),
                "label": _as_text(record.get("VolumeName")),
                "filesystem": _as_text(record.get("FileSystem")),
                "total_bytes": total,
                "free_bytes": free,
                "used_bytes": (total - free) if (total is not None and free is not None) else None,
                "used_percent": used_percent(total, free),
            }
        )
    return out


def parse_processes(text: str, top_n: int = DEFAULT_TOP_PROCESSES) -> list[dict]:
    """解析进程输出：按 WorkingSetSize 降序取前 top_n 条。"""
    limit = max(0, int(top_n))
    trimmed: list[dict] = []
    for record in as_records(parse_json_payload(text)):
        if not isinstance(record, dict):
            continue
        bytes_used = _as_int(record.get("WorkingSetSize"))
        trimmed.append(
            {
                "pid": _as_int(record.get("ProcessId")),
                "name": _as_text(record.get("Name")),
                "memory_bytes": bytes_used,
                "memory_mib": round(bytes_used / 1048576, 1) if bytes_used is not None else None,
            }
        )
    trimmed.sort(key=lambda item: item["memory_bytes"] if item["memory_bytes"] is not None else -1, reverse=True)
    return trimmed[:limit]


def normalize_power_status(
    ac_line_status: Any = None, battery_flag: Any = None, battery_percent: Any = None
) -> dict:
    """把 SYSTEM_POWER_STATUS 的原始字段归一成可读事实（纯函数）。

    口径（按 MSDN 的 SYSTEM_POWER_STATUS 定义，**不是靠猜**）：
    - `ACLineStatus`：0=用电池、1=接电源、255=未知（未知不猜）
    - `BatteryFlag`：1/2/4=电量档、8=充电中；**128=本机没有系统电池**（台式机实测就是这个）；
      255=状态读不到。所以"有电池"= 有档位位且**不带 0x80 位**；128/255/0 一律判为未知或没有。
    - `BatteryLifePercent`：255=未知（没有电池的机器也报 255）
    """
    ac = _as_int(ac_line_status)
    on_ac = True if ac == 1 else (False if ac == 0 else None)
    flag = _as_int(battery_flag)
    present: bool | None
    if flag is None or flag in (0, 255):
        present = None
    else:
        present = (flag & 0x80) == 0
    percent = _as_int(battery_percent)
    return {
        "on_ac": on_ac,
        "battery_present": present,
        "charge_percent": None if percent is None or percent > 100 else percent,
        "note": (
            "on_ac=null 表示系统未报电源状态（不是\"没接电源\"）；"
            "battery_present=false 表示本机没有系统电池（BatteryFlag=128），"
            "null 表示读不到电池标志；没有电池的机器 charge_percent 通常为 null。"
        ),
    }


# --------------------------------------------------------------------------
# 原生调用（可注入替换）
# --------------------------------------------------------------------------


def _powershell(script: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """跑一段 PowerShell（只读命令），返回解码后的 stdout。

    找不到解释器 / 超时 / 退出码非 0 都归一成 SystemProbeError，
    让上层按 code 判断是"环境缺命令"还是"命令失败"。
    """
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise SystemProbeError("找不到 PowerShell", "请确认 powershell.exe 或 pwsh 在 PATH 上")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", _PS_UTF8_PREFIX + script],
            capture_output=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemProbeError(f"PowerShell 超时（>{timeout}s）", str(exc)) from exc
    if proc.returncode != 0:
        raise SystemProbeError(
            f"PowerShell 退出码 {proc.returncode}",
            decode_output(proc.stderr)[:200],
        )
    return decode_output(proc.stdout)


def read_power_status() -> dict:
    """读电源状态（原生 GetSystemPowerStatus）。非 Windows 时返回可用性为假的结构。"""
    if os.name != "nt":
        out = normalize_power_status(None, None, None)
        out["available"] = False
        return out
    try:
        import ctypes
        from ctypes import wintypes

        class _PowerStatus(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD),
            ]

        status = _PowerStatus()
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        if not ok:
            raise OSError("GetSystemPowerStatus 返回失败")
        out = normalize_power_status(
            status.ACLineStatus, status.BatteryFlag, status.BatteryLifePercent
        )
        out["available"] = True
        return out
    except Exception as exc:  # noqa: BLE001 - 归一化成结构，不让异常穿层
        out = normalize_power_status(None, None, None)
        out["available"] = False
        out["error"] = str(exc)[:200]
        return out


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def _section(
    errors: list[dict], name: str, func: Callable[[], Any], retries: int = 0
) -> Any:
    """跑一段盘点：失败只记录不抛（一个分区失败不该让整份盘点作废）。

    `retries` 额外重试次数只作用于**命令级失败**（`SystemProbeError`：超时/退出码非 0），
    因为 WMI 卡顿是偶发的；代码级异常（TypeError 之类）不重试——重试只会把 bug 拖慢。
    """
    attempts = max(1, int(retries) + 1)
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return func()
        except SystemProbeError as exc:
            last = exc
            continue
        except WinUseError as exc:
            errors.append({"section": name, **exc.to_dict()})
            return None
        except Exception as exc:  # noqa: BLE001
            errors.append({"section": name, "code": "UNEXPECTED", "message": str(exc)[:200]})
            return None
    if isinstance(last, WinUseError):
        errors.append({"section": name, **last.to_dict()})
    elif last is not None:  # pragma: no cover - 兜底，正常路径不会到这里
        errors.append({"section": name, "code": "UNEXPECTED", "message": str(last)[:200]})
    return None


def describe_system(
    spec: SystemSpec | None = None,
    runner: Callable[[str], str] | None = None,
    power_reader: Callable[[], dict] | None = None,
) -> dict:
    """只读盘点本机：OS/CPU/内存/磁盘/电源/进程 Top N。

    `runner` 与 `power_reader` 可注入（单测用桩，完全不碰真实机器）。
    """
    spec = spec or SystemSpec()
    top_n = max(0, min(int(spec.top_processes), MAX_TOP_PROCESSES))
    retries = max(0, int(spec.retries))
    run = runner or (lambda script: _powershell(script, timeout=spec.timeout_seconds))
    read_power = power_reader or read_power_status

    errors: list[dict] = []
    os_info = _section(errors, "os", lambda: parse_os_info(run(_PS_OS)), retries)
    cpu_info = _section(errors, "cpu", lambda: parse_cpu_info(run(_PS_CPU)), retries)
    disks = _section(errors, "disks", lambda: parse_disks(run(_PS_DISKS)), retries)
    procedures = (
        _section(
            errors,
            "processes",
            lambda: parse_processes(run(_PS_PROCS.format(n=top_n)), top_n),
            retries,
        )
        if top_n
        else []
    )
    power = _section(errors, "power", read_power, retries)

    os_out = None
    memory = None
    if os_info:
        os_out = {key: os_info.get(key) for key in ("caption", "version", "build", "arch")}
        memory = {
            "total_bytes": os_info.get("memory_total_bytes"),
            "available_bytes": os_info.get("memory_available_bytes"),
            "used_percent": used_percent(
                os_info.get("memory_total_bytes"), os_info.get("memory_available_bytes")
            ),
        }

    out: dict[str, Any] = {
        "ok": True,
        "os": os_out,
        "cpu": cpu_info,
        "memory": memory,
        "disks": disks or [],
        "power": power,
        "processes_top": procedures or [],
        "top_n": top_n,
        "partial": bool(errors),
        "errors": errors,
        "note": SYSTEM_NOTE,
    }
    return out


def plan_power_action(action: str) -> dict:
    """生成电源动作的**意图对象**——本模块绝不执行关机/重启/睡眠/注销。

    为什么不执行：这类动作会打断用户正在做的事、可能丢未保存的数据，属于
    不可逆的高风险操作。按项目铁律，默认干跑：本函数只回"将会运行什么命令"，
    由门禁在用户确认后交给执行方（人工或受控脚本）落地。
    """
    key = str(action or "").strip().lower()
    entry = POWER_ACTIONS.get(key)
    if entry is None:
        raise UnknownAction(
            f"不支持的电源动作：{action}",
            "可用：" + "、".join(sorted(POWER_ACTIONS)),
        )
    return {
        "ok": True,
        "requires_confirm": True,
        "intent": entry["intent"],
        "would_run": list(entry["would_run"]),
        "note": POWER_NOTE,
    }
