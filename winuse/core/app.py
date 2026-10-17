"""winuse_app：应用生命周期（launch / list / kill）——P1-3。

**为什么要有这个模块**：P1-3 之前 WinUse 只能操作"已经开着的窗口"——agent 想
打开一个应用、想知道某个应用装没装、想收尾自己启动的进程，全都做不到。驱动层
（cua-driver 0.28.2）早就有 `launch_app` / `list_apps` / `kill_app` 三个工具，
缺的只是核心层把它们翻成项目口径：**默认干跑、危险动作先确认、结果裁剪留痕、
每次尝试落审计**。

四条关键纪律：

1. **默认干跑**：`run_app(..., execute=False)` 只回"将要下发的驱动调用"，一行都不
   发。**三个动作一律如此**（含只读的 `list`）——这是项目既有约定（`core.act` /
   `core.desktop` / `core.notify` 同款），不给本模块开特例。`list` 因此带
   `read_only: true` 标注：它没有副作用，调用方要数据就显式 `execute=True`。
2. **危险判定下沉到核心、判据单一出处（`core.guard`）**：
   - `kill` **必须** `confirm=True`——未确认一律返回 `CONFIRM_REQUIRED` 且不下发
     （强杀会丢未保存数据，且驱动等价于 `taskkill /F`）。判据来自
     `guard.assess_action("kill", ...)`，本模块**不另写一份危险清单**。
   - `launch` 默认可执行，但目标若命中 `guard.SYSTEM_APPS`（`lockapp.exe` /
     `consent.exe` / `winlogon.exe` / `explorer.exe` / `systemsettings.exe` …）
     则继承既有的"系统级窗口"规则：启动锁屏/同意界面/系统外壳都属于"操作系统
     本体"那一类，要确认。做法是把目标喂给 `guard.assess_action`，清单将来加名字
     这里自动覆盖——而不是在这里抄一份系统应用名。
   - 判定顺序与 `core.act` 一致：**先过安全门，再校验参数**（没确认就报"要确认"，
     那是调用方当下最该解决的事）。
3. **结果裁剪要留痕（`list`）**：驱动 `list_apps` 原始回执**实测 92,706 字节 /
   261 条**（本机实测，含 105 个 UWP 包），整块丢给上层会顶破 MCP 的 24,000 字节
   线路预算。`trim_apps` 做三件事：**投影**（只留可行动字段，`windows` 数组折成
   `window_count`）、**过滤**（`running` / `kind` / `query`）、**截断并标注**
   （`truncated` / `returned` / `total_before_truncate` / `truncate_hint`，与
   `winuse/limit.py` 同形——裁掉多少、为什么裁必须写在回包里）。
   `MAX_APP_LIMIT = 80` 是**量出来的**：投影后每条约 227 字节（实测均摊），
   80 条 ≈ 17.7 KB，留在 24 KB 预算内有约 26% 余量；100 条已到 22.4 KB（只有
   6% 余量），路径再长一点就会被判 `RESULT_TOO_LARGE`。
4. **`pid` 只认驱动给的**：启动回执没有 `pid`（或没有窗口）时**如实说明**，不编值。
   驱动文档明确"启动未稳定时 `windows` 会是空的"是正常瞬时状态，所以这种情况给
   提示而不是报错。

返回结构（三种回执同族，agent 按 `ok` + `code` 分支即可）：

    成功（干跑） {"ok": true, "dry_run": true, "action", "tool", "target",
                "would_call": {"tool", "args", "note"}, "code": null, ["read_only"],
                ["danger"], ["audit_written"]}
    成功（真跑） {"ok": true, "dry_run": false, "action", "tool", "target",
                "result": {...} | "apps"/"counts" 系列, "code": null, ["note"], ...}
    失败/被拦  {"ok": false, "dry_run", "action", "tool", "target",
                "would_call": null, "code", "error": {"code", "message", "detail"},
                ["requires_confirm", "danger"], ["audit_written"]}

`list` 的清单放在**顶层**（`apps` / `apps_total` / …）而不是塞进 `result`：它读的是
"一份清单"，与 `limit.py` 的裁剪标注同形更便于上层复用；`launch` / `kill` 回的是
"一次动作的结果"，放 `result`。

**未实测声明（如实说，不编）**：`kill_app` 的回执形态没有实测——实测要真杀进程，
不在本 PR 的允许范围内。因此 `kill` 的 `result` **原样透传**驱动字段（不猜字段名、
不静默丢字段），`ok` 的判据只来自驱动拒绝语义（`act._driver_refusal`，与
`core.act` 共用同一份实现，不复制）。`launch_app` 的回执形态来自 `describe
launch_app` 的官方 schema（`pid` / `name` / `bundle_id` / `active` / `windows`），
所以那边可以放心投影。
"""

from __future__ import annotations

import math
from pathlib import PureWindowsPath
from typing import Any

from ..driver.errors import MissingParam, ParamOutOfRange, UnknownAction, WinUseError
from . import guard as guard_mod
from .act import _driver_refusal

#: 支持的动作 → 驱动工具（动作名与工具名的唯一映射，回执与调用都从这里取）
ACTION_TOOLS: dict[str, str] = {
    "launch": "launch_app",
    "list": "list_apps",
    "kill": "kill_app",
}

#: 动作全集
APP_ACTIONS = frozenset(ACTION_TOOLS)

#: 干跑 / 真跑的统一说明（写进回执，免得调用方把"计划"当"已执行"）
DRY_RUN_NOTE = "干跑：未下发任何驱动调用（显式 execute=True 才真实执行）"
READ_ONLY_NOTE = (
    "list 是只读动作（不改任何桌面状态），但仍按项目约定默认干跑："
    "带 execute=True 才真正读驱动清单"
)
#: launch 的干跑说明带一句驱动行为事实：启动本来就不抢前台；要全程不露窗口另有开关
LAUNCH_NOTE = (
    DRY_RUN_NOTE
    + "；驱动启动不抢前台（SW_SHOWNOACTIVATE，用户当前窗口仍是前台）；"
    "要连窗口都不出现在屏幕上，用 options={'start_minimized': True}"
)

#: `kill` 的固定提示（驱动文档：能用关闭就别用强杀）
KILL_NOTE = (
    "kill_app 是强杀（驱动侧等价 taskkill /F）：目标未保存的内容会丢失。"
    "能走正常关闭（winuse_window --action close）就别用强杀"
)

#: 清单投影口径（随回执一起下发，避免上层把"pid=0"读成"进程号是 0"）
APP_LIST_NOTE = (
    "清单已投影：只保留可行动字段，驱动的 windows 数组折成 window_count"
    "（要窗口级细节用 winuse_state）；pid=0 表示该应用未运行（驱动口径，不是真实进程号）；"
    "launch_path 就是 launch_app 要吃的路径"
)

#: `launch` 允许透传的驱动字段（驱动 launch_app schema 的其它可选项）。
#: `path` / `additional_arguments` 有专属参数（path / args），不从这里走。
LAUNCH_OPTION_KEYS = ("name", "bundle_id", "aumid", "launch_path", "urls", "start_minimized")

#: `list` 的筛选参数（**只在本模块内做过滤，不下发给驱动**——list_apps 的 schema
#: 是 additionalProperties:false 且不带属性，传任何键都会被驱动拒）
LIST_OPTION_KEYS = ("running", "kind", "query", "limit")

#: 清单默认返回条数与硬上限（上限是量出来的，见模块 docstring 纪律 3）
DEFAULT_APP_LIMIT = 50
MAX_APP_LIMIT = 80

#: 可识别的应用类型
APP_KINDS = ("desktop", "uwp")


class BadParam(WinUseError):
    """参数**存在但取值非法**（类型不对、pid 非正、limit 越界、认不出的选项）。

    为什么定义在本模块而不上提 `driver/errors.py`：`MISSING_PARAM` /
    `PARAM_OUT_OF_RANGE` / `UNKNOWN_ACTION` 都是既有共享错误码，能复用就复用；
    `BAD_PARAM` 目前只被本模块的动作参数校验使用，就近声明可避免为一个新错误码
    去动共享文件（做法与 `core/desktop.py` 的 `BadParam` 一致）。若将来有第二个
    模块需要它，应上提到 `winuse.driver.errors` 并同步 `__all__`。
    """

    code = "BAD_PARAM"


# --------------------------------------------------------------------------
# 纯逻辑小工具
# --------------------------------------------------------------------------


def _as_text(value: Any) -> str | None:
    """取文本：None/空白 → None（**不编空串**，让"驱动没给"和"给了空值"可区分）。"""
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _basename(value: Any) -> str:
    """路径 / 标识 → 文件名（小写）。判定系统级二进制时按**文件名**而不是整条路径。

    用 `PureWindowsPath`：它在任何平台上都按 Windows 规则解析（`C:/a/b.exe` 与
    `C:\\a\\b.exe` 都能给出 `b.exe`），单测在非 Windows 跑也一致。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return PureWindowsPath(text).name.strip().lower()


def _identifier(path: Any = None, options: Any = None) -> str | None:
    """本动作的"目标标识"（用于危险判定与审计）：来自 path 或 options 里的定位字段。"""
    if _as_text(path):
        return str(path)
    opts = options if isinstance(options, dict) else {}
    for key in ("aumid", "bundle_id", "name", "launch_path"):
        if _as_text(opts.get(key)):
            return str(opts[key])
    return None


def _receipt_target(pid: Any, path: Any, options: Any) -> dict | None:
    """回执里的 target（形状与其它模块同族：`app` + `pid`）。都没有时给 None。"""
    ident = _identifier(path, options)
    if ident is None and pid is None:
        return None
    out: dict[str, Any] = {}
    if ident is not None:
        out["app"] = ident
    if pid is not None:
        out["pid"] = pid
    return out


def _normalize_args(args: Any) -> list[str]:
    """把 `args` 归一成驱动要的 `additional_arguments`（字符串列表）。

    - `None` / 空列表 → 不带这个字段（空数组没有意义）
    - 单个字符串 → **一个**参数（**不按空格切分**：切分会把带空格的路径拆坏，
      那正是"猜"）
    - 列表 / 元组 → 逐项必须是字符串（非字符串一律拒，不做 str() 转换）
    """
    if args is None:
        return []
    if isinstance(args, str):
        return [args] if args.strip() else []
    if isinstance(args, (list, tuple)):
        out: list[str] = []
        for item in args:
            if not isinstance(item, str):
                raise BadParam(
                    f"args 里只能放字符串，收到 {type(item).__name__}：{item!r}",
                    "用法示例：args=['--profile', 'demo']；数字/布尔请自己转成字符串",
                )
            out.append(item)
        return out
    raise BadParam(
        f"args 必须是字符串或字符串列表，收到 {type(args).__name__}",
        "用法示例：args='--demo' 或 args=['--a', 'b c']",
    )


def _as_pid(value: Any) -> int:
    """pid 校验：必须是正整数（bool 是 int 子类，最容易蒙混过关）。"""
    if value is None:
        raise MissingParam(
            "kill 需要 pid",
            "pid 来自 winuse_app list 的清单（running=true 的那条），不要手填",
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadParam(
            f"pid 必须是正整数，收到 {type(value).__name__}：{value!r}",
            "pid 是数字；字符串/布尔不行（避免 '123' 与 123 的歧义）",
        )
    if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
        raise BadParam(f"pid 必须是整数值，收到 {value!r}", "进程号没有小数")
    out = int(value)
    if out <= 0:
        raise BadParam(
            f"pid 必须为正整数，收到 {out}",
            "0 是驱动表达\"该应用未运行\"的哨兵值（见 list 的 pid 字段），不是可杀对象",
        )
    return out


def _option_hint(action: str) -> str:
    keys = LAUNCH_OPTION_KEYS if action == "launch" else LIST_OPTION_KEYS
    if not keys:  # kill
        return "kill 没有可选项（只需要 pid）；要收尾自己启动的进程就传 pid"
    return f"{action} 可用：" + "、".join(keys)


def _launch_option(key: str, value: Any) -> Any:
    """launch 选项逐一校验：字符串类字段必须是非空字符串，布尔字段必须是真布尔。"""
    if key == "urls":
        if not isinstance(value, (list, tuple)) or not value:
            raise BadParam(
                f"urls 必须是非空字符串列表，收到 {value!r}",
                "每个 URL 一个字符串，例如 ['https://example.invalid/']",
            )
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise BadParam(f"urls 里只能放非空字符串，收到 {item!r}", "去掉空项或改成字符串")
        return [str(item) for item in value]
    if key == "start_minimized":
        if not isinstance(value, bool):
            raise BadParam(
                f"start_minimized 必须是布尔值，收到 {type(value).__name__}",
                "请用 true / false（不要用 1/0 或 \"yes\"，避免歧义）",
            )
        return value
    if not isinstance(value, str) or not value.strip():
        raise BadParam(
            f"{key} 必须是非空字符串，收到 {value!r}",
            "例如 aumid='Vendor.App_abc!App'",
        )
    return value


def _list_option(key: str, value: Any) -> Any:
    """list 筛选参数校验（只在本模块内使用，不下发驱动）。"""
    if key == "running":
        if not isinstance(value, bool):
            raise BadParam(
                f"running 必须是布尔值，收到 {type(value).__name__}",
                "只筛运行中给 true，只筛未运行给 false",
            )
        return value
    if key == "kind":
        if not isinstance(value, str) or value.strip().lower() not in APP_KINDS:
            raise BadParam(
                f"kind 必须是 {' / '.join(APP_KINDS)} 之一，收到 {value!r}",
                "desktop = .exe 桌面应用；uwp = 商店打包应用",
            )
        return value.strip().lower()
    if key == "query":
        if not isinstance(value, str):
            raise BadParam(f"query 必须是字符串，收到 {type(value).__name__}", "按关键字收窄清单")
        return value
    # limit
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadParam(
            f"limit 必须是整数，收到 {type(value).__name__}：{value!r}",
            f"默认 {DEFAULT_APP_LIMIT}，上限 {MAX_APP_LIMIT}",
        )
    if value <= 0:
        raise BadParam(
            f"limit 必须为正整数，收到 {value}",
            "0 条清单没有意义；不筛就传大一点的 limit（上限 80）",
        )
    if value > MAX_APP_LIMIT:
        raise ParamOutOfRange(
            f"limit={value} 超过上限 {MAX_APP_LIMIT}",
            f"投影后每条约 227 字节（实测），{MAX_APP_LIMIT} 条约 17.7 KB；"
            "再大就会顶破 MCP 的 24000 字节线路预算被判 RESULT_TOO_LARGE。"
            "要更全的清单请用 options 的 query / running / kind 收窄",
        )
    return value


def _validated_options(action: str, options: Any) -> dict:
    """选项校验：认不出的键**直接拒**（静默丢弃会让调用方以为已生效）。"""
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise BadParam(
            f"options 必须是对象（dict），收到 {type(options).__name__}",
            "用法示例：options={'running': True, 'limit': 20}",
        )
    allowed = LAUNCH_OPTION_KEYS if action == "launch" else (LIST_OPTION_KEYS if action == "list" else ())
    unknown = sorted(str(key) for key in options if key not in allowed)
    if unknown:
        raise BadParam(
            "不支持的选项：" + "、".join(unknown),
            f"动作 {action} " + _option_hint(action),
        )
    out: dict = {}
    for key, value in options.items():
        out[key] = _launch_option(key, value) if action == "launch" else _list_option(key, value)
    return out


def build_app_call(
    action: str,
    *,
    pid: Any = None,
    path: Any = None,
    args: Any = None,
    options: Any = None,
) -> tuple[str, dict]:
    """纯函数：动作 → (驱动工具名, 驱动参数)（**不执行、不读状态**）。

    参数不全 / 类型不对 / 动作未知一律抛带稳定 code 的异常：
    - `UNKNOWN_ACTION`（动作不在 `APP_ACTIONS`）
    - `MISSING_PARAM`（launch 没有任何目标；kill 缺 pid）
    - `BAD_PARAM`（类型不对、空路径、认不出的选项、pid 非正）
    - `PARAM_OUT_OF_RANGE`（limit 超过上限）
    """
    name = str(action or "").strip().lower()
    tool = ACTION_TOOLS.get(name)
    if tool is None:
        raise UnknownAction(
            f"不支持的应用动作：{action}",
            "可用：" + "、".join(sorted(APP_ACTIONS)),
        )
    opts = _validated_options(name, options)

    if name == "launch":
        if pid is not None:
            raise BadParam(
                "launch 不接受 pid",
                "pid 是 kill 的目标；启动后的 pid 由驱动在回执里给出（result.pid）",
            )
        call: dict = {}
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                raise BadParam(
                    f"path 必须是非空字符串，收到 {path!r}",
                    "给可执行文件的完整路径，例如 path='C:/Apps/Demo/demo.exe'",
                )
            call["path"] = path
        call.update(opts)
        # 驱动 launch_app 的原话：bundle_id / name / aumid / path / launch_path 至少给一个
        if not any(key in call for key in ("path", "name", "bundle_id", "aumid", "launch_path", "urls")):
            raise MissingParam(
                "launch 缺少目标",
                "给 path（可执行文件路径），或用 options 传 name / bundle_id / aumid / launch_path / urls",
            )
        extra = _normalize_args(args)
        if extra:
            call["additional_arguments"] = extra
        return tool, call

    if name == "list":
        for given, label in ((path, "path"), (args, "args"), (pid, "pid")):
            if given is not None:
                raise BadParam(
                    f"list 不接受 {label}",
                    "枚举应用没有作用对象；要筛选用 options 的 " + "、".join(LIST_OPTION_KEYS),
                )
        # 驱动 list_apps 的 schema 没有属性且 additionalProperties:false → 只能传 {}
        return tool, {}

    # kill
    for given, label in ((path, "path"), (args, "args")):
        if given is not None:
            raise BadParam(
                f"kill 不接受 {label}",
                "按 pid 结束进程；path 无法定位进程（同名可执行文件可能有多个）",
            )
    return tool, {"pid": _as_pid(pid)}


# --------------------------------------------------------------------------
# 危险判定（判据单一出处：winuse.core.guard）
# --------------------------------------------------------------------------


def assess_app_action(action: str, identifier: Any = None) -> guard_mod.RiskVerdict:
    """应用动作的危险判定（判据**全部**来自 `core.guard`，本模块不另写清单）。

    - `kill`：命中 `guard.DANGEROUS_ACTIONS`（结束进程）→ 必须确认
    - `launch`：目标若经 `_basename` 命中 `guard.SYSTEM_APPS`（锁屏 / 同意界面 /
      系统外壳 / 任务管理器…）→ 走既有的"系统级窗口"规则，必须确认。这是**复用**
      既有清单而不是新增一条判据：启动这些二进制等于操作用户的操作系统本体。
    - `list`：只读枚举，永远不危险

    `identifier` **只在 `launch` 参与判定**：`kill` 的危险来自动作名本身（不看目标），
    `list` 只读。给非 launch 动作喂目标会得出误导性结论（例如 `list` 带上一条系统
    二进制的路径会被判"要确认"，而它真正的问题是"list 不接受 path"）。
    """
    name = str(action or "").strip().lower()
    target = {"app": _basename(identifier)} if (identifier and name == "launch") else None
    return guard_mod.assess_action(name, target)


# --------------------------------------------------------------------------
# 清单裁剪（投影 + 过滤 + 截断标注）
# --------------------------------------------------------------------------


def _slim_app(record: dict) -> dict:
    """单条应用记录的投影：只留"能行动"的字段。

    - `windows` 数组（驱动回执里是完整窗口对象）→ `window_count`：要窗口细节该用
      `winuse_state` / `list_windows`，塞在这里只是纯字节浪费。
    - `bundle_id` **只在它和 `launch_path` 不同才留**：桌面应用的这两个字段是同一
      个 exe 路径（实测），重复字段就是重复字节。
    """
    windows = record.get("windows")
    out: dict[str, Any] = {
        "name": _as_text(record.get("name")),
        "pid": record.get("pid"),
        "running": record.get("running"),
        "active": record.get("active"),
        "kind": _as_text(record.get("kind")),
        "launch_path": _as_text(record.get("launch_path")),
        "last_used": _as_text(record.get("last_used")),
        "window_count": len(windows) if isinstance(windows, list) else 0,
    }
    bundle = _as_text(record.get("bundle_id"))
    if bundle and bundle != out["launch_path"]:
        out["bundle_id"] = bundle
    return out


def _matches(record: dict, options: dict) -> bool:
    """筛选：running（严格相等，缺失值不算命中）/ kind / query（名称、路径、标识）。"""
    if "running" in options and bool(record.get("running")) != bool(options["running"]):
        return False
    if "kind" in options and str(record.get("kind") or "").lower() != options["kind"]:
        return False
    query = options.get("query")
    if query and str(query).strip():
        needle = str(query).strip().lower()
        haystack = " ".join(
            str(record.get(part) or "") for part in ("name", "launch_path", "bundle_id")
        ).lower()
        if needle not in haystack:
            return False
    return True


def _limit_of(options: dict) -> int:
    value = options.get("limit")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_APP_LIMIT
    return min(value, MAX_APP_LIMIT)


def trim_apps(payload: Any, options: Any = None) -> dict:
    """纯函数：驱动 `list_apps` 回执 → 投影 + 过滤 + 截断（**不复制原件、不改入参**）。

    返回（与 `winuse/limit.py` 的裁剪标注同形，裁掉多少必须写在回包里）::

        {"apps": [...], "apps_total": 驱动给了几条, "apps_matched": 过滤后几条,
         "returned": 实际返回几条, "total_before_truncate": 截断前几条,
         "truncated": bool, "truncate_hint": str|None,
         "filters": {只回显显式给的筛选项}, "note": 投影口径}

    `apps_total` 按**驱动给的原始条数**计（含解析不了的脏记录）：计数如实报告，
    不假装驱动只给了能用的那几条。
    """
    opts = options if isinstance(options, dict) else {}
    records = payload.get("apps") if isinstance(payload, dict) else payload
    records = records if isinstance(records, list) else []

    matched: list[dict] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = _slim_app(raw)
        if _matches(record, opts):
            matched.append(record)

    limit = _limit_of(opts)
    kept = matched[:limit]
    truncated = len(kept) < len(matched)
    out: dict[str, Any] = {
        "apps": kept,
        "apps_total": len(records),
        "apps_matched": len(matched),
        "returned": len(kept),
        "total_before_truncate": len(matched),
        "truncated": truncated,
        "truncate_hint": None,
        "filters": {key: opts[key] for key in ("running", "kind", "query") if key in opts},
        "note": APP_LIST_NOTE,
    }
    if truncated:
        out["truncate_hint"] = (
            f"清单超过上限：命中 {len(matched)} 条，已返回前 {len(kept)} 条"
            f"（limit={limit}，上限 {MAX_APP_LIMIT}）；"
            "收窄请用 options 的 query / running / kind，或调大 limit"
        )
    return out


# --------------------------------------------------------------------------
# 控制流：回执构造 + 审计
# --------------------------------------------------------------------------


def _write_app_audit(
    audit,
    event: str,
    *,
    action: str,
    ok: bool,
    code: str | None,
    target: dict | None,
    detail: dict,
) -> bool | None:
    """应用动作的审计（复用 `audit.sink_event` 协议与 `core.desktop` 的事件风格）。

    `detail` 里**绝不放命令行参数正文**（可能含 token / 本机路径），只放条数。
    """
    from ..audit import sink_event

    return sink_event(
        audit,
        event=event,
        action=action,
        ok=ok,
        code=code,
        target=target,
        detail={"app_action": True, **detail},
    )


def _mark(out: dict, written: bool | None) -> dict:
    """把落盘结果挂进回执：没有 sink 时不加字段（不污染回执形状）。"""
    if written is not None:
        out["audit_written"] = written
    return out


def _failure(action: str, exc: WinUseError, execute: bool, target: dict | None) -> dict:
    return {
        "ok": False,
        "dry_run": not execute,
        "action": action or None,
        "tool": ACTION_TOOLS.get(action),
        "target": target,
        "would_call": None,
        "code": exc.code,
        "error": exc.to_dict(),
    }


def _blocked(
    action: str,
    verdict: guard_mod.RiskVerdict,
    execute: bool,
    target: dict | None,
    audit_written: bool | None,
) -> dict:
    """危险且未确认 → 结构化拒绝（形状与 `core.act` / `core.desktop` 一致）。

    拒绝时**不返回任何调用计划**：拿一份"会下发什么"容易被误当成"已下发过"。
    """
    out = {
        "ok": False,
        "dry_run": not execute,
        "action": action or None,
        "tool": ACTION_TOOLS.get(action),
        "target": target,
        "would_call": None,
        "code": verdict.code,
        "requires_confirm": True,
        "danger": verdict.to_dict(),
        "error": {
            "code": verdict.code,
            "message": f"应用动作 {action} 被安全层拦截：{verdict.reason}",
            "detail": (
                f"规则：{verdict.rule}；"
                "确认方式：CLI 加 --confirm / MCP 传 confirm=true"
            ),
        },
    }
    return _mark(out, audit_written)


#: `launch_app` 回执里的窗口投影字段（驱动给的窗口与 `list_windows` 同形，
#: bounds / z_index / DPI 之类留给 winuse_state —— 这里只要"认得出哪个窗口"）
WINDOW_FIELDS = ("window_id", "pid", "title", "app_name", "minimized")


def _slim_window(window: dict) -> dict:
    return {key: window.get(key) for key in WINDOW_FIELDS}


def _slim_launch_result(response: Any) -> dict:
    """启动回执投影（驱动 launch_app 的 schema：pid / name / bundle_id / active / windows）。"""
    data = response if isinstance(response, dict) else {}
    windows = data.get("windows") if isinstance(data.get("windows"), list) else []
    return {
        "pid": data.get("pid"),
        "name": _as_text(data.get("name")),
        "bundle_id": _as_text(data.get("bundle_id")),
        "active": data.get("active"),
        "window_count": len(windows),
        "windows": [_slim_window(item) for item in windows if isinstance(item, dict)],
    }


def _launch_note(result: dict) -> str | None:
    """启动回执不完整时的如实提示（**不编 pid，也不把"没窗口"当失败**）。

    驱动文档明确：launch 落定但窗口还没出现是**正常瞬时状态**，此时 windows 为空，
    过一会儿再 list_windows(pid) 就有。所以这里给的是提示而不是错误码。
    """
    pid = result.get("pid")
    if pid is None:
        return (
            "驱动回执里没有 pid：拿不到进程号就无法确认这次启动（也拿不到后续要用的 pid）。"
            "请稍后用 winuse_state 按应用名复查，或看 winuse_audit 的行动记录"
        )
    if not result.get("windows"):
        return (
            f"已启动（pid={pid}），但驱动回执未带窗口：启动尚未稳定（驱动文档明确这是正常瞬时状态）。"
            "稍后用 winuse_state（pid）复查窗口，或直接对该 pid 下元素动作"
        )
    return None


def run_app(
    driver,
    *,
    action: str,
    pid: Any = None,
    path: Any = None,
    args: Any = None,
    confirm: bool = False,
    execute: bool = False,
    options: Any = None,
    audit=None,
) -> dict:
    """应用生命周期动作（launch / list / kill），**默认干跑**。

    - `action`：`launch`（启动，不抢焦点）/ `list`（枚举：运行中 + 已安装）/ `kill`（按 pid 强杀）
    - `launch`：`path` + 可选 `args` → 驱动 `launch_app`；回执带驱动给的 `pid` 与窗口信息
    - `list`：`options` 里的 `running` / `kind` / `query` / `limit` 做筛选与截断
    - `kill`：**危险动作**——没有 `confirm=True` 一律返回 `CONFIRM_REQUIRED` 且不下发；
      驱动是强杀（未保存内容会丢），能走 `winuse_window --action close` 就别用强杀
    - 参数/驱动错误不抛给上层：统一收口成 `{"ok": false, "code": ...}`（门面给 agent 用，
      `code` 比异常更好分支）；审计每次尝试都记一条（blocked / dry_run / executed / failed）

    `confirm` 由门面透传（CLI `--confirm` / MCP `confirm=true`）。`launch` 若命中
    `guard.SYSTEM_APPS`（系统外壳 / 锁屏 / 同意界面…）同样要走确认门。
    """
    name = str(action or "").strip().lower()
    target = _receipt_target(pid, path, options)

    # ① 安全门（在下发**之前**；顺序与 core.act 一致：先判危险，再校验参数）
    verdict = assess_app_action(name, _identifier(path, options))
    if verdict.dangerous and not confirm:
        written = _write_app_audit(
            audit,
            event="app.blocked",
            action=name,
            ok=False,
            code=verdict.code,
            target=target,
            detail={"danger": verdict.to_dict(), "confirmed": False},
        )
        return _blocked(name, verdict, execute, target, written)

    # ② 参数校验 + 构造驱动调用
    try:
        tool, call_args = build_app_call(name, pid=pid, path=path, args=args, options=options)
    except WinUseError as exc:
        return _failure(name, exc, execute, target)

    out: dict[str, Any] = {
        "ok": True,
        "dry_run": not execute,
        "action": name,
        "tool": tool,
        "target": target,
        "code": None,
    }
    if name == "list":
        out["read_only"] = True
    if verdict.dangerous:
        out["danger"] = verdict.to_dict()
    arg_count = len(call_args.get("additional_arguments") or [])

    if not execute:
        out["would_call"] = {
            "tool": tool,
            "args": call_args,
            "note": READ_ONLY_NOTE if name == "list" else (LAUNCH_NOTE if name == "launch" else DRY_RUN_NOTE),
        }
        written = _write_app_audit(
            audit,
            event="app.dry_run",
            action=name,
            ok=True,
            code=None,
            target=target,
            detail={"confirmed": bool(confirm), "arg_count": arg_count, "read_only": name == "list"},
        )
        return _mark(out, written)

    # ③ 真下发
    try:
        response = driver.call(tool, call_args)
    except WinUseError as exc:
        _write_app_audit(
            audit,
            event="app.failed",
            action=name,
            ok=False,
            code=exc.code,
            target=target,
            detail={"confirmed": bool(confirm), "arg_count": arg_count},
        )
        return _failure(name, exc, execute, target)

    refusal = _driver_refusal(response)
    if refusal is not None:
        out["ok"] = False
        out["code"] = refusal["code"]
        out["error"] = refusal
        written = _write_app_audit(
            audit,
            event="app.executed",
            action=name,
            ok=False,
            code=refusal["code"],
            target=target,
            detail={"confirmed": bool(confirm), "arg_count": arg_count},
        )
        return _mark(out, written)

    detail: dict[str, Any] = {"confirmed": bool(confirm), "arg_count": arg_count}
    if name == "list":
        out.update(trim_apps(response, options))
        detail["returned"] = out["returned"]
        detail["apps_total"] = out["apps_total"]
    elif name == "launch":
        out["result"] = _slim_launch_result(response)
        note = _launch_note(out["result"])
        if note:
            out["note"] = note
        detail["pid"] = out["result"]["pid"]
    else:
        # kill：回执形态未实测 → 原样透传（不猜字段名、不静默丢字段）
        out["result"] = response
        out["note"] = KILL_NOTE

    written = _write_app_audit(
        audit,
        event="app.executed",
        action=name,
        ok=True,
        code=None,
        target=target,
        detail=detail,
    )
    return _mark(out, written)
