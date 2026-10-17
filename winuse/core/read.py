"""winuse_read：一次读**多个**元素的值（省掉 N 次往返）。

痛点：要读一个列表里 20 个元素的值，原先只能一次读一个——每次都要
`get_window_state` + 人工挑字段，20 次往返换 20 个值。本模块把"多个元素 × 多个
字段"收成**一次** `get_window_state`：驱动一次就把整棵树给了，多读几个元素是
纯免费的（同一份快照内的索引本来就都在手上）。

## 关键设计

1. **索引必须绑定同源快照**（沿用 `core.act` / `core.drag` 的既有纪律）：任何一个
   元素用**索引**寻址而没给 `snapshot_id` → `SNAPSHOT_REQUIRED`，**一个字节都不发给
   驱动**。令牌（`element_token`）自带快照信息，可以不带。
2. **读一次会刷新驱动快照**（实测现象，见 `core.act.check_write_gate`）：所以回执里
   的 `snapshot_id` 与每个 item 的 `token` 都是**这次读回来的新值**——调用方要接着
   下动作，就用回执里的这两个，别用手上旧的。回执 `note` 里也写了这句话。
3. **字段名以驱动真实字段为准**（照抄 0.28.2 实测字段全集，不臆造）：
   `value / label / role / enabled / selected / actions / frame / depth /
   in_web_content`。调用方常说的 `text` / `bounds` / `name` / `title` 由
   `FIELD_ALIASES` 翻译成真实字段，**出口一律给真实字段名**（出口给别名，调用方
   拿着一个并不存在的字段名去别处用就静默失效了）。
4. **字段不存在给 `None`，不编空串**：按钮没有 `value` 就是 `None`——空串是"有这个
   字段且内容为空"，两者含义完全不同（写值门禁就靠这个区别）。
5. **条数超上限是拒，不是夹**（见 `MAX_READ_ELEMENTS`）：静默少读几个，调用方会
   以为整批都读到了，而"读没读到"正是这个工具唯一的存在意义。
6. **不下推 `max_elements`**：本模块读的就是"树里第 N 个元素"，下推截断会把要读的
   元素正好裁掉。要省成本用 `options.query` 投影——但投影掉的目标会进 `missing`
   （**不**自动重读一次，重读就又多一次往返，且结果集已经变了）。

## 只读，也要审计

读值会把界面上的真实内容（可能是草稿、密码框内容、客户资料）带进上下文，所以
每次读都记一条 `read.executed`——但**只记字段名与计数，绝不记读到的内容**
（审计文件会长期留存，写进去就等于长期留存用户数据）。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..driver.errors import (
    InvalidParams,
    MissingParam,
    MissingTarget,
    ParamOutOfRange,
    SnapshotRequired,
    WinUseError,
)
# 刻意复用 act 的收纳辅助：审计落盘、`audit_written` 标注全项目一份
# （两份必然漂移，漂移的表现是"某条路径的审计静默没写"）。
from .act import _audit, _mark_audit, _token_index

__all__ = [
    "DEFAULT_READ_FIELDS",
    "FIELD_ALIASES",
    "FIELD_MAX_CHARS",
    "MAX_READ_ELEMENTS",
    "READABLE_FIELDS",
    "READ_OPTION_FIELDS",
    "READ_TOOL",
    "SNAPSHOT_NOTE",
    "run_read",
]

#: 驱动工具名（读状态就是它）
READ_TOOL = "get_window_state"

#: 一次最多读多少个元素。为什么是 50：
#:
#: - 与 `limit.MAX_BATCH_STEPS`（单次批量 50 步）同量级——"一次操作 50 个界面对象"
#:   已经是这个门面的常规粒度；
#: - 出口预算能算出来：一条 item 约 100~150 字符，50 条约 5~7.5k 字符，
#:   离门面的单条线路预算（`limit.MAX_WIRE_BYTES = 24000`）还有 3 倍余量，
#:   所以**不会**出现"读回来又被 `limit_payload` 裁掉一半"的情况；
#: - 反例：200 条约 20~30k 字符，正好撞上线路预算被裁——那才是真丢数据。
#:
#: **超限的处置是拒**（`PARAM_OUT_OF_RANGE`），不是夹紧：夹紧会让调用方以为
#: "整批读完了"，而它只拿到了前 50 个。要读更多就分批，每批带新的 `snapshot_id`。
MAX_READ_ELEMENTS = 50

#: 每个字段值出口的最大字符数（与 `core.state` 的 label/value 裁剪量级一致：
#: 界面控件的当前值通常很短；长到上千字符的多半是正文/日志，那种内容要按需取，
#: 不该因为"顺手读了 50 个"就整段灌进上下文）。被截断的字段名会记在 item 的
#: `truncated_fields` 里——**截断必须留痕**。
FIELD_MAX_CHARS = 1000

#: 元素上**真实存在**的字段（照抄 cua-driver 0.28.2 实测全集，别臆造）：
#: `label`=UIA Name，`value`=Value 模式内容，`frame`=原生桌面坐标矩形。
READABLE_FIELDS: tuple[str, ...] = (
    "value",
    "label",
    "role",
    "enabled",
    "selected",
    "actions",
    "frame",
    "depth",
    "in_web_content",
)

#: 默认读的字段：一个控件"是什么"（label）+"现在是什么"（value）——最常用的一对。
DEFAULT_READ_FIELDS: tuple[str, ...] = ("value", "label")

#: 调用方常用叫法 → 真实字段名（只在入口翻译，出口一律真实名）
FIELD_ALIASES: dict[str, str] = {
    "text": "value",
    "name": "label",
    "title": "label",
    "bounds": "frame",
    "rect": "frame",
}

#: 允许透传给驱动的可选项（**额外字段一律拒**：拼错的字段被静默忽略比报错更危险）
READ_OPTION_FIELDS = frozenset(
    {
        "query",  # 大小写不敏感子串投影（只回匹配行 + 祖先链，索引保留）
        "max_elements",  # 下推给驱动的元素条数上限（会截断，默认不动它）
        "max_depth",  # UIA 树深度上限
    }
)

SNAPSHOT_NOTE = (
    "读状态会刷新驱动快照：本回执的 snapshot_id 与每个 item 的 token 都是**这次读**回来的"
    "新值，接着下动作用它们；手上更早的 token 可能已失效（stale_element_token）。"
)


# --------------------------------------------------------------------------- 参数校验


def _require_window(target: dict) -> None:
    """读值要指定窗口——驱动不变量是 (pid, window_id) 一轮一次状态。"""
    if target.get("window_id") is None or target.get("pid") is None:
        raise MissingTarget(
            "读元素值需要窗口（pid + window_id）",
            "先用 winuse_state / winuse_window 选定目标窗口，再把 pid/window_id 传进来",
        )


def _as_element_ref(value: Any) -> tuple[int | None, str | None]:
    """元素引用归一 → `(index, token)`（二选一）。

    接受三种写法（与 `core.drag._as_element_ref` 同口径，调用方能照着搬）：
    索引 `3` / 令牌 `"s0000006e:3"` / 显式字典 `{"index": 3}`
    （`element_index` / `element_token` 长名也认）。
    """
    if isinstance(value, bool):
        raise InvalidParams(
            "元素引用不能是布尔（收到 True/False）",
            "写法：索引 3 / 令牌 \"s0000006e:3\" / {\"index\": 3}",
        )
    if isinstance(value, int):
        return value, None
    if isinstance(value, str):
        if not value.strip():
            raise InvalidParams(
                "元素令牌不能是空串",
                "写法：索引 3 / 令牌 \"s0000006e:3\" / {\"index\": 3}",
            )
        return None, value
    if isinstance(value, dict):
        index = value.get("element_index", value.get("index"))
        token = value.get("element_token", value.get("token"))
        if isinstance(index, int) and not isinstance(index, bool):
            return index, None
        if isinstance(token, str) and token.strip():
            return None, token
    raise InvalidParams(
        f"元素引用格式不对（收到 {type(value).__name__}：{value!r}）",
        "写法：索引 3 / 令牌 \"s0000006e:3\" / {\"index\": 3} / {\"token\": \"s0000006e:3\"}",
    )


def _as_refs(elements: Any) -> list[tuple[int | None, str | None]]:
    """把 `elements` 归一成引用列表，并卡住条数上限。

    只收**列表/元组**：单个标量不"顺手包一层"——`elements` 是复数名，收标量会让
    "给了 N 个"与"给了 1 个"在回执里长得一样（`count` 到底是几全靠猜），
    与 `core.params` 那条"宁可让调用方显式传"的纪律一致。
    """
    if elements is None:
        raise MissingParam(
            "elements 不能为空——读值至少要给一个元素",
            "写法：elements=[3, \"s0000006e:7\"]（索引需同源 snapshot_id；令牌自带）",
        )
    if not isinstance(elements, (list, tuple)):
        raise InvalidParams(
            f"elements 必须是元素引用列表（收到 {type(elements).__name__}）",
            "写法：elements=[3, \"s0000006e:7\"]；单个元素也要写成列表 [3]",
        )
    if not elements:
        raise MissingParam(
            "elements 不能为空——读值至少要给一个元素",
            "写法：elements=[3, \"s0000006e:7\"]（索引需同源 snapshot_id；令牌自带）",
        )
    refs = [_as_element_ref(item) for item in elements]
    if len(refs) > MAX_READ_ELEMENTS:
        raise ParamOutOfRange(
            f"一次最多读 {MAX_READ_ELEMENTS} 个元素（收到 {len(refs)} 个）",
            f"分批读：每批 ≤ {MAX_READ_ELEMENTS} 个，读完后用回执里的 snapshot_id "
            "续下一批（同一个快照内索引仍然有效）",
        )
    return refs


def _resolve_fields(fields: Sequence[str] | None) -> list[str]:
    """字段名 → 真实字段名（去重保序）。陌生字段一律拒，不静默忽略。"""
    if fields is None:
        fields = DEFAULT_READ_FIELDS
    if isinstance(fields, str):
        fields = (fields,)
    if not isinstance(fields, (list, tuple, set, frozenset)):
        raise InvalidParams(
            f"fields 必须是字段名序列（收到 {type(fields).__name__}）",
            "可用：" + "、".join(READABLE_FIELDS),
        )
    items = list(fields)
    if not items:
        raise InvalidParams(
            "fields 不能为空——空列表等于「什么都不读」",
            "可用：" + "、".join(READABLE_FIELDS),
        )
    resolved: list[str] = []
    for name in items:
        if not isinstance(name, str):
            raise InvalidParams(
                f"fields 里必须是字符串字段名（收到 {type(name).__name__}：{name!r}）",
                "可用：" + "、".join(READABLE_FIELDS),
            )
        key = name.strip()
        real = key if key in READABLE_FIELDS else FIELD_ALIASES.get(key)
        if real is None:
            raise InvalidParams(
                f"不认识的元素字段：{name!r}（不静默忽略——写错的字段名会让这一读静默变空）",
                "可用：" + "、".join(READABLE_FIELDS)
                + "；调用方习惯叫法 " + "、".join(sorted(FIELD_ALIASES)) + " 会被翻译",
            )
        if real not in resolved:
            resolved.append(real)
    return resolved


def _validate_options(options: dict | None) -> dict:
    """驱动侧可选项校验：非字典或含未知字段一律拒。"""
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise InvalidParams(
            f"options 必须是字典（收到 {type(options).__name__}）",
            "可用字段：" + "、".join(sorted(READ_OPTION_FIELDS)),
        )
    unknown = sorted(set(options) - READ_OPTION_FIELDS)
    if unknown:
        raise InvalidParams(
            "options 含驱动不认识的字段：" + "、".join(unknown),
            "可用字段：" + "、".join(sorted(READ_OPTION_FIELDS))
            + "；include_screenshot 由本模块固定为 false（text-first），不可覆盖",
        )
    return dict(options)


# --------------------------------------------------------------------------- 元素定位


def _element_index_of(element: dict) -> int | None:
    """取元素序号（驱动原始回执叫 `element_index`，`core.state` 投影成 `index`）。"""
    index = element.get("element_index")
    return index if index is not None else element.get("index")


def _element_token_of(element: dict) -> str | None:
    """取元素令牌（驱动原始回执叫 `element_token`，`core.state` 投影成 `token`）。"""
    return element.get("element_token") or element.get("token")


def find_element(elements: list[dict], index: int | None, token: str | None) -> dict | None:
    """在状态树里定位元素（与 `core.act._match_element` / `core.drag.find_element` 同口径）。

    顺序刻意如此：**令牌完全一致**优先 → 令牌里编码的序号 → 纯索引。
    令牌前缀带快照标识，跨快照会失效（`stale_element_token`），而序号不变——
    所以第 2 条兜底不能省（`core.drag` 真机踩过：只认完全一致会把所有跨快照调用判死）。
    """
    if token is not None:
        for element in elements:
            if _element_token_of(element) == token:
                return element
        index = _token_index(token)
        if index is None:
            return None
    if index is None:
        return None
    for element in elements:
        if _element_index_of(element) == index:
            return element
    return None


def _read_field(element: dict, field: str) -> tuple[Any, bool]:
    """取一个字段的值 → `(值, 是否被截断)`。

    字段不存在 → `(None, False)`：**不编空串**（空串是"有这个字段且为空"）。
    """
    if field not in element:
        return None, False
    value = element[field]
    if isinstance(value, str) and len(value) > FIELD_MAX_CHARS:
        return value[:FIELD_MAX_CHARS] + "…", True
    return value, False


# --------------------------------------------------------------------------- 业务


def run_read(
    driver,
    *,
    target: dict,
    elements: Any,
    fields: Sequence[str] | None = None,
    snapshot_id: str | None = None,
    options: dict | None = None,
    audit=None,
) -> dict:
    """一次 `get_window_state` 读多个元素的指定字段（只读，不需要 confirm）。

    参数：

    - `driver`：驱动对象（`driver.call(tool, args)`）。
    - `target`：目标窗口，至少含 `pid` + `window_id`（缺 → `MISSING_TARGET`）。
    - `elements`：要读的元素引用列表：索引 `int`（**必须配同源 `snapshot_id`**）、
      令牌 `str`（自带快照信息）、或 `{"index": N}` / `{"token": "..."}`。
      空/缺 → `MISSING_PARAM`；格式不对 → `INVALID_PARAMS`；
      超过 `MAX_READ_ELEMENTS` → `PARAM_OUT_OF_RANGE`（**拒，不夹**）。
    - `fields`：要读的字段，默认 `("value", "label")`。以**驱动真实字段名**为准：
      `value / label / role / enabled / selected / actions / frame / depth /
      in_web_content`；`text → value`、`bounds/rect → frame`、`name/title → label`
      的别名会被翻译。陌生字段 → `INVALID_PARAMS`。
    - `snapshot_id`：元素索引所属快照（`winuse_state` 的 `snapshot_id`）。
      索引寻址缺它 → `SNAPSHOT_REQUIRED`（一个字节都不发给驱动）。
    - `options`：透传给驱动的可选项，只认 `query / max_elements / max_depth`
      （`include_screenshot` 固定 false，不可覆盖）。注意 `query` 是**投影**：
      被投影掉的元素会进 `missing`，本模块不会为了找它们偷偷再读一次。
    - `audit`：审计 sink（`entry -> Any`）。记一条 `read.executed`，
      **只记字段名与计数，不记读到的内容**。

    返回：`{ok, tool, snapshot_id, target, items, missing, count, found_count,
    missing_count, requested_fields, max_elements, field_max_chars, note, ...}`

    - `items`：与 `elements` **同序**，每项 `{index, token, fields, found}`。
      找到时 `fields` 里**每个请求的字段都有键**（元素上不存在 → `None`）；
      `found=false` 时 `fields` 为空字典，`missing` 里另附 `reason`。
      `index`/`token` 取自**这次读回来**的元素（可继续下动作；token 免快照）。
    - `missing`：没找到的元素（选择器 + 原因）。缺元素**不是错误**——界面在变，
      读不到是正常结果，`ok` 仍为 `true`（与 `winuse_wait` 的超时口径一致）。
    - 驱动侧故障回收成 `ok: false` + `error`（组装期参数错误照旧抛，不混进去）。
    """
    target = target or {}
    _require_window(target)
    refs = _as_refs(elements)
    keys = _resolve_fields(fields)
    opts = _validate_options(options)

    # 索引绑定快照（与 core.act / core.drag 同纪律）：拿旧索引去读，可能读到同序号的
    # 另一个控件——而且读值会被上层当成"这个控件的内容就是这样"。
    if snapshot_id is None:
        for index, token in refs:
            if token is None:
                raise SnapshotRequired(
                    f"按元素索引读值必须带同一份快照的 snapshot_id（收到索引 {index}）",
                    "先调 winuse_state 取 snapshot_id，再用它返回的索引；"
                    "或改用元素令牌（element_token，自带快照信息）",
                )

    args: dict[str, Any] = {
        "pid": target.get("pid"),
        "window_id": target.get("window_id"),
        # 纯文本闭环：读值不需要截图（省一次 grab，也不让 base64 有机会进上下文）
        "include_screenshot": False,
    }
    args.update(opts)

    base: dict[str, Any] = {
        "tool": READ_TOOL,
        "target": {"pid": target.get("pid"), "window_id": target.get("window_id")},
        "requested_fields": list(keys),
        "max_elements": MAX_READ_ELEMENTS,
        "field_max_chars": FIELD_MAX_CHARS,
    }

    try:
        raw = driver.call(READ_TOOL, args) or {}
    except WinUseError as exc:  # 驱动侧故障回收成结构，不让异常穿层
        written = _audit(
            audit,
            event="read.failed",
            action=READ_TOOL,
            ok=False,
            code=exc.code,
            target=target,
            detail={"requested_fields": list(keys), "elements_requested": len(refs)},
        )
        return _mark_audit(
            {
                "ok": False,
                **base,
                "error": exc.to_dict(),
                "snapshot_id": snapshot_id,
                "items": [],
                "missing": [],
                "count": 0,
            },
            written,
        )

    elements_raw = [e for e in (raw.get("elements") or []) if isinstance(e, dict)]
    fresh_snapshot = raw.get("snapshot_id") or snapshot_id

    items: list[dict] = []
    missing: list[dict] = []
    for index, token in refs:
        element = find_element(elements_raw, index, token)
        if element is None:
            # 找不到时也给出**能认出来的**身份：索引优先取令牌里编码的序号
            known = index if index is not None else _token_index(token)
            item = {
                "index": known,
                "token": token,
                "found": False,
                "fields": {},
                "reason": "这次快照里没有该元素（界面已变 / 索引过期 / 被 query 投影掉了）",
            }
            missing.append({k: item[k] for k in ("index", "token", "reason")})
            items.append(item)
            continue
        values: dict[str, Any] = {}
        truncated: list[str] = []
        for key in keys:
            value, cut = _read_field(element, key)
            values[key] = value
            if cut:
                truncated.append(key)
        item = {
            "index": _element_index_of(element),
            "token": _element_token_of(element),
            "found": True,
            "fields": values,
        }
        if truncated:
            item["truncated_fields"] = truncated
        items.append(item)

    found = sum(1 for item in items if item["found"])
    written = _audit(
        audit,
        event="read.executed",
        action=READ_TOOL,
        ok=True,
        target=target,
        # 只记"读了什么字段、读到了几个"，**绝不记读到的内容**（审计长期留存）
        detail={
            "requested_fields": list(keys),
            "elements_requested": len(refs),
            "found": found,
            "missing": len(missing),
            "truncated": sum(1 for item in items if item.get("truncated_fields")),
            "snapshot_id": fresh_snapshot,
        },
    )
    out: dict[str, Any] = {
        "ok": True,
        **base,
        "snapshot_id": fresh_snapshot,
        "items": items,
        "missing": missing,
        "count": len(items),
        "found_count": found,
        "missing_count": len(missing),
        "elements_complete": raw.get("elements_complete"),
        "snapshot_element_count": raw.get("returned_element_count", raw.get("element_count")),
        "snapshot_total_element_count": raw.get("total_element_count"),
        "note": SNAPSHOT_NOTE,
    }
    return _mark_audit(out, written)
