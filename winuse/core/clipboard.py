"""winuse_clipboard：剪贴板出口（读 / 写，零第三方依赖）。

为什么要有它：剪贴板是桌面 agent 最顺手的"搬运通道"（把结果交给用户、把用户刚复制的东西
拿过来）。驱动已经给了 `clipboard_read` / `clipboard_write`，但剪贴板是**双向隐私面**：

- **读**：里面可能是刚复制的口令、验证码、密钥。驱动描述里明写 plain text 属隐私敏感内容、
  *is never retained in telemetry*，`include_text` 默认 false。所以本门面默认**只列类型**
  （`CF_UNICODETEXT` 这类格式名，不含内容），要正文必须 `include_text=True` **且确认**。
- **写**：一次调用会**替换**剪贴板里的当前内容（可能正是用户还没粘贴的东西），且**不可撤销**
  （Windows 没有"恢复上一个剪贴板"的公开接口）。所以写是危险动作：`confirm=True` 是权限门，
  `execute=True` 才是真下发（默认干跑，回 `would_call`）。

四条口径：

1. **三层顺序固定**：参数与路径校验 → 确认门 → 干跑 / 下发。任何情况下都不会"先碰驱动再报错"。
   与 `core.menu` 的"闸门在参数校验之前"**刻意不同**：菜单的路径校验只关乎下发内容的形状，
   而剪贴板的"三选一"关乎**意图本身**——值都没定，给用户的"确认请求"就是一句空话；
   文件存在性又被明确要求在下发前拒（驱动对不存在的路径只会给一句与原因无关的报错）。
2. **回执与审计分离**：文本正文只回给**调用方**（回执里的 `text`），**绝不进审计**；审计只记
   "哪个方向、什么类型、多长"。写路径同理：干跑的 `would_call` 要给**确切**调用（确认者必须
   看见将要写进去的是什么），但**真下发**的回执不回显正文（回执会进会话/日志，要看计划就先干跑）。
3. **没读过就不给字段**：只列类型时回执里**没有** `text` / `text_length`——避免把"没读过"
   误读成"剪贴板是空的"（同 `core.search` 的「缺工具 ≠ 无结果」口径）。
4. **失败不抛**：驱动侧失败 / 拒绝一律回收成 `ok:false` + 稳定 `code`；只有**调用方参数错误**
   才抛（编程错误要被看见，与 `core.registry` / `core.search` 同口径）。

驱动事实（0.28.2 实测，`describe clipboard_read` / `describe clipboard_write`）：

- `clipboard_read` 回 `{supported, privacy_sensitive, content_redacted_from_telemetry, types, text}`；
  `include_text=false` 时 `text` 为 null。
- `clipboard_write` 的参数是 `text` / `image_path` / `file_path`，描述明写 *"exactly one value"*
  （文本 / 绝对路径的图片 / 绝对路径的文件 URL），回执给出可读回的 `types`。

用法（核心层，门面只做参数翻译）：

    from winuse.core.clipboard import run_clipboard
    run_clipboard(driver, action="read")                                    # 只列类型
    run_clipboard(driver, action="read", include_text=True, confirm=True)   # 读纯文本（隐私）
    run_clipboard(driver, action="write", text="结果已生成", confirm=True)   # 干跑（默认）
    run_clipboard(driver, action="write", text="结果已生成", confirm=True, execute=True)

待接线（本期改动范围只在本模块，故先定义在此，门面接线时一并上移）：

- 危险判定 `assess_clipboard` 与两个规则名（`clipboard_read_text` / `clipboard_write`）
  按项目口径应搬进 `core.guard`（判定的单一出处）；
- `MAX_READ_TEXT_CHARS` 按项目惯例应搬进 `winuse/limit.py`（R-21 家族，与
  `MAX_OCR_IMAGE_BYTES` 并列，同 `core.search.MAX_SEARCH_RESULTS` 的处理方式）。
"""

from __future__ import annotations

import os
from typing import Any

from ..driver.errors import InvalidParams, WinUseError

__all__ = [
    "ACTIONS",
    "ACTION_READ",
    "ACTION_WRITE",
    "KIND_FILE",
    "KIND_IMAGE",
    "KIND_TEXT",
    "MAX_READ_TEXT_CHARS",
    "NOTE_READ_TEXT",
    "NOTE_READ_TYPES",
    "NOTE_WRITE_DRY",
    "NOTE_WRITE_DONE",
    "RULE_READ_TEXT",
    "RULE_WRITE",
    "SUPPORTED_OPTIONS",
    "TOOL_READ",
    "TOOL_WRITE",
    "VALUE_FIELDS",
    "ClipboardFileNotFound",
    "ClipboardInvalidAction",
    "ClipboardMissingValue",
    "ClipboardMultipleValues",
    "ClipboardNotAbsolutePath",
    "ClipboardOptionInvalid",
    "ClipboardOptionUnknown",
    "ClipboardOutputInvalid",
    "ClipboardUnsupported",
    "assess_clipboard",
    "normalize_action",
    "parse_options",
    "parse_read_result",
    "pick_write_value",
    "run_clipboard",
    "validate_local_file",
]

# --------------------------------------------------------------------------
# 常量：驱动工具名、动作名、写值三选一
# --------------------------------------------------------------------------

#: 驱动工具名（唯一出处；门面不许再写一遍字面量）
TOOL_READ = "clipboard_read"
TOOL_WRITE = "clipboard_write"

ACTION_READ = "read"
ACTION_WRITE = "write"
ACTIONS: tuple[str, ...] = (ACTION_READ, ACTION_WRITE)

#: 回执里的"写了哪一类值"
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_FILE = "file"

#: 写值三选一：（门面参数名, kind）
VALUE_FIELDS: tuple[tuple[str, str], ...] = (
    ("text", KIND_TEXT),
    ("image_path", KIND_IMAGE),
    ("file_path", KIND_FILE),
)

#: kind → 门面参数名（下发驱动时用的键）
WRITE_FIELD: dict[str, str] = {kind: field for field, kind in VALUE_FIELDS}

#: 危险判定规则名（**接线时随 `assess_clipboard` 一起搬进 `core/guard.py`**）
RULE_READ_TEXT = "clipboard_read_text"
RULE_WRITE = "clipboard_write"

#: options 白名单（写错的键当场报，不静默忽略——同 `core.screenshot.parse_options`）
SUPPORTED_OPTIONS: tuple[str, ...] = ("max_text_chars",)

#: 读回文本的字符上限（超长**裁剪但留痕**：`truncated` + `truncate_hint`）。
#:
#: 为什么是 8000：门面的线路预算是 24000 **字节**，中文按 3 字节/字算，8000 字已顶到预算，
#: 再长会把整条回包撑爆——那时 `limit.enforce_wire_budget` 只能回 `RESULT_TOO_LARGE`，
#: 调用方连"读了多长"都看不到，比拿到前 8000 字糟糕得多。
MAX_READ_TEXT_CHARS = 8000

#: 回执里的口径声明（上层据此判断"要不要确认"，不必猜）
NOTE_READ_TYPES = (
    "只列剪贴板类型（格式名，不含内容）。读不改变任何状态，因此不需要 confirm、也不设干跑闸门"
    "（与 winuse_search 的只读口径一致）；要纯文本需 include_text=true 且 confirm=true。"
)
NOTE_READ_TEXT = (
    "已读取剪贴板纯文本：正文只回给调用方，审计只记类型与长度、不记内容；"
    "文本可能被裁剪（见 truncated），长度按全文报。"
)
NOTE_WRITE_DRY = (
    "干跑：未下发任何写入（加 --execute 才真实替换剪贴板）。"
    "请注意写操作会整段覆盖当前剪贴板内容，且不可撤销。"
)
NOTE_WRITE_DONE = "已把剪贴板替换为一个值（覆盖掉的内容不可恢复）"


# --------------------------------------------------------------------------
# 错误码（调用方错误一律抛；命名与本模块错误族一致）
# --------------------------------------------------------------------------


class ClipboardInvalidAction(WinUseError):
    """action 不是 read / write。"""

    code = "CLIPBOARD_INVALID_ACTION"


class ClipboardMissingValue(WinUseError):
    """写剪贴板三选一里一个值都没给（含全空白文本）。"""

    code = "CLIPBOARD_MISSING_VALUE"


class ClipboardMultipleValues(WinUseError):
    """写剪贴板给了多个值——驱动只接受"替换为一个值"。"""

    code = "CLIPBOARD_MULTIPLE_VALUES"


class ClipboardNotAbsolutePath(WinUseError):
    """图片/文件路径不是绝对路径（驱动只接受绝对路径）。"""

    code = "CLIPBOARD_NOT_ABSOLUTE_PATH"


class ClipboardFileNotFound(WinUseError):
    """路径不存在、或给的是目录——**在碰驱动之前**拒。"""

    code = "CLIPBOARD_FILE_NOT_FOUND"


class ClipboardOutputInvalid(WinUseError):
    """驱动回执形态不对（如 include_text=true 却回了非文本）——不猜、不当成空。"""

    code = "CLIPBOARD_OUTPUT_INVALID"


class ClipboardUnsupported(WinUseError):
    """驱动报告当前环境不支持剪贴板访问。

    绝不降级成"类型列表为空"：那是"剪贴板里没有东西"的意思，与"读不到"完全不是一回事。
    """

    code = "CLIPBOARD_UNSUPPORTED"


class ClipboardOptionUnknown(WinUseError):
    """options 里有不认识的键——不静默忽略（写错的键必须当场被看见）。"""

    code = "CLIPBOARD_OPTION_UNKNOWN"


class ClipboardOptionInvalid(WinUseError):
    """options 不是映射，或取值非法。"""

    code = "CLIPBOARD_OPTION_INVALID"


# --------------------------------------------------------------------------
# 纯函数：参数归一与校验
# --------------------------------------------------------------------------


def normalize_action(action: Any) -> str:
    """动作名归一（大小写不敏感、去两端空白）；认不出一律抛（纯函数，不碰驱动）。"""
    name = str(action or "").strip().lower()
    if name not in ACTIONS:
        raise ClipboardInvalidAction(
            f"不支持的剪贴板动作：{action}",
            "可用：read（列类型 / 读文本）、write（写一个值）",
        )
    return name


def _as_flag(value: Any, field: str) -> bool:
    """布尔参数严格校验：只收 True / False / None（None 视为 False）。

    为什么不用 `bool(value)`：字符串 `"false"` 会变成 **True**——对 `include_text` 来说，
    那意味着一次"只列类型"的调用悄悄变成真正的剪贴板正文读取（隐私面）。这类错必须当场拒。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise InvalidParams(
        f"{field} 必须是布尔值",
        f'收到：{value!r}（类型 {type(value).__name__}）；注意字符串 "false" 不是 False，请传 JSON 布尔',
    )


def parse_options(options: Any) -> dict:
    """纯函数：options 校验并归一（未知键**报错**，不静默忽略）。

    目前只认一个键：

    - `max_text_chars`：读回文本的字符上限（正整数；超过模块上限则**夹紧**并标
      `max_text_chars_clamped`，与 R-21 的"上限夹紧、下界报错"取舍一致）。
      只对 `action="read"` 且 `include_text=true` 生效。
    """
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ClipboardOptionInvalid(
            f"options 必须是映射（收到 {type(options).__name__}）",
            "支持的键：" + "、".join(SUPPORTED_OPTIONS),
        )
    unknown = sorted(str(key) for key in options if key not in SUPPORTED_OPTIONS)
    if unknown:
        raise ClipboardOptionUnknown(
            "不支持的 options 键：" + "、".join(unknown),
            "支持的键：" + "、".join(SUPPORTED_OPTIONS),
        )

    out: dict[str, Any] = {}
    if "max_text_chars" in options and options["max_text_chars"] is not None:
        raw = options["max_text_chars"]
        # bool 是 int 的子类，`max_text_chars=True` 必须被拒（同 registry / search 的口径）
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ClipboardOptionInvalid(
                f"max_text_chars 必须是正整数（收到 {raw!r}）",
                "它是读回文本的字符上限；不要用字符串或小数",
            )
        if raw <= 0:
            raise ClipboardOptionInvalid(
                f"max_text_chars 必须为正（收到 {raw}）",
                "0 或负数只会让回执里什么都读不到；要少读就设小一点点的正整数",
            )
        out["max_text_chars"] = min(raw, MAX_READ_TEXT_CHARS)
        if out["max_text_chars"] != raw:
            out["max_text_chars_clamped"] = True
    return out


def pick_write_value(text: Any = None, image_path: Any = None, file_path: Any = None) -> tuple[str, str]:
    """纯函数：写值**三选一** → `(kind, 值)`。

    规则（依据驱动 `clipboard_write` 的描述 *"Replace the system clipboard with exactly one
    value"*）：

    - 一个值都没给（含全空白文本 / 全空白路径）→ `ClipboardMissingValue`。**不替你猜**：
      空文本等于"清空剪贴板"，是另一件事，本门面不提供该语义（同 registry「不猜空值」）。
    - 给了多个 → `ClipboardMultipleValues`：哪个为准无法从参数推断，宁可拒也不挑一个。
    - 文本**不做 trim**（首尾空白是内容）；路径做 trim（" D:/a.png" 的空白是误输入的，
      Windows 自己也忽略路径首尾空白）。
    """
    supplied = {"text": text, "image_path": image_path, "file_path": file_path}
    present: list[str] = []
    for field, _kind in VALUE_FIELDS:
        value = supplied[field]
        if value is None:
            continue
        if not isinstance(value, str):
            raise InvalidParams(
                f"{field} 必须是字符串",
                f"收到类型：{type(value).__name__}；写剪贴板的值一律按字符串原样给",
            )
        if value.strip():
            present.append(field)
        # 全空白的值按"没给"处理：写空串 / 空路径都不是调用方真正想要的动作

    if not present:
        raise ClipboardMissingValue(
            "写剪贴板必须三选一：text / image_path / file_path",
            "三者都没给（或为空白）：驱动只接受「替换为一个值」，本门面不替你猜要写什么",
        )
    if len(present) > 1:
        raise ClipboardMultipleValues(
            f"写剪贴板一次只能给一个值，收到 {len(present)} 个：" + "、".join(present),
            "驱动是「替换为一个值」：纯文本 / 图片 / 文件 URL 三选一；"
            "同时给多个（如 text + file_path）无法确定以哪个为准，故拒绝",
        )

    field = present[0]
    kind = {name: kind for name, kind in VALUE_FIELDS}[field]
    value = str(supplied[field])
    return kind, (value.strip() if kind != KIND_TEXT else value)


def validate_local_file(kind: str, path: str) -> str:
    """纯函数 + 只读文件系统：图片 / 文件路径校验（**在碰驱动之前**跑）。

    两道检查，都是"驱动只会给一句与原因无关的报错"的形态：

    1. **必须是绝对路径**：相对路径的解析基准（当前工作目录？当前盘？）对驱动来说未知，
       写进去的可能根本不是调用方以为的那个文件。盘符相对路径（`C:name.png`）同样拒。
    2. **必须是存在的普通文件**：不存在 → 驱动报"无法打开"；是目录 → 驱动报"不支持"。
       两者都在这里提前说清楚（目录单列一句提示，因为用户常把"文件夹"当"文件"给）。
    """
    field = WRITE_FIELD.get(kind, "path")
    text = str(path or "").strip()
    if not text:
        raise ClipboardMissingValue(
            f"{field} 是空白", "空白路径永远指向不了文件；请给绝对路径"
        )
    # Windows 上 `os.path.isabs` 认 `\\dir\\file`（当前盘根相对）为真，但它没有盘符：
    # 解析基准依赖"当前盘"，不是真正意义上的绝对路径，故要求 splitdrive 也有值。
    if not os.path.isabs(text) or not os.path.splitdrive(text)[0]:
        raise ClipboardNotAbsolutePath(
            f"{field} 必须是绝对路径：{text}",
            "驱动只接受绝对路径；请给形如 <盘符>:\\dir\\file 或 \\\\server\\share\\file 的写法",
        )
    if os.path.isdir(text):
        raise ClipboardFileNotFound(
            f"{field} 给的是目录，不是文件：{text}",
            "剪贴板只能放**一个文件**（文件 URL）；目录请先打包成压缩包或改放文本",
        )
    if not os.path.isfile(text):
        raise ClipboardFileNotFound(
            f"{field} 指向的文件不存在：{text}",
            "在碰驱动之前拒掉：不存在的路径只会换来一句与原因无关的驱动报错",
        )
    return text


def assess_clipboard(action: str, include_text: bool = False) -> Any:
    """危险判定：**委派给 `core.guard`**（判定口径只有一处，规则名见 guard.RULE_*）。

    两条规则：

    | 规则 | 触发 | 为什么危险 |
    |---|---|---|
    | `clipboard_write` | `action="write"` | 一次调用**整段替换**用户剪贴板内容（可能正是他还没粘贴的东西），且不可撤销 |
    | `clipboard_read_text` | `action="read"` 且 `include_text=True` | 剪贴板里可能是刚复制的口令 / 验证码 / 密钥；读正文等于把隐私面接进 agent 上下文 |

    只列类型不算危险：拿到的是格式名（`CF_UNICODETEXT` 之类），不含内容。保留这个函数名
    是为了让本模块的调用点不必知道判定住在哪。
    """
    from .guard import assess_clipboard_read, assess_clipboard_write

    if str(action or "").strip().lower() == ACTION_WRITE:
        return assess_clipboard_write()
    return assess_clipboard_read(bool(include_text))


def parse_read_result(raw: Any, include_text: bool = False, max_chars: int = MAX_READ_TEXT_CHARS) -> dict:
    """纯函数：驱动 `clipboard_read` 回执 → 对外字段（把"驱动换形态"的风险挡在这里）。

    - `types`：格式名列表。非字符串项**过滤掉**而不是 `str()` 出来——宁可少报格式名，
      也不编一个从来没存在过的类型名。
    - `supported`：驱动是否自报支持剪贴板。缺字段按"支持"处理（认不出的正常回执不该被判死），
      但**明说 false 就是 false**（上层据此报 `CLIPBOARD_UNSUPPORTED`，不谎报"剪贴板是空的"）。
    - `text`：只有 `include_text=True` 且驱动真回了字符串时才是 `str`；`None` = 剪贴板上没有
      可读的纯文本。**空串与 null 不是一回事**（前者是"真有一段空文本"）。
    - 回了非字符串（数字 / 对象）→ 抛 `ClipboardOutputInvalid`：不猜、不 `str()` 硬转。
    - 超长按 `max_chars` 裁剪，但 `text_length` 仍报**全文**长度（裁剪留痕，不虚报长度）。
    """
    data = raw if isinstance(raw, dict) else {}
    raw_types = data.get("types")
    types = [item for item in raw_types if isinstance(item, str) and item] if isinstance(raw_types, list) else []

    text: str | None = None
    if include_text:
        value = data.get("text")
        if isinstance(value, str):
            text = value
        elif value is not None:
            raise ClipboardOutputInvalid(
                "驱动在 include_text=true 时回的不是文本内容",
                f"收到类型：{type(value).__name__}；正文不回显（不把不可信字段当文本，也不 str() 硬转）",
            )

    full_length = len(text) if text is not None else None
    truncated = False
    limit = max(1, int(max_chars))
    if text is not None and len(text) > limit:
        text = text[:limit]
        truncated = True

    return {
        "types": types,
        "types_count": len(types),
        "supported": bool(data.get("supported", True)),
        "privacy_sensitive": bool(data.get("privacy_sensitive", False)),
        "text": text,
        "text_length": full_length,
        "truncated": truncated,
    }


# --------------------------------------------------------------------------
# 回执与审计的小工具
# --------------------------------------------------------------------------


def _error(code: str, message: str, detail: str = "") -> dict:
    """构造 `error` 块。脱敏与截断口径跟 `WinUseError.to_dict` 一致（R-20 统一出口）。"""
    from ..audit import redact_text

    out: dict[str, Any] = {"code": code, "message": redact_text(message, limit=300)}
    if detail:
        out["detail"] = redact_text(detail, limit=400)
    return out


def _failure(
    error: dict,
    *,
    action: str,
    tool: str,
    dry_run: bool = False,
    written_kind: str | None = None,
) -> dict:
    """失败回执：与成功回执同族（`ok` / `action` / `dry_run` / `written_kind` / `tool`）

    `error` 必须来自 `WinUseError.to_dict()` 或本模块 `_error()`——两者都已脱敏。
    """
    return {
        "ok": False,
        "action": action,
        "dry_run": bool(dry_run),
        "written_kind": written_kind,
        "tool": tool,
        "code": error.get("code"),
        "error": dict(error),
    }


def _audit(sink, **fields) -> bool | None:
    """写一条审计事件（与 `core.act._audit` 同协议：只有显式返回 False 才算写失败）。"""
    from ..audit import sink_event

    return sink_event(sink, **fields)


def _mark_audit(out: dict, written: bool | None) -> dict:
    """把审计结果放进回执（`None` = 没有 sink，就不给这个字段）。"""
    if written is not None:
        out["audit_written"] = written
    return out


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def _run_read(driver, *, want_text: bool, opts: dict, confirmed: bool, audit) -> dict:
    """读剪贴板：默认只列类型；`want_text=True` 是隐私读取，未确认一律拦下。"""
    verdict = assess_clipboard(ACTION_READ, include_text=want_text)

    if verdict.dangerous and not confirmed:
        written = _audit(
            audit,
            event="clipboard.blocked",
            action=ACTION_READ,
            ok=False,
            code=verdict.code,
            detail={"rule": verdict.rule, "include_text": True},
        )
        return _mark_audit(
            {
                "ok": False,
                "action": ACTION_READ,
                "dry_run": False,
                "tool": TOOL_READ,
                "written_kind": None,
                "include_text": True,
                "read_only": True,
                "would_call": None,  # 被拦不给调用计划
                "requires_confirm": True,
                "danger": verdict.to_dict(),
                "code": verdict.code,
                "error": _error(
                    verdict.code or "CONFIRM_REQUIRED",
                    f"读取剪贴板纯文本被安全层拦截：{verdict.reason}",
                    f"规则：{verdict.rule}；确认方式：CLI 加 --confirm / MCP 传 confirm=true。"
                    "只要类型不要内容时不必确认（本动作默认就只列类型）。",
                ),
            },
            written,
        )

    # 读不改变任何状态 → 不做干跑：读的"干跑"只会回一份拿不到内容的调用计划，没有意义。
    # 隐私面的闸门是 confirm（上面那道），不是 execute。
    try:
        raw = driver.call(TOOL_READ, {"include_text": want_text})
    except WinUseError as exc:
        written = _audit(
            audit,
            event="clipboard.read",
            action=ACTION_READ,
            ok=False,
            code=exc.code,
            detail={"include_text": want_text},
        )
        return _mark_audit(
            _failure(exc.to_dict(), action=ACTION_READ, tool=TOOL_READ), written
        )

    # 驱动"没抛异常但没成功"（空回执 / ok=false / refused）——不把"没报错"当"读到了"
    from .act import _driver_refusal

    refusal = _driver_refusal(raw)
    if refusal is not None:
        written = _audit(
            audit,
            event="clipboard.read",
            action=ACTION_READ,
            ok=False,
            code=refusal["code"],
            detail={"include_text": want_text},
        )
        return _mark_audit(_failure(refusal, action=ACTION_READ, tool=TOOL_READ), written)

    try:
        parsed = parse_read_result(
            raw, include_text=want_text, max_chars=opts.get("max_text_chars", MAX_READ_TEXT_CHARS)
        )
    except WinUseError as exc:
        written = _audit(
            audit,
            event="clipboard.read",
            action=ACTION_READ,
            ok=False,
            code=exc.code,
            detail={"include_text": want_text},
        )
        return _mark_audit(_failure(exc.to_dict(), action=ACTION_READ, tool=TOOL_READ), written)

    if not parsed["supported"]:
        code = ClipboardUnsupported.code
        written = _audit(
            audit,
            event="clipboard.read",
            action=ACTION_READ,
            ok=False,
            code=code,
            detail={"include_text": want_text},
        )
        return _mark_audit(
            _failure(
                _error(
                    code,
                    "驱动报告当前环境不支持剪贴板访问",
                    "supported=false：别把它当成「剪贴板是空的」——是**读不到**。"
                    "空列表另有其事（那才是「没有东西」）。",
                ),
                action=ACTION_READ,
                tool=TOOL_READ,
            ),
            written,
        )

    out: dict[str, Any] = {
        "ok": True,
        "action": ACTION_READ,
        "dry_run": False,
        "tool": TOOL_READ,
        "written_kind": None,
        "include_text": want_text,
        "read_only": True,
        "types": parsed["types"],
        "types_count": parsed["types_count"],
        "supported": True,
        "privacy_sensitive": parsed["privacy_sensitive"],
    }

    # 审计：方向 / 类型 / 长度，**不含正文**（正文只回调用方）
    detail: dict[str, Any] = {
        "include_text": want_text,
        "types": parsed["types"],
        "types_count": parsed["types_count"],
    }

    if not want_text:
        out["requires_confirm"] = False
        out["note"] = NOTE_READ_TYPES
    else:
        out["text"] = parsed["text"]
        out["text_length"] = parsed["text_length"]
        out["has_text"] = parsed["text"] is not None
        out["danger"] = verdict.to_dict()
        out["note"] = NOTE_READ_TEXT
        detail["text_length"] = parsed["text_length"]
        detail["truncated"] = parsed["truncated"]
        if "max_text_chars" in opts:
            out["max_text_chars"] = opts["max_text_chars"]
            if opts.get("max_text_chars_clamped"):
                out["clamped"] = {"max_text_chars": opts["max_text_chars"]}
        if parsed["truncated"]:
            # 裁剪必须留痕：让上层知道"这不是全文"，而不是以为剪贴板就这么长
            out["truncated"] = True
            out["truncate_hint"] = (
                f"文本共 {parsed['text_length']} 字，本次只回前 {len(parsed['text'] or '')} 字"
                f"（可调 options.max_text_chars，模块上限 {MAX_READ_TEXT_CHARS}）；"
                "要拿全文请改用别的方式（本门面不把整段正文灌进回包）"
            )

    written = _audit(audit, event="clipboard.read", action=ACTION_READ, ok=True, detail=detail)
    return _mark_audit(out, written)


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def _run_write(
    driver, *, text: Any, image_path: Any, file_path: Any, confirmed: bool, execute: bool, audit
) -> dict:
    """写剪贴板：三选一 → 路径校验 → 确认门 → 干跑 / 真下发。"""
    kind, value = pick_write_value(text=text, image_path=image_path, file_path=file_path)
    if kind in (KIND_IMAGE, KIND_FILE):
        value = validate_local_file(kind, value)

    verdict = assess_clipboard(ACTION_WRITE)
    # 审计只记规模与类别，**不记值**（文本正文 / 本机路径都不进日志）
    detail = {"kind": kind, "written_chars": len(value)}

    if not confirmed:
        written = _audit(
            audit,
            event="clipboard.blocked",
            action=ACTION_WRITE,
            ok=False,
            code=verdict.code,
            detail={**detail, "rule": verdict.rule, "execute": bool(execute)},
        )
        return _mark_audit(
            {
                "ok": False,
                "action": ACTION_WRITE,
                "dry_run": not execute,
                "tool": TOOL_WRITE,
                "written_kind": kind,
                "would_call": None,  # 被拦不给调用计划
                "requires_confirm": True,
                "danger": verdict.to_dict(),
                "code": verdict.code,
                "error": _error(
                    verdict.code or "CONFIRM_REQUIRED",
                    f"写剪贴板被安全层拦截：{verdict.reason}",
                    f"规则：{verdict.rule}；确认方式：CLI 加 --confirm / MCP 传 confirm=true；"
                    "确认后仍默认干跑，真替换还要 --execute。",
                ),
            },
            written,
        )

    args = {WRITE_FIELD[kind]: value}

    if not execute:
        written = _audit(
            audit,
            event="clipboard.dry_run",
            action=ACTION_WRITE,
            ok=True,
            detail={**detail, "confirmed": True},
        )
        return _mark_audit(
            {
                "ok": True,
                "action": ACTION_WRITE,
                "dry_run": True,
                "tool": TOOL_WRITE,
                "written_kind": kind,
                "would_call": {"tool": TOOL_WRITE, "args": args, "note": NOTE_WRITE_DRY},
                "code": None,
                "danger": verdict.to_dict(),
            },
            written,
        )

    try:
        result = driver.call(TOOL_WRITE, args)
    except WinUseError as exc:
        written = _audit(
            audit,
            event="clipboard.executed",
            action=ACTION_WRITE,
            ok=False,
            code=exc.code,
            detail={**detail, "confirmed": True},
        )
        return _mark_audit(
            _failure(exc.to_dict(), action=ACTION_WRITE, tool=TOOL_WRITE, written_kind=kind), written
        )

    from .act import _driver_refusal

    refusal = _driver_refusal(result)
    if refusal is not None:
        written = _audit(
            audit,
            event="clipboard.executed",
            action=ACTION_WRITE,
            ok=False,
            code=refusal["code"],
            detail={**detail, "confirmed": True},
        )
        return _mark_audit(_failure(refusal, action=ACTION_WRITE, tool=TOOL_WRITE, written_kind=kind), written)

    out: dict[str, Any] = {
        "ok": True,
        "action": ACTION_WRITE,
        "dry_run": False,
        "tool": TOOL_WRITE,
        "written_kind": kind,
        # 真下发的回执**不回显写入内容**（回执会进会话/日志；要看将写什么请先干跑）
        "written_chars": len(value),
        "code": None,
        "danger": verdict.to_dict(),
        "note": NOTE_WRITE_DONE,
    }
    # 驱动回执里的可读回类型（描述原文：Returns the available types for read-back before paste）
    readback = parse_read_result(result, include_text=False)
    if readback["types"]:
        out["types"] = readback["types"]
        out["types_count"] = readback["types_count"]

    written = _audit(
        audit,
        event="clipboard.executed",
        action=ACTION_WRITE,
        ok=True,
        detail={**detail, "confirmed": True},
    )
    return _mark_audit(out, written)


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def run_clipboard(
    driver,
    *,
    action: Any,
    include_text: Any = False,
    text: Any = None,
    image_path: Any = None,
    file_path: Any = None,
    confirm: bool = False,
    execute: bool = False,
    options: dict | None = None,
    audit=None,
) -> dict:
    """读 / 写系统剪贴板（读默认只列类型；写默认干跑）。

    返回（成功，读）::

        {"ok": true, "action": "read", "dry_run": false, "tool": "clipboard_read",
         "written_kind": null, "include_text": false, "read_only": true,
         "types": ["CF_UNICODETEXT", …], "types_count": 4,
         "supported": true, "privacy_sensitive": true, "requires_confirm": false, "note": "…"}

    读文本（`include_text=true` 且 `confirm=true`）另有 `text` / `text_length` / `has_text` /
    `danger`；超长时另有 `truncated` / `truncate_hint`。

    返回（成功，写）::

        # 干跑（confirm=true, execute=false）
        {"ok": true, "action": "write", "dry_run": true, "tool": "clipboard_write",
         "written_kind": "text", "would_call": {"tool": …, "args": {…}, "note": "…"}, "danger": {…}}
        # 真下发（confirm=true, execute=true）
        {"ok": true, "action": "write", "dry_run": false, "written_kind": "text",
         "written_chars": 5, "types": ["CF_UNICODETEXT", …], "danger": {…}}

    失败（驱动报错 / 拒绝 / 不支持）：`{ok: false, action, dry_run, written_kind, tool, code, error}`。

    门禁矩阵（哪一条都没走通时**一个字节都不下发**）::

        | action | 参数 | confirm | execute | 结果 |
        |---|---|---|---|---|
        | read  | include_text=false | 不需要 | 不需要（读不设干跑闸门） | 真列类型 |
        | read  | include_text=true  | 必须 true | 不需要 | 未确认 → 结构化拦截；确认 → 真读 |
        | write | 三选一             | 必须 true | 默认 false | 未确认 → 拦截；确认+未 execute → 干跑 `would_call` |
        | write | 三选一             | true | true | 真替换剪贴板 |

    **调用方错误直接抛** `WinUseError` 子类（动作名、布尔参数、三选一、路径绝对性 / 存在性、
    options），且在**下发任何驱动调用之前**——参数错就别浪费一次驱动，更别让上层以为"碰过剪贴板"。
    """
    name = normalize_action(action)
    opts = parse_options(options)
    confirmed = _as_flag(confirm, "confirm")
    do_execute = _as_flag(execute, "execute")
    want_text = _as_flag(include_text, "include_text")

    if name == ACTION_READ:
        stray = [
            field
            for field, value in (("text", text), ("image_path", image_path), ("file_path", file_path))
            if value is not None
        ]
        if stray:
            raise InvalidParams(
                "action='read' 不接受 " + "、".join(stray),
                "读只需要 include_text；要写剪贴板请用 action='write'"
                "（三选一：text / image_path / file_path）",
            )
        return _run_read(driver, want_text=want_text, opts=opts, confirmed=confirmed, audit=audit)

    if want_text:
        raise InvalidParams(
            "include_text 只对 action='read' 有意义",
            "写剪贴板请用 text / image_path / file_path 三选一给出要写入的值",
        )
    return _run_write(
        driver,
        text=text,
        image_path=image_path,
        file_path=file_path,
        confirmed=confirmed,
        execute=do_execute,
        audit=audit,
    )
