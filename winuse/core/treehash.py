"""treehash：UI 树指纹（稳定哈希）与两份树的可读差异。

为什么"稳定"是这个模块全部设计取舍的来源：指纹的唯一用途是回答"界面变了没有"。
一份在界面**没变**时也会变的指纹毫无用处，而且它的失灵是**静默**的——不抛异常、
不报错，只让"没变"看起来像"变了"。所以下面每一条排除/归一都必须有理由：

1. **白名单，不是黑名单**（`HASHED_FIELDS`）：只有名单内的字段参与哈希。用黑名单
   的话，驱动将来往元素里加一个易变字段（命中测试计数、渲染批次号、时间戳……）
   就会**静默**毁掉所有指纹——而新增字段恰恰最可能是这类。
2. **坐标类字段（`frame` / `bounds`）必须排除**：窗口一移动、缩放、滚动，甚至只是
   用户拖了一下，这些值就全变，而树的内容一个比特都没变。它们描述的是"画在哪"，
   不是"画的是什么"。
3. **`element_token` 必须排除**（最隐蔽的一条）：令牌形如 `s00000039:0`，前缀是
   **快照号**；驱动每读一次状态就换一批快照号，于是**同一棵没变的树**、连读两次，
   令牌也不相等。把令牌算进指纹会让指纹每次调用都变。
4. **`element_index` / 快照号同理排除**：索引是"这张快照里的第几个元素"，是寻址用的
   位置属性；序列顺序与 `depth` 已经表达了结构，再算一次只会引入噪声。
5. **归一后再哈希**：缺省值补齐（`enabled` 缺省为真、`selected` 缺省为假——与
   `core/state.py` 同口径）、集合类字段**排序**（`actions` 的顺序由驱动内部遍历决定，
   不是界面属性）、文本**原样不截断**（截断会让尾部变化逃出指纹）。
   归一还保证"缺字段"与"显式给了缺省值"算出同一个哈希。
6. **哈希口径与 diff 口径同源**：`diff_trees` 只按 `HASHED_FIELDS` 判"变了没有"。
   两处若不同源，就会出现最糟的自相矛盾回执——"哈希没变，但说有几个元素变了"，
   调用方无从判断该信哪一个。

零第三方依赖、不 import 项目内其它模块（同 `limit.py` 的取舍）：门面与独立部署的
插件都能安全引用。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: 参与哈希的字段（**白名单**；顺序即对外契约，改动必须是有意识的）
#:
#: 选择标准 = "这条信息描述界面的**语义**，且不随'什么时候取'而变"。
#: - `depth`：元素在树里的层级（结构）
#: - `role`：无障碍角色（Button / Edit / ListItem……，描述"这是什么控件"）
#: - `label` / `value`：可见文本与内容（用户能看到的全部文字）
#: - `enabled` / `selected`：控件状态（禁用/选中会改变界面含义）
#: - `actions`：可用的动作集合（"能不能点"是语义；顺序不是，见下）
HASHED_FIELDS: tuple[str, ...] = (
    "depth",
    "role",
    "label",
    "value",
    "enabled",
    "selected",
    "actions",
)

#: 算法名（写出来是为了让"指纹怎么来的"自解释，不靠猜）
ALGORITHM = "sha256"

#: 回执里文本的截断长度（与 `state.py` / `wait_for.py` 的 120 同口径）
#: ⚠️ 截断**只作用于展示用的摘要**，绝不作用于哈希——否则长文本的尾部变化会逃出指纹
DIGEST_TEXT_MAX = 120


# ------------------------------------------------------------------ 归一


def _text(value: Any) -> str:
    """文本归一：`None` → 空串，其它非字符串显式转成字符串。

    为什么把 `None` 与 `""` 并成一种：驱动对"没有标签"有时回 `null`、有时干脆不写、
    有时回空串。这三种在界面上是同一件事，指纹里也必须是一件事。
    """
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _depth(value: Any) -> int | None:
    """层级归一：非整数（含布尔）一律当作"未知"，避免 `True` 被当成第 1 层。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _actions(value: Any) -> list[str]:
    """动作集合归一：只留字符串，**去重并排序**。

    排序的理由：`actions` 的顺序来自驱动内部遍历 UIA pattern 的顺序，不是界面属性。
    不排序的话，同一个按钮在不同轮次可能算出不同指纹——正是要避免的那类静默失灵。
    """
    return sorted({a for a in (value or []) if isinstance(a, str)})


def normalized_element(element: Any) -> dict:
    """元素 → 参与哈希的规范形状（缺省补齐 / 集合排序 / 文本原样不截断）。"""
    e = element if isinstance(element, dict) else {}
    return {
        "depth": _depth(e.get("depth")),
        "role": _text(e.get("role")),
        "label": _text(e.get("label")),
        "value": _text(e.get("value")),
        # 缺省值与 state.py 的读取口径一致：enabled 缺省为真、selected 缺省为假
        "enabled": bool(e.get("enabled", True)),
        "selected": bool(e.get("selected", False)),
        "actions": _actions(e.get("actions")),
    }


def _iter_elements(elements: Any):
    """把入参收敛成"元素字典的可迭代"（单给一个字典等价于只含它的列表）。"""
    if isinstance(elements, dict):
        return [elements]
    return elements or []


def _rows(elements: Any) -> list[dict]:
    """规范行序列（丢弃非字典项：它们不可能来自驱动的元素数组）。"""
    return [normalized_element(e) for e in _iter_elements(elements) if isinstance(e, dict)]


# ------------------------------------------------------------------ 指纹


#: 指纹的**口径提示**：随指纹一起回报，说明这两次指纹可比不可比。核心里指纹一律在
#: 裁剪与 query 投影**之前**算（整窗口径），所以只有**驱动侧**就回不全时才是部分口径——
#: 那时候指纹回答的是「驱动看到的那部分变了没」，而不是「整窗变了没」，两者结论可能相反
#: 且**都不报错**（静默失灵）。
SCOPE_NOTE_FULL = "指纹覆盖**整窗**元素树（在裁剪与 query 投影之前计算，与 max_elements / query 无关）"
SCOPE_NOTE_PARTIAL = (
    "指纹只覆盖**驱动返回的那部分**元素（回执 stats.driver_elements_complete=false，"
    "说明驱动侧元素数已到上限）：它回答的是「这部分变了没」，不代表整窗。"
    "要整窗指纹请调大元素上限"
)


def compute_tree_hash(elements: Any) -> str:
    """对无障碍元素**序列**算稳定哈希，返回 64 位小写十六进制 sha256。

    稳定性保证（测试逐条钉住）：同一棵没变的树，无论窗口移动到哪、无论驱动刷新了
    几次快照、无论 `actions` 用什么顺序返回，算出的值都相同；反过来，任何语义字段
    （role / label / value / depth / enabled / selected / actions）或元素顺序/条数变了，
    值就不同。

    "序列"是重点：元素顺序本身是结构信息，所以先把规范行序列按顺序序列化成 JSON
    （`sort_keys` 保证行内键序与字典插入顺序无关），再取 sha256。
    """
    payload = json.dumps(
        _rows(elements),
        ensure_ascii=False,  # 中文不转义：同样的字面 `\uXXXX` 会让指纹读起来像噪声
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 元素身份


def element_index_of(element: Any) -> int | None:
    """元素序号：驱动原始回执叫 `element_index`，`core/state.py` 投影成 `index`。"""
    if not isinstance(element, dict):
        return None
    idx = element.get("element_index")
    if idx is None:
        idx = element.get("index")
    if isinstance(idx, bool) or not isinstance(idx, int):
        return None
    return idx


def element_token_of(element: Any) -> str | None:
    """元素令牌：驱动原始回执叫 `element_token`，`core/state.py` 投影成 `token`。"""
    if not isinstance(element, dict):
        return None
    token = element.get("element_token") or element.get("token")
    return token if isinstance(token, str) and token else None


def token_index(token: Any) -> int | None:
    """从令牌里取序号（`s00000039:0` → 0）。

    为什么需要它：令牌前缀是**快照号**，驱动换快照后前缀会变，序号不变。所以跨两次
    状态读认"同一个元素"时，唯一可用的稳定部分是序号。
    """
    if not isinstance(token, str) or ":" not in token:
        return None
    tail = token.rsplit(":", 1)[1]
    return int(tail) if tail.isdigit() else None


def element_key(element: Any, position: int = 0) -> str:
    """元素身份键（跨两份树配对用）：有序号用序号，确实没有才退化为位置。

    - `index:3` = 按元素序号认（跨快照、跨两次读都稳定）
    - `pos:5` = 按在本轮序列里的位置认（驱动没给序号时的兜底）

    两种前缀**刻意不同**：位置兜底算出的 `pos:0` 绝不能与真实序号 `index:0` 撞键——
    撞键会把两个不相干的元素当成"同一个元素变了"，让回执直接说谎。
    """
    idx = element_index_of(element)
    if idx is None:
        idx = token_index(element_token_of(element))
    return f"index:{idx}" if idx is not None else f"pos:{position}"


# ------------------------------------------------------------------ 差异


def _truncate(text: str, limit: int = DIGEST_TEXT_MAX) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _digest(element: dict, position: int = 0) -> dict:
    """展示用摘要（**截断只发生在这里**，不影响任何哈希）。"""
    return {
        "key": element_key(element, position),
        "index": element_index_of(element),
        "role": _text(element.get("role")) or None,
        "label": _truncate(_text(element.get("label"))) or None,
        "value": _truncate(_text(element.get("value"))) or None,
        "depth": _depth(element.get("depth")),
    }


def _keyed(elements: Any) -> dict[str, dict]:
    """按身份键索引元素，保持**原序列顺序**（回执顺序要可预期）。

    同键重复（驱动异常）时首见为准——与 `core/act.py::_match_element` 的
    "命中第一个"口径一致，同一个仓库里两套认元素规则会互相打脸。
    """
    out: dict[str, dict] = {}
    for position, element in enumerate(_iter_elements(elements)):
        if not isinstance(element, dict):
            continue
        key = element_key(element, position)
        if key in out:
            continue
        out[key] = {
            "element": element,
            "key": key,
            "index": element_index_of(element),
            "position": position,
            "normalized": normalized_element(element),
        }
    return out


def diff_trees(before: Any, after: Any) -> dict:
    """两份元素树的可读差异：`{added, removed, changed, unchanged_count, hash_before, hash_after}`。

    判定口径与 `compute_tree_hash` **同源**（只比 `HASHED_FIELDS`）：所以"坐标动了一下"
    既不会改哈希、也不会出现在 `changed` 里。设计上必须如此——两处口径不一致时，
    调用方会收到"哈希没变但说有 N 个元素变了"这种自相矛盾的回执。

    - `added` / `removed`：某一侧独有的元素（按各自原序列顺序）
    - `changed`：两侧都有但内容不同的元素，`fields` 说明**变在哪几个字段**
      （`before` / `after` 只给这些字段的值，省 token）
    - `unchanged_count`：配对上且内容一致的元素条数
    """
    before_rows = _keyed(before)
    after_rows = _keyed(after)

    added = [
        _digest(row["element"], row["position"]) for key, row in after_rows.items() if key not in before_rows
    ]
    removed = [
        _digest(row["element"], row["position"]) for key, row in before_rows.items() if key not in after_rows
    ]

    changed: list[dict] = []
    unchanged_count = 0
    for key, before_row in before_rows.items():
        after_row = after_rows.get(key)
        if after_row is None:
            continue  # 已在 removed 里
        nb, na = before_row["normalized"], after_row["normalized"]
        fields = [name for name in HASHED_FIELDS if nb[name] != na[name]]
        if not fields:
            unchanged_count += 1
            continue
        changed.append(
            {
                "key": key,
                "index": before_row["index"],
                "role": _text(before_row["element"].get("role")) or None,
                "fields": fields,
                "before": {name: nb[name] for name in fields},
                "after": {name: na[name] for name in fields},
            }
        )

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged_count,
        "hash_before": compute_tree_hash(before),
        "hash_after": compute_tree_hash(after),
    }
