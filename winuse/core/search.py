"""winuse_search：Everything 全盘文件名搜索（零第三方依赖，**只读**）。

为什么要有它：`winuse_state` 只能看"当前窗口里有什么"，`winuse_system` / `winuse_registry`
只能看系统事实；而"那份资料/那个工程在哪个盘的哪个目录"这类问题，Windows 上唯一
飞快且覆盖全盘的办法是 Everything 的索引——本模块调它的命令行工具 `es.exe`。

四条铁律：

1. **不拼 shell 字符串**：一律 argv 列表交给 `subprocess.run`。搜索词里的 `&`、`|`、
   `>`、`;`、引号都只是普通字符（真机实测 `es.exe` 自己也把每个 argv 词当一个条件），
   既没有注入面，也不需要"引号转义地狱"。
2. **缺工具 ≠ 搜索无结果**：找不到 `es.exe` 返回 `ok:false` + `ES_NOT_FOUND`（并把
   尝试过的候选路径列出来），绝不返回 `count:0` 让上层以为"机器上没有这个文件"。
   同理：Everything 没在跑 → `ES_RUN_FAILED`，超时 → `SEARCH_TIMEOUT`，输出不可解析 →
   `ES_OUTPUT_INVALID`，各是各的码。
3. **只读**：搜索不改变任何桌面/系统状态，因此**不需要 confirm、也不落审计**
   （项目审计约定：只记动作类事件 `act.*` / `window.*` / `batch.*` / `notify.executed`；
   给只读搜索写审计只会让日志里多出一堆看不出所以然的"事件"）。回执里显式给出
   `read_only: true` / `requires_confirm: false`，门面据此直接放行。
4. **超时必须变成错误码**：`subprocess.run(timeout=...)` 超时会**杀掉子进程**再抛
   `TimeoutExpired`，所以不存在"进程挂住不返回"的形态；本模块把它翻成 `SEARCH_TIMEOUT`。

真机实测（Everything 1.4.1.1032 / es.exe 1.1.0.30，Windows 11，本机 ANSI 代码页 cp936）
——下面这些结论决定了参数怎么拼，**改动前请先复测**：

| 事实 | 实测命令与结果 |
|---|---|
| 列输出 | `es.exe -n 3 -csv -no-header -size -size-format 1 -date-modified -date-format 1 -path "D:\\work\\proj\\docs" "*.md"`<br>→ 每行 `55494,2026-09-27T15:58:51,"D:\\work\\proj\\docs\\requirements\\notes.md"`（路径列总带引号；目录行的 Size 列为空） |
| 关闭表头 | `-no-header` 生效（不加时首行是 `Size,Date Modified,Filename`） |
| 限条数 | `-n <num>`；结果数少于 num 时全给（`-n 99` 对 5 个匹配返回 5 行） |
| 搜索词语义 | **每个 argv 词是一个 AND 条件**；含空格的一个 argv 被当整串：`es ... <dir> "*.md size:>1kb"` → 0 条；拆成 `"*.md"` `"size:>1kb"` 两个词 → 4 条。所以 `query` 按空白拆词（引号包住的一组当短语） |
| 大小过滤 | `size:>=1024` / `size:<=20000` / `size:1000..20000`（**字节**），作为独立搜索词跟在后面；`size:>=1024` 4 条、`size:<=20000` 3 条、`size:1000..20000` 2 条（取交集，自洽） |
| 正则 | `-r <regex>`，**后面必须紧跟正则**：`-r <过滤词> <正则>` 返回 0 条；`-r <正则> <过滤词>` 正常。默认匹配**文件名**（加 `-p` 才匹配全路径） |
| 排序 | `-sort size-descending` / `-sort date-modified-descending` 有效；名字非法时 es 把整页 help 打到 **stdout** 并**退出码 4**（不是静默忽略，所以本模块先做白名单校验） |
| 输出编码 | stdout 是**系统 ANSI 代码页**（本机 cp936）字节，不是 UTF-8（中文路径 `\xd6\xd0...` 按 UTF-8 解会直接抛）——见 `decode_output` |
| 故障形态 | Everything 未运行 → 退出码 8 + stderr `Error 8: Everything IPC window not found. Please make sure Everything is running.` |
| 无查询 | 只给 `-path` → 列出该目录条目（合法用法）；**什么都不给** → 把整盘结果倒出来（所以本模块要求 query/regex/path 至少给一个） |

已知边界（有意为之，不是遗漏）：

- 结果里的**完整路径是刻意保留**的（换成占位符就没法用了）；它只回给调用方，
  不落审计、不落盘（所以也不走 `audit.redact_text` 的本机路径替换）。
- 不请求"精确总数"（没有多余的 `-get-result-count` 往返）：多要一条即可判定截断，
  回执里 `count` = **本次实际返回条数**，`truncated=true` 表示还有更多。
- `MAX_SEARCH_RESULTS` 这个资源上限按项目惯例应当放进 `winuse/limit.py`（R-21 家族，
  与 `MAX_REGISTRY_SUBKEYS` 并列）；本期改动范围限定在 `core/search.py`，故先定义在本模块，
  待门面接线时一并上移。

用法（核心层，门面只做参数翻译）：

    from winuse.core.search import run_search
    run_search(query="*.md size:>10kb", path=r"D:\\Projects", max_results=20)
"""

from __future__ import annotations

import csv
import ntpath
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .. import config as config_mod
from ..driver.errors import InvalidParams, MissingParam, WinUseError

__all__ = [
    "DEFAULT_ES_PATH",
    "ENV_ES_PATH",
    "ES_EXE_NAME",
    "EsNotFound",
    "EsOutputInvalid",
    "EsRunFailed",
    "MAX_SEARCH_RESULTS",
    "SEARCH_NOTE",
    "SearchPathNotFound",
    "SearchTimeout",
    "ansi_encoding",
    "build_search_command",
    "coerce_size",
    "coerce_timeout",
    "command_preview",
    "configured_es_path",
    "decode_output",
    "normalize_sort",
    "parse_search_output",
    "resolve_es",
    "run_search",
    "size_filter_terms",
    "split_query_terms",
]

#: es.exe 的文件名（PATH 查找用；默认安装位置见 `config.DEFAULT_ES_PATH`）
ES_EXE_NAME = "es.exe"

#: 配置项与默认安装路径的**唯一出处**在 `config`（键名漂移是最难查的一类问题）
ENV_ES_PATH = config_mod.ENV_ES_PATH
DEFAULT_ES_PATH = config_mod.DEFAULT_ES_PATH

#: 单次搜索最多返回多少条（超过就夹紧并显式标 `clamped`；见模块 docstring 的边界说明）
MAX_SEARCH_RESULTS = 200

#: 默认超时（秒）。Everything 是索引查询，正常在百毫秒级；15s 留足"索引正在重建"的余量
DEFAULT_TIMEOUT_SECONDS = 15.0

#: Everything 的 `-sort` 白名单（取自 `es.exe -help`；给错名字 es 会退 4 并打整页 help）
SORT_NAMES: tuple[str, ...] = (
    "name",
    "path",
    "size",
    "extension",
    "date-created",
    "date-modified",
    "date-accessed",
    "attributes",
    "run-count",
    "date-recently-changed",
    "date-run",
)
SORT_DIRECTIONS: tuple[str, ...] = ("ascending", "descending")

#: 回执里的口径声明（避免上层把"0 条"当成"工具没装"或反过来）
SEARCH_NOTE = (
    "只读搜索（Everything 索引）：不改变任何桌面/系统状态，故不需要 confirm、不落审计；"
    "count 是本次**实际返回条数**（truncated=true 表示还有更多匹配）；"
    "结果为 es.exe 原始路径（完整路径，不做脱敏）；mtime 为 es 给出的 ISO-8601 本地时间。"
)

#: CSV 表头识别（`-no-header` 万一失效时的兜底：表头绝不能当结果）
_HEADER_SIZE_CELLS = frozenset({"size"})
_HEADER_PATH_CELLS = frozenset({"filename", "name", "path", "full path", "full-path-and-name"})

#: 可注入点（单测用假实现替换，绝不真跑全盘搜索）
Which = Callable[[str], "str | None"]
Exists = Callable[[str], bool]


# --------------------------------------------------------------------------
# 错误码
# --------------------------------------------------------------------------


class EsNotFound(WinUseError):
    """找不到 es.exe——**缺工具，不是搜索无结果**。

    这类故障是可恢复的环境问题（同一份代码换台机器就能跑），所以走返回值
    `ok:false` + 本码，并带上"哪几处找过了"，而不是抛异常。
    """

    code = "ES_NOT_FOUND"


class EsRunFailed(WinUseError):
    """es.exe 起不来或退出码非零（含 Everything 没在运行时的 `Error 8`）。"""

    code = "ES_RUN_FAILED"


class SearchTimeout(WinUseError):
    """搜索超时（进程已被杀掉）。"""

    code = "SEARCH_TIMEOUT"


class EsOutputInvalid(WinUseError):
    """es.exe 有输出但一行都解析不出来——宁可报错也不静默返回空列表。"""

    code = "ES_OUTPUT_INVALID"


class SearchPathNotFound(WinUseError):
    """搜索范围（path）不是已存在的目录——调用方错误，直接抛。

    为什么不静默当"0 条"：es 对不存在的 `-path` 返回**空**且退出码 0，
    与"这个目录里确实没有匹配"完全同形，上层会据此得出错误结论。
    """

    code = "SEARCH_PATH_NOT_FOUND"


# --------------------------------------------------------------------------
# 纯函数：输出解码
# --------------------------------------------------------------------------


def ansi_encoding() -> str:
    """本机 ANSI 代码页的 Python 编解码器名（`es.exe` 的 stdout 就是它编的）。

    刻意不用 `locale.getpreferredencoding()`：本机实测它返回 `utf-8`（进程被设了
    UTF-8 模式），和 es.exe 实际用的 cp936 不是一回事。`GetACP()` 才是那个事实。
    """
    if os.name == "nt":
        try:
            import ctypes

            return f"cp{int(ctypes.windll.kernel32.GetACP())}"
        except Exception:  # noqa: BLE001 - 拿不到就退到 "mbcs"（Windows 上的 ANSI 别名）
            return "mbcs"
    return "utf-8"


def decode_output(raw: bytes | None) -> str:
    """stdout 字节 → 文本。顺序：UTF-8（含 BOM）→ 本机 ANSI → 带替换的 UTF-8。

    为什么 UTF-8 优先：纯 ASCII 时两者等价；而真机上的中文路径字节（cp936）**过不了**
    UTF-8 严格解码（`\\xd6\\xd0` 直接抛），自然落到 ANSI 分支。反过来（ANSI 优先）
    在英文机器上会把将来可能出现的 UTF-8 输出静默解成乱码——那是最难发现的一类错误。
    与 `core/system.decode_output` 同一套口径（那里写死 gbk，这里用本机 ACP 更准）。
    """
    if not raw:
        return ""
    for encoding in ("utf-8-sig", "utf-8", ansi_encoding()):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 纯函数：搜索词与参数 → argv
# --------------------------------------------------------------------------


def split_query_terms(query: str | None) -> list[str]:
    """查询串 → 搜索词列表（空白分隔；`"..."` 包住的一组算**一个短语词**）。

    为什么不是整串当一个词：真机实测 `es.exe -path <dir> "*.md size:>1kb"` 返回 0 条，
    拆成 `*.md` + `size:>1kb` 两个 argv 词返回 4 条——es.exe 自己的命令行解析就是
    "空白分隔 + 引号成组"，这里保持同一套语义，免得出现"我明明写了条件却 0 结果"。
    引号必须**剥掉**：真机实测把带引号的串原样当一个 argv 传下去同样返回 0 条。
    """
    terms: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in str(query or ""):
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in "\"'":
            quote = char
            continue
        if char.isspace():
            if current:
                terms.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        terms.append("".join(current))
    return terms


def coerce_size(value: Any, field: str) -> int | None:
    """大小参数归一成**字节**整数；非法一律抛 `InvalidParams`（不猜单位、不猜量级）。

    刻意不收 `"1kb"` 这类带单位的字符串：Everything 的 `size:` 同时接受字节与单位写法，
    猜错单位就是 1024 倍的误差，而"看起来跑了其实筛错了"最难发现。单位请自己换算。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，`min_size=True` 必须被拒
        raise InvalidParams(f"{field} 必须是字节数", f"收到：{value!r}")
    if isinstance(value, int):
        out = value
    elif isinstance(value, float):
        if value != int(value):
            raise InvalidParams(f"{field} 必须是整数字节", f"收到：{value!r}")
        out = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            out = int(text)
        except ValueError as exc:
            raise InvalidParams(
                f"{field} 必须是整数字节数（不接受带单位的写法）", f"收到：{value!r}"
            ) from exc
    else:
        raise InvalidParams(f"{field} 类型不支持", f"收到类型：{type(value).__name__}")
    if out < 0:
        raise InvalidParams(f"{field} 不能为负数", f"收到：{out}")
    return out


def size_filter_terms(min_size: Any = None, max_size: Any = None) -> list[str]:
    """大小过滤 → Everything 的 `size:` 搜索词（单位一律字节）。

    三种形态（真机实测都有效）：只给下限 `size:>=1024`、只给上限 `size:<=2048`、
    两头都给 `size:1000..20000`（区间，含两端）。
    """
    low = coerce_size(min_size, "min_size")
    high = coerce_size(max_size, "max_size")
    if low is not None and high is not None and low > high:
        raise InvalidParams(
            "min_size 不能大于 max_size", f"收到：min_size={low} > max_size={high}"
        )
    if low is not None and high is not None:
        return [f"size:{low}..{high}"]
    if low is not None:
        return [f"size:>={low}"]
    if high is not None:
        return [f"size:<={high}"]
    return []


def normalize_sort(sort: Any) -> str | None:
    """排序参数归一化并**白名单校验**（None/空 = 不排序）。

    为什么非要校验：真机实测 `-sort bogus-name` 不会"忽略该选项"，而是把整页 help
    打到 stdout 并退出码 4——那时 stdout 里全是 help 文本，靠"有没有结果"根本无法分辨。
    所以错名字在这里就挡住，报错比事后猜输出干净得多。
    """
    text = str(sort or "").strip().lower()
    if not text:
        return None
    base, _, direction = text.rpartition("-")
    if base and direction in SORT_DIRECTIONS:
        name = base
    else:
        name, direction = text, ""
    if name not in SORT_NAMES:
        raise InvalidParams(
            f"不认识的排序名：{sort}",
            "可用：" + "、".join(SORT_NAMES) + ";可加后缀 -ascending / -descending",
        )
    return f"{name}-{direction}" if direction else name


def build_search_command(
    es_path: str,
    *,
    query: str | None = None,
    regex: str | None = None,
    path: str | None = None,
    limit: Any = 50,
    sort: str | None = None,
    min_size: Any = None,
    max_size: Any = None,
) -> list[str]:
    """纯函数：参数 → 下发给 `subprocess` 的 argv（**永不拼 shell 字符串**）。

    参数顺序有讲究（全部真机实测）：

    1. `-n <limit>` 与列选项 `-csv -no-header -size -size-format 1 -date-modified
       -date-format 1`：固定 3 列 = `字节数,ISO时间,完整路径`，行数受限；
    2. `-sort <名字>`；
    3. `-r <正则>`：**正则必须紧跟 `-r`**（反例见模块 docstring 的实测表）；
    4. 搜索词：`query` 拆出的词 + `size:` 过滤词（每个词都是独立 argv 项 = AND 条件）；
    5. `-path <目录>`：范围限定，放最后（与词序无关，只是让预览好读）。

    `path` 是否真实存在**不在这里校验**（本函数是纯函数，不碰文件系统），
    由 `run_search` 负责。
    """
    text = str(es_path or "").strip()
    if not text:
        raise MissingParam("缺少 es.exe 路径", "先经 `resolve_es` 定位，再构造命令")

    if limit is None or isinstance(limit, bool):
        raise InvalidParams("limit 必须是正整数", f"收到：{limit!r}")
    try:
        count = int(limit)
    except (TypeError, ValueError) as exc:
        raise InvalidParams("limit 必须是正整数", f"收到：{limit!r}") from exc
    if count < 1:
        raise InvalidParams("limit 必须 ≥ 1", f"收到：{count}")

    pattern = str(regex or "").strip()
    terms = split_query_terms(query) + size_filter_terms(min_size, max_size)
    scope = str(path or "").strip()

    if not terms and not pattern and not scope:
        raise MissingParam(
            "至少要给 query / regex / path 之一",
            "三者全空时 Everything 会把整盘结果倒出来（真机实测：随机几条来自 F:/E: 盘），"
            "既不可读也没意义，所以这里直接拒",
        )

    argv = [
        text,
        "-n",
        str(count),
        "-csv",
        "-no-header",
        "-size",
        "-size-format",
        "1",
        "-date-modified",
        "-date-format",
        "1",
    ]
    normalized_sort = normalize_sort(sort)
    if normalized_sort:
        argv += ["-sort", normalized_sort]
    if pattern:
        argv += ["-r", pattern]
    argv += terms
    if scope:
        argv += ["-path", scope]
    return argv


#: 命令预览里必须加引号的字符：空白 + **cmd 的元字符**。
#: 为什么连 `>` / `^` / `&` 都要加：搜索词里天然会带它们（`size:>10kb`、正则里的 `^`），
#: 而预览是要能"原样粘进 cmd 复现"的——`size:>10kb` 不加引号粘过去会被 cmd 当成重定向。
_PREVIEW_SPECIALS = frozenset(' \t"&|<>^%()')


def command_preview(argv: list[str]) -> str:
    """纯函数：argv → 一行可读预览（含空白或 cmd 元字符的词加双引号，可原样粘进 cmd 复现）。

    刻意**不做路径脱敏**：搜索结果本身就要带完整路径，预览与结果同族；而且本模块
    不落审计，预览不构成新的泄露面（与 `core/notify.command_preview` 的取舍不同——
    那里要进审计日志，所以把本机路径换成了 `<PATH>`）。
    """
    parts: list[str] = []
    for part in argv:
        text = str(part)
        has_special = not text or any(char in _PREVIEW_SPECIALS for char in text)
        parts.append(f'"{text}"' if has_special else text)
    return " ".join(parts)


# --------------------------------------------------------------------------
# 纯函数：es.exe 输出 → 结果条目
# --------------------------------------------------------------------------


def _is_header_row(row: list[str]) -> bool:
    """首行是不是 CSV 表头（`Size,...,Filename`）——表头绝不能被当结果。"""
    if len(row) < 2:
        return False
    return row[0].strip().lower() in _HEADER_SIZE_CELLS and row[-1].strip().lower() in _HEADER_PATH_CELLS


def _as_int(value: Any) -> int | None:
    """尽力取整数；取不到给 None——**不猜 0**（0 会被读成"空文件"）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _entry(path: str, size_text: str | None, mtime_text: str | None) -> dict:
    """一行 → 对外条目。`name` 用 `ntpath`（本模块处理的是 Windows 路径）。"""
    return {
        "path": path,
        "name": ntpath.basename(path.replace("/", "\\")),
        "size": _as_int(size_text),
        "mtime": (mtime_text or "").strip() or None,
    }


def parse_search_output(raw: str) -> list[dict]:
    """纯函数：es.exe 的 stdout → 结果条目列表。

    认两种形态（都是真机实测的）：

    - 三列 CSV（默认口径）：`字节数,ISO时间,完整路径`
    - 每行一个路径（不带 `-size/-date-modified` 时，或调用方自己拼的命令）

    三个兜底，都是为了"别把噪音当结果、别把结果读丢"：

    1. **表头行**：`-no-header` 万一失效，首行 `Size,...,Filename` 要认出来并跳过；
    2. **列数多于三**：路径里有逗号且 es 没给引号时（正常情况它会加引号并被 csv 正确
       还原），多出来的列重新用 `,` 拼回路径，而不是截断；
    3. **空路径行**：丢弃（没有路径的条目对调用方毫无用处）。

    不校验 mtime 格式：`-date-format 1` 给什么就原样带什么（改口径可以，别猜）。
    """
    rows: list[dict] = []
    lines = [line for line in str(raw or "").splitlines() if line.strip()]
    for index, row in enumerate(csv.reader(lines)):
        if not row or all(cell.strip() == "" for cell in row):
            continue
        if index == 0 and _is_header_row(row):
            continue
        if len(row) == 1:
            path, size_text, mtime_text = row[0].strip(), None, None
        elif len(row) >= 3:
            path = ",".join(row[2:]).strip()
            size_text, mtime_text = row[0], row[1]
        else:  # 两列：认不出是哪两列，只信最后一个是路径
            path, size_text, mtime_text = row[-1].strip(), None, None
        if not path:
            continue
        rows.append(_entry(path, size_text, mtime_text))
    return rows


# --------------------------------------------------------------------------
# es.exe 定位：显式参数 → 配置 → 默认安装路径 → PATH
# --------------------------------------------------------------------------


def configured_es_path(env_path: str | Path | None = None) -> tuple[str | None, str | None]:
    """从**项目内 `.env`** 读 `WINUSE_ES_PATH`（键名唯一出处：`config.ENV_ES_PATH`）。

    返回 `(值, 失败原因)`：`.env` 坏了不该让搜索整体崩——原因随错误详情带出去，
    定位继续走"默认安装路径 → PATH"两级兜底（搜索本身照常能用）。
    """
    try:
        cfg = config_mod.Config.load(env_path=env_path)
    except Exception as exc:  # noqa: BLE001 - 配置错不该毁掉一次只读搜索
        return None, f"{type(exc).__name__}: {exc}"
    value = str(getattr(cfg, "es_path", "") or "").strip()
    return (value or None), None


def resolve_es(
    *,
    es_path: str | None = None,
    configured: str | None = None,
    default_path: str = DEFAULT_ES_PATH,
    which: Which | None = None,
    exists: Exists | None = None,
) -> tuple[str | None, list[dict]]:
    """按优先级定位 `es.exe`：**显式参数 → 配置 → 默认安装路径 → PATH**。

    返回 `(找到的路径或 None, 候选清单)`；候选清单逐项带 `source` / `path` / `exists`，
    找不到时直接拼进错误详情——"哪几处找过了"是排障时唯一有用的信息。

    显式给了 `es_path` 却不存在时**不回退**：悄悄换用另一个 es.exe 会让结果无法归因
    （谁知道那次搜索是哪份 Everything 索引答的）。这条与"配置值为空就继续往下找"
    并不矛盾：配置项表达的是"建议",显式参数表达的是"就用它"。
    """
    probe_which = which or shutil.which
    probe_exists = exists or os.path.isfile
    candidates: list[dict] = []

    def consider(source: str, candidate: str | None) -> str | None:
        text = str(candidate or "").strip()
        ok = bool(text) and bool(probe_exists(text))
        candidates.append({"source": source, "path": text or None, "exists": ok})
        return text if ok else None

    explicit_text = str(es_path or "").strip()
    if explicit_text:
        # 显式参数是权威：找不到就是找不到，不偷偷换人（结果必须可归因）
        return consider("param", explicit_text), candidates

    found = consider("config", configured)
    if found is None:
        found = consider("default", default_path)
    if found is None:
        found = consider("path", probe_which(ES_EXE_NAME) if probe_which else None)
    return found, candidates


# --------------------------------------------------------------------------
# 真跑
# --------------------------------------------------------------------------


def coerce_timeout(value: Any) -> float:
    """超时秒数归一（>0 的有限数）；非法抛 `InvalidParams`。"""
    if value is None or isinstance(value, bool):
        raise InvalidParams("timeout_seconds 必须是正数（秒）", f"收到：{value!r}")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParams("timeout_seconds 必须是正数（秒）", f"收到：{value!r}") from exc
    if seconds <= 0 or seconds != seconds or seconds in (float("inf"),):
        raise InvalidParams("timeout_seconds 必须是正数（秒）", f"收到：{value!r}")
    return seconds


def clamp_max_results(value: Any) -> tuple[int, bool]:
    """`max_results` 归一：取下限 1（下界非法直接拒），**上界夹紧**并回报是否夹过。

    与注册表递归参数同一取舍（R-21）：上限是资源保护，夹紧比拒绝好用；而 0 / 负数
    不是"要少一点"，是调用方写错了，报错比猜清楚。
    """
    if value is None or isinstance(value, bool):
        raise InvalidParams("max_results 必须是正整数", f"收到：{value!r}")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParams("max_results 必须是正整数", f"收到：{value!r}") from exc
    if count < 1:
        raise InvalidParams("max_results 必须 ≥ 1", f"收到：{count}")
    if count > MAX_SEARCH_RESULTS:
        return MAX_SEARCH_RESULTS, True
    return count, False


def _run_es(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """真起 es.exe（argv 列表，无 shell，无 stdin）。

    `subprocess.run(timeout=...)` 超时会**先杀掉子进程**再抛 `TimeoutExpired`，
    因此不会留下挂住的后台进程；`CREATE_NO_WINDOW` 是为了别每搜一次闪一个黑窗
    （es.exe 是控制台程序，项目里有专门的"窗口闪烁"前车之鉴）。
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(  # noqa: S603 - argv 由本模块构造，无 shell
        argv,
        capture_output=True,
        timeout=timeout,
        creationflags=creationflags,
    )
    return int(proc.returncode), proc.stdout or b"", proc.stderr or b""


def _failure(code: str, message: str, detail: str = "", command: str | None = None) -> dict:
    """失败回执：与成功回执**同族**（ok/count/results/truncated 恒在），错误另有 error 块。"""
    out: dict[str, Any] = {
        "ok": False,
        "count": 0,
        "results": [],
        "truncated": False,
        "error": {"code": code, "message": message},
    }
    if detail:
        out["error"]["detail"] = detail
    if command:
        out["command"] = command
    out["read_only"] = True
    out["requires_confirm"] = False
    out["note"] = SEARCH_NOTE
    return out


def validate_search_request(query: Any, regex: Any, path: Any) -> None:
    """调用方错误直接抛（编程错误应当被看见，不回收成 ok:false）。"""
    if not (split_query_terms(query) or str(regex or "").strip() or str(path or "").strip()):
        raise MissingParam(
            "至少要给 query / regex / path 之一",
            "三者全空时 Everything 会把整盘结果倒出来，故拒绝；例如：query='*.md' 或 path='D:\\\\Projects'",
        )
    scope = str(path or "").strip()
    if scope and not os.path.isdir(scope):
        raise SearchPathNotFound(
            f"搜索范围不是已存在的目录：{scope}",
            "es 的 -path 只接受目录；目录不存在时它返回空且退出码 0，"
            "与'目录里没有匹配'同形，故这里先挡住",
        )


def run_search(
    *,
    query: str | None = None,
    regex: str | None = None,
    path: str | None = None,
    max_results: Any = 50,
    sort: str | None = None,
    min_size: Any = None,
    max_size: Any = None,
    timeout_seconds: Any = DEFAULT_TIMEOUT_SECONDS,
    es_path: str | None = None,
) -> dict:
    """用 Everything（`es.exe`）搜本机文件——**只读**，不需要 confirm。

    返回 `{ok, count, results: [{path, name, size, mtime}], truncated, command?, error?}`
    （成功时另有 `es_path` / `sort` / `max_results` / `read_only` / `requires_confirm` /
    `note`，被夹紧时另有 `clamped`）。

    失败分流（**缺工具 ≠ 无结果**）：

    | 情况 | 结果 |
    |---|---|
    | 找不到 es.exe（参数/配置/默认路径/PATH 都没有） | `ok:false` + `ES_NOT_FOUND`（详情列出候选） |
    | Everything 没在运行 / es 退出码非零 | `ok:false` + `ES_RUN_FAILED` |
    | 超时（进程已被杀） | `ok:false` + `SEARCH_TIMEOUT` |
    | 输出一行都解析不出来 | `ok:false` + `ES_OUTPUT_INVALID` |
    | 搜索跑了但确实没有匹配 | **`ok:true` + `count:0`** |

    调用方参数错误（`query/regex/path` 全空、path 不是目录、`max_results`/`sort`/
    `min_size`/`max_size`/`timeout_seconds` 非法）**直接抛** `WinUseError` 子类——
    编程错误不该被吞成 `ok:false`，否则"我传错了参数"和"环境有问题"分不开。

    截断判定：内部向 es 要 `max_results + 1` 条，多出来的那条只用来判定"还有更多"，
    永远不会出现在 `results` 里。
    """
    # 1) 参数与范围（顺序刻意靠前：不合法就别去碰工具，报错更干净）
    validate_search_request(query, regex, path)
    effective_max, clamped = clamp_max_results(max_results)
    timeout = coerce_timeout(timeout_seconds)
    normalized_sort = normalize_sort(sort)  # 提前校验：错名字会在 es 那边退 4 并打整页 help

    # 2) 定位 es.exe
    scope = str(path or "").strip()
    configured, config_error = configured_es_path()
    found, candidates = resolve_es(
        es_path=es_path,
        configured=configured,
        default_path=DEFAULT_ES_PATH,
        which=shutil.which,
        exists=os.path.isfile,
    )
    if not found:
        tried = "；".join(
            f"{item['source']}={item['path'] or '(未配置)'}" for item in candidates if item["source"] != "path"
        )
        path_candidate = next((item for item in candidates if item["source"] == "path"), None)
        if path_candidate is not None:
            tried += f"；path={path_candidate['path'] or '(PATH 上没有 es.exe)'}"
        detail = f"已尝试：{tried}。装 Everything 后重试，或用 WINUSE_ES_PATH / es_path 显式指定。"
        if config_error:
            detail += f"（配置读取失败：{config_error}）"
        return _failure(
            EsNotFound.code,
            "找不到 es.exe（Everything 命令行工具）——这是缺工具，不是搜索无结果",
            detail,
        )

    # 3) 构造并真跑（多要一条用于判定截断）
    argv = build_search_command(
        found,
        query=query,
        regex=regex,
        path=scope or None,
        limit=effective_max + 1,
        sort=normalized_sort,
        min_size=min_size,
        max_size=max_size,
    )
    preview = command_preview(argv)

    try:
        returncode, stdout, stderr = _run_es(argv, timeout)
    except subprocess.TimeoutExpired:
        return _failure(
            SearchTimeout.code,
            f"es.exe 调用超时（>{timeout:g}s），进程已终止",
            "Everything 索引正在重建或磁盘繁忙时可调大 timeout_seconds；本模块不会留下挂住的进程",
            preview,
        )
    except FileNotFoundError as exc:
        return _failure(EsRunFailed.code, f"无法启动 es.exe：{found}", str(exc)[:200], preview)
    except OSError as exc:
        return _failure(EsRunFailed.code, f"启动 es.exe 失败（{type(exc).__name__}）", str(exc)[:200], preview)

    if returncode != 0:
        # 真机实测：Everything 没在跑时 stderr 是 `Error 8: Everything IPC window not found...`
        hint = "（Everything 未运行？）" if returncode == 8 else ""
        return _failure(
            EsRunFailed.code,
            f"es.exe 退出码 {returncode}{hint}",
            decode_output(stderr).strip()[-200:] or decode_output(stdout).strip()[-200:],
            preview,
        )

    stdout_text = decode_output(stdout)
    parsed = parse_search_output(stdout_text)
    truncated = len(parsed) > effective_max
    results = parsed[:effective_max]

    out: dict[str, Any] = {
        "ok": True,
        "count": len(results),
        "results": results,
        "truncated": truncated,
        "query": str(query).strip() or None if query is not None else None,
        "regex": str(regex).strip() or None if regex is not None else None,
        "path": scope or None,
        "sort": normalized_sort,
        "min_size": coerce_size(min_size, "min_size"),
        "max_size": coerce_size(max_size, "max_size"),
        "max_results": effective_max,
        "es_path": found,
        "command": preview,
        "read_only": True,
        "requires_confirm": False,
        "note": SEARCH_NOTE,
    }
    if clamped:
        out["clamped"] = {"max_results": effective_max}
    if truncated:
        # 裁剪必须留痕：让上层知道"这不是全部"，而不是以为机器上就这么多
        out["truncate_hint"] = (
            f"只返回前 {effective_max} 条（还有更多匹配）；可调大 max_results，"
            "或用 path / min_size / max_size / sort 收窄"
        )
    return out
