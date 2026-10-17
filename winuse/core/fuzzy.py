"""模糊元素寻址：记错一个字也能找到元素（编辑距离容错 + 相似度排序）。

为什么要有这个模块：元素寻址原先只有**精确子串**一条路（`core.state` 的 `query`
投影、窗口选择用的也是子串）——调用方（人/模型）把「保存文件」说成「保存文档」、
把 `Settings` 打成 `Settigns`，就永远匹配不上，只能反复重读状态瞎试。本模块把
「模糊匹配」做成核心能力，并**把匹配度定义写死在文档里**（口径含糊的相似度等于
每次调用都在猜）。

## 匹配度（score）的定义——三条规则，按序短路

先把两边做归一：`strip()` + `casefold()`（大小写与首尾空白不算差别）。

1. **完全相等 → 1.0**
2. **互为子串 → 1.0**（任一方包含另一方）

   为什么子串直接给满分：① 精确子串命中**本来就是**加模糊之前的行为，不能被
   "模糊化"降级（原先能命中的元素不能因为加了容错反而掉出候选）；② 界面标签与
   调用方说辞的长短方向不定（模型常把「搜索」说成「搜索框」，也会把
   「保存并退出」说成「保存」），只认单向包含会把一半常见命中判掉；
   ③ 子串判定是 O(n) 的 `in`，对五万字的正文反而**最省**（不用跑编辑距离）。

3. **否则 → `1 - 编辑距离 / max(两边长度)`**（归一化 Levenshtein）

   这一条比的是**三组串**，取最高分：全长、候选的**头部窗口**、候选的**尾部窗口**
   （窗口宽度 = 查询长度，只在候选更长时才试）。为什么不全长一比就完事：真机采样的
   标签大量带首尾记号（`修改日期▲`、`打印预览（PDF）`、`★ 收藏`），调用方不会打出
   那截记号，而"多一个记号"在纯长度归一下**也是一步编辑**——「修改日斯」对
   `修改日期▲` 就是 2 步 / 分母 5 = 0.6，掉到阈值下，痛点照样在。窗口一比：
   对 `修改日期` 是 1 步 / 分母 4 = 0.75，命中。
   为什么不是全滑窗（fuzzywuzzy 的 `partial_ratio`）：全滑窗代价是
   `候选长度 × 每次比较`，一棵两千元素的树会拖垮一次寻址；界面标签的差异集中在
   首尾（记号、快捷键提示），头尾两个窗口就够，**代价只有 3 倍常数**。
   出口回的是**完整原值**，窗口只用于算分。

   比较时带 `max_distance = int(窗口长度 × (1 - threshold))` 的上界早停。

入参里的 `threshold` 与 `score` 同尺度：**score ≥ threshold 才算候选**。

## 为什么阈值默认 0.7

- **太高（如 0.9）**：短标签错一个字就被判死——4 个字的标签错一个字是
  `1 - 1/4 = 0.75`，5 个字错一个字是 `0.8`，阈值 0.9 等于"模糊匹配根本没生效"，
  痛点原样保留。
- **太低（如 0.4）**：两三个字的完全无关短词也会互相"相似"（分母小、一个字就是
  0.5~0.67），候选里全是噪音，调用方还得自己再挑一遍——省下的那点重读成本全赔回去。
- 0.7 的落点：4 字错 1 字命中（0.75 ≥ 0.7），3 字错 2 字、2 字错 1 字不命中
  （0.33 / 0.5 < 0.7）。也就是说**允许"记错一个字"，不允许"记错一半"**。
  （带首尾记号的候选由规则 3 的窗口兜住，等价于"允许候选多一截记号 + 记错一个字"。）
- **短标签的已知边界**（别以为是 bug）：3 字标签错 1 字是 `1 - 1/3 = 0.667`，
  在默认 0.7 下**不命中**（分母小，一个字就是 33%）。这类调用请显式把
  `threshold` 调到 0.6 上下，或先用 `fields`/`winuse_state` 的 `query` 缩小范围。
  默认值刻意取 0.7 而不是 0.6：0.6 会让"3 字里错 2 字"（0.333）之外的
  两字无关短词开始互撞（0.5 附近的噪音），而候选列表是要人/模型直接挑的。

## 长串保护（别在长字符串上退化成平方复杂度灾难）

- `MAX_COMPARE_CHARS = 256`：参与编辑距离比较的只是**头部 256 字符**。界面标签/
  值的辨别信息在头部；超过 256 字符的多半是正文/日志，那类内容本来就不该走模糊
  匹配（要精确找内容请用 `winuse_state` 的 `query` 投影或 `winuse_read`）。
- `MAX_DP_CELLS = 250_000`：**无上界**的精确距离只在很小的串上算；超预算直接
  `PARAM_OUT_OF_RANGE`（明确拒绝，不静默截断成错答案，也不静默算到天亮）。
- 走上界时是带内 DP（只看 `|i-j| ≤ 上界` 的格子）：`O(长度 × 上界)`，两万字符也秒回。
- 另有公共前缀/后缀剥离：相似串（最常见的情形）实际比较的只剩中间那一小段。
- 规则 3 的窗口只多两组、宽度等于查询长度——**常数级**开销，不随候选长度增长。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..driver.errors import InvalidParams, MissingParam, ParamOutOfRange

__all__ = [
    "DEFAULT_FIELDS",
    "FIELD_ALIASES",
    "MATCHED_VALUE_MAX_CHARS",
    "MAX_COMPARE_CHARS",
    "MAX_DP_CELLS",
    "MAX_FUZZY_LIMIT",
    "MIN_THRESHOLD",
    "MAX_THRESHOLD",
    "SUGGEST_HINT",
    "TEXT_FIELDS",
    "find_elements",
    "levenshtein",
]

# --------------------------------------------------------------------------- 字段口径

#: 元素上**真实存在**的文本字段（照抄 cua-driver 0.28.2 实测字段，别臆造）：
#: 驱动的 `label` = UIA 的 Name 属性，`value` = Value 模式的当前内容。
TEXT_FIELDS: tuple[str, ...] = ("label", "value", "role")

#: 调用方常用叫法 → 驱动真实字段名。**只在入口做翻译，出口一律给真实名**——
#: 出口给别名会让调用方拿着一个并不存在的字段名去别处用（项目踩过
#: 「短名/长名混用」的坑：`element_token` 与 `token` 混着找，门禁静默失效）。
FIELD_ALIASES: dict[str, str] = {
    "name": "label",  # UIA 的 Name 属性
    "text": "value",  # 控件的当前文本内容
    "title": "label",
}

#: `find_elements` 的默认比较字段（调用方习惯叫法，由 `FIELD_ALIASES` 落地）
DEFAULT_FIELDS: tuple[str, ...] = ("name", "text", "title")

# --------------------------------------------------------------------------- 数值口径

#: 一次最多回多少候选（超过就该收紧条件，而不是读一屏）
MAX_FUZZY_LIMIT = 50

#: 参与编辑距离比较的最大字符数（超过只比头部，见模块 docstring）
MAX_COMPARE_CHARS = 256

#: 无上界的精确距离允许的最大格子数（`len(a) × len(b)`），超了明确拒绝
MAX_DP_CELLS = 250_000

MIN_THRESHOLD = 0.0
MAX_THRESHOLD = 1.0

#: 出口回显的 `matched_value` 最大字符数（与 `core.state` 的 label/value 裁剪同口径：
#: 120 字符 + 省略号）。完整内容用 `winuse_read` 读。
MATCHED_VALUE_MAX_CHARS = 120

SUGGEST_HINT = (
    "候选按匹配度降序；拿到了 index/token 就去下动作"
    "（token 自带快照信息，index 必须配同源 snapshot_id）。"
    "一次没找到：放宽 threshold、或换 fields、或用 winuse_state 重读一遍状态。"
)


def _as_int(value: Any, *, field: str, low: int, high: int) -> int:
    """整数参数校验：类型不对 → `INVALID_PARAMS`；量级不对 → `PARAM_OUT_OF_RANGE`。

    两类分开是刻意的（与 `driver.errors`、`core.params.strict_bool` 同分工）：类型错是
    调用方序列化写错（`"3"` 这种字符串绝不能"顺手转一下"——一旦开了口子，
    `core.params` 那条"布尔不能写成字符串"的纪律就会在隔壁字段上破功），
    量级错是调用方该改小——修法不同，错误码必须能区分。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidParams(
            f"{field} 必须是整数（收到 {type(value).__name__}：{value!r}）",
            f"{field} 限 {low}~{high}；不要写成字符串",
        )
    if value < low or value > high:
        raise ParamOutOfRange(
            f"{field}={value} 超出允许范围 {low}~{high}",
            "一次读太多候选本身就说明条件没收紧，请改小后重试",
        )
    return value


def _as_score(value: Any, *, field: str) -> float:
    """匹配度类参数（0~1）校验：类型不对 → `INVALID_PARAMS`，量级不对 → `PARAM_OUT_OF_RANGE`。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParams(
            f"{field} 必须是 0~1 之间的数字（收到 {type(value).__name__}：{value!r}）",
            "例如 0.7（默认）表示「允许记错一个字」量级；不要写成字符串",
        )
    score = float(value)
    if not (MIN_THRESHOLD <= score <= MAX_THRESHOLD):
        raise ParamOutOfRange(
            f"{field}={value} 超出 {MIN_THRESHOLD}~{MAX_THRESHOLD}",
            "阈值与匹配度同尺度：1.0=只认完全一致，0.7=允许错一个字",
        )
    return score


def _resolve_fields(fields: Sequence[str] | None) -> list[str]:
    """把调用方给的字段名翻译成**真实字段名**并去重保序（顺序 = 优先级）。"""
    if fields is None:
        fields = DEFAULT_FIELDS
    if isinstance(fields, str):  # 单个字符串当一格用，别按字符拆
        fields = (fields,)
    try:
        items = list(fields)
    except TypeError:
        raise InvalidParams(
            f"fields 必须是字段名序列（收到 {type(fields).__name__}）",
            "可用：" + "、".join(sorted(set(TEXT_FIELDS) | set(FIELD_ALIASES))),
        ) from None
    if not items:
        raise InvalidParams(
            "fields 不能为空——空列表等于「不比任何字段」",
            "可用：" + "、".join(sorted(set(TEXT_FIELDS) | set(FIELD_ALIASES))),
        )
    resolved: list[str] = []
    for name in items:
        if not isinstance(name, str):
            raise InvalidParams(
                f"fields 里必须是字符串字段名（收到 {type(name).__name__}：{name!r}）",
                "可用：" + "、".join(sorted(set(TEXT_FIELDS) | set(FIELD_ALIASES))),
            )
        key = name.strip()
        real = key if key in TEXT_FIELDS else FIELD_ALIASES.get(key)
        if real is None:
            raise InvalidParams(
                f"不认识的元素字段：{name!r}",
                "可用：" + "、".join(sorted(set(TEXT_FIELDS) | set(FIELD_ALIASES)))
                + "（别名会落到真实字段上）",
            )
        if real not in resolved:
            resolved.append(real)
    return resolved


def _as_query(text: Any) -> str:
    """要匹配的文本：非空字符串（空串匹配一切，属于"没给条件"，一律拒）。"""
    if not isinstance(text, str):
        raise InvalidParams(
            f"text 必须是字符串（收到 {type(text).__name__}：{text!r}）",
            "示例：text=\"保存文件\"",
        )
    if not text.strip():
        raise MissingParam("text 不能为空", "示例：text=\"保存文件\"")
    return text


# --------------------------------------------------------------------------- 编辑距离


def _validate_bound(max_distance: Any) -> int | None:
    if max_distance is None:
        return None
    if isinstance(max_distance, bool) or not isinstance(max_distance, int):
        raise InvalidParams(
            f"max_distance 必须是整数（收到 {type(max_distance).__name__}：{max_distance!r}）",
            "它是「只关心是否 ≤ N」的编辑次数上界；不要写成字符串",
        )
    if max_distance < 0:
        raise ParamOutOfRange(
            f"max_distance={max_distance} 不能为负",
            "0 表示只接受完全一致",
        )
    return max_distance


def levenshtein(a: str, b: str, *, max_distance: int | None = None) -> int:
    """编辑距离（插入/删除/替换各计 1）——纯 stdlib。

    返回值契约（**给 `max_distance` 时是近似口径，必须看清**）：

    - 不给 `max_distance`：返回**真实距离**；但 `len(a) × len(b)` 超过
      `MAX_DP_CELLS` 时抛 `PARAM_OUT_OF_RANGE`（精确比较有预算，不允许静默算到天亮，
      也不允许静默截断成错答案）。
    - 给了 `max_distance`：只保证**「返回值 ≤ max_distance ⇒ 是真实距离」**；
      返回值 > max_distance 时只表示「真实距离也 > max_distance」（可能是长度差
      这个下界，也可能是 `max_distance + 1`）。调用方只需要拿它跟阈值比。

    三层保护（对应模块 docstring 的"长串保护"）：

    1. 长度差早停：`|len(a) - len(b)| > max_distance` 时距离必然超过上界，直接返回长度差。
    2. 公共前缀/后缀剥离：相似串（最常见情形）真正比较的只剩中间一小段。
    3. 带内 DP + Ukkonen 截断：只看 `|i-j| ≤ max_distance` 的格子，值超过上界的格子
       直接剪掉；整行全被剪掉即可断定真实距离 > 上界，当场返回。
    """
    if not isinstance(a, str) or not isinstance(b, str):
        raise InvalidParams(
            f"levenshtein 只接受字符串（收到 {type(a).__name__} / {type(b).__name__}）",
            "元素文本请从 label/value 取；非字符串字段（enabled/selected）不进模糊匹配",
        )
    bound = _validate_bound(max_distance)

    if a == b:
        return 0
    if bound is not None:
        gap = abs(len(a) - len(b))
        if gap > bound:  # 长度差早停：下界已经超上界
            return gap

    # 公共前缀/后缀剥离（不改变距离，但把最常见的"只有一个字不同"降成常数级）
    start, left_len, right_len = 0, len(a), len(b)
    while start < left_len and start < right_len and a[start] == b[start]:
        start += 1
    end = 0
    while (
        end < left_len - start
        and end < right_len - start
        and a[left_len - 1 - end] == b[right_len - 1 - end]
    ):
        end += 1
    a2, b2 = a[start : left_len - end], b[start : right_len - end]
    if not a2 or not b2:
        return len(a2) + len(b2)
    if len(a2) > len(b2):  # DP 行取较短的串：O(min) 内存
        a2, b2 = b2, a2

    n, m = len(a2), len(b2)
    if bound is None:
        if n * m > MAX_DP_CELLS:
            raise ParamOutOfRange(
                f"精确编辑距离的格子数 {n * m} 超过预算 {MAX_DP_CELLS}"
                f"（{n} × {m} 字符）",
                "长文本请先截断（如 MAX_COMPARE_CHARS），或给 max_distance 走上界模式"
                "（带上界的复杂度是 长度 × 上界，不随两者乘积爆炸）",
            )
        prev = list(range(m + 1))
        for i in range(1, n + 1):
            ai = a2[i - 1]
            cur = [i] + [0] * m
            for j in range(1, m + 1):
                cost = 0 if ai == b2[j - 1] else 1
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            prev = cur
        return prev[m]

    # ---- 带上界的带内 DP（O(长度 × 上界)）----
    inf = bound + 1
    if bound == 0:
        return inf  # 已经过 a != b 的判定：只接受完全一致，必然超界
    prev = [inf] * (m + 1)
    prev[0] = 0
    for j in range(1, min(m, bound) + 1):
        prev[j] = j
    for i in range(1, n + 1):
        cur = [inf] * (m + 1)
        if i <= bound:
            cur[0] = i
        low = max(1, i - bound)
        high = min(m, i + bound)
        row_min = inf
        ai = a2[i - 1]
        for j in range(low, high + 1):
            cost = 0 if ai == b2[j - 1] else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if value > bound:  # Ukkonen 截断：这条路已经超上界，剪掉
                continue
            cur[j] = value
            if value < row_min:
                row_min = value
        if row_min > bound:
            # 整行剪光 → 任何一条 ≤ 上界的对齐路径都不存在（见模块 docstring 的证明思路）
            return inf
        prev = cur
    return prev[m] if prev[m] <= bound else inf


# --------------------------------------------------------------------------- 匹配度


def _normalize(text: str) -> str:
    """归一：去首尾空白 + casefold（大小写不敏感）。"""
    return text.strip().casefold()


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _candidate_windows(query: str, candidate: str) -> list[tuple[str, str]]:
    """三组待比串：全长 / 头部窗口 / 尾部窗口（见模块 docstring 规则 3）。

    窗口只在候选**更长**时才有意义（宽度取查询长度）：候选短于查询时，"窗口"就是它
    自己，而"候选完全被查询包含"已经被子串规则拿走了。
    """
    pairs = [(query, candidate)]
    if len(candidate) > len(query):
        width = len(query)
        pairs.append((query, candidate[:width]))
        pairs.append((query, candidate[-width:]))
    return pairs


def _ratio(query: str, candidate: str, threshold: float) -> float | None:
    """一组串的归一化相似度；低于 `threshold` 返回 `None`。

    阈值只用来**剪枝**：给 `levenshtein` 的上界由 `长度 × (1 - threshold)` 推出，
    因此"留下的候选"其 score 一定是精确值（≤ 上界的距离必为真值）。
    """
    left, right = query[:MAX_COMPARE_CHARS], candidate[:MAX_COMPARE_CHARS]
    span = max(len(left), len(right))
    if span == 0:
        return None
    budget = int(span * (1.0 - threshold))
    distance = levenshtein(left, right, max_distance=budget)
    if distance > budget:
        return None
    return 1.0 - distance / span


def score_of(text: str, value: str, threshold: float) -> float | None:
    """一次比较的匹配度；低于 `threshold` 返回 `None`（不是候选）。

    三条规则与"三组窗口"的口径见模块 docstring——**这里是匹配度的唯一定义处**，
    别在外面再写一份（两份必然漂移，而漂移的表现是"同一份状态两次给不同顺序"）。
    """
    query = _normalize(text)
    candidate = _normalize(value)
    if not query or not candidate:
        return None
    if query == candidate:
        return 1.0
    # 子串命中（O(n) 的 in，不必跑 DP；五万字正文直接满分命中）
    if query in candidate or candidate in query:
        return 1.0
    best: float | None = None
    for pair in _candidate_windows(query, candidate):
        score = _ratio(pair[0], pair[1], threshold)
        if score is not None and (best is None or score > best):
            best = score
    return best


# --------------------------------------------------------------------------- 元素定位


def _element_index_of(element: dict) -> int | None:
    """取元素序号（驱动原始回执叫 `element_index`，`core.state` 投影成 `index`）。"""
    index = element.get("element_index")
    return index if index is not None else element.get("index")


def _element_token_of(element: dict) -> str | None:
    """取元素令牌（驱动原始回执叫 `element_token`，`core.state` 投影成 `token`）。"""
    return element.get("element_token") or element.get("token")


def find_elements(
    elements: Iterable[dict] | None,
    *,
    text: str,
    threshold: float = 0.7,
    fields: Sequence[str] | None = DEFAULT_FIELDS,
    limit: int = 10,
) -> list[dict]:
    """在元素数组里按**匹配度**找元素，返回降序候选（每项含 index/token）。

    参数：

    - `elements`：元素数组。**驱动原始形状（label/value/element_index/element_token）
      与 `core.state` 投影形状（label/value/index/token）都吃**——两种形状都实测存在，
      调用方不该先挑形状。非 dict 的条目跳过（驱动回包可能有 null 占位）。
    - `text`：要找的文本，非空字符串；`None`/空串抛 `MISSING_PARAM`。
    - `threshold`：匹配度下限（0~1，默认 0.7，见模块 docstring）。超范围抛
      `PARAM_OUT_OF_RANGE`。
    - `fields`：参与比较的字段，默认 `("name","text","title")`（调用方叫法，经
      `FIELD_ALIASES` 落到真实字段 `label`/`value`）。空列表或陌生字段名抛
      `INVALID_PARAMS`。
    - `limit`：最多回多少条（1~`MAX_FUZZY_LIMIT`，默认 10）。**超上限是拒不是夹**：
      静默少回几条会让调用方以为"没有更合适的候选了"。

    返回：`[{"index", "token", "score", "matched_field", "matched_value"}, ...]`

    - `score`：该元素与 `text` 的匹配度（定义见模块 docstring），保留 4 位小数。
    - `matched_field` / `matched_value`：**真实**字段名与其原始值；一个元素在多个
      字段都命中时只回最好的那条。`matched_value` 出口截到
      `MATCHED_VALUE_MAX_CHARS`（完整内容用 `winuse_read` 读，别把一屏正文灌进上下文）。
    - 排序：score 降序 → `fields` 里的优先级 → 元素序号（升序）→ 令牌。**刻意完全
      确定**：同一份状态调用两次必须给同一个顺序，否则"上次排第一的那个"没有意义。

    与动作的接线：`token` 自带快照信息，可直接下动作；`index` 必须配同一份状态的
    `snapshot_id`（`winuse_act` 会拒没有快照的索引）。
    """
    query = _as_query(text)
    threshold = _as_score(threshold, field="threshold")
    limit = _as_int(limit, field="limit", low=1, high=MAX_FUZZY_LIMIT)
    keys = _resolve_fields(fields)

    rows: list[dict] = []
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        # 一个元素可能在多个字段都命中 → 只留最好的一条；同一分时按
        # (字段在 fields 里的优先级, 值的长度) 定序，保证顺序完全确定
        best: tuple[tuple[float, int, int], float, str, str] | None = None
        for key in keys:
            value = element.get(key)
            if not isinstance(value, str):  # 只有文本字段参与（enabled/selected 不是文本）
                continue
            score = score_of(query, value, threshold)
            if score is None:
                continue
            rank = (score, -keys.index(key), len(value))
            if best is None or rank > best[0]:
                best = (rank, score, key, value)
        if best is None:
            continue
        _, score, key, value = best
        index = _element_index_of(element)
        rows.append(
            {
                "index": index,
                "token": _element_token_of(element),
                "score": round(score, 4),
                "matched_field": key,
                "matched_value": _truncate(value, MATCHED_VALUE_MAX_CHARS),
                "_field_rank": keys.index(key),
                "_sort_index": index if isinstance(index, int) else 1 << 30,
                "_sort_token": _element_token_of(element) or "",
            }
        )

    rows.sort(key=lambda row: (-row["score"], row["_field_rank"], row["_sort_index"], row["_sort_token"]))
    out = []
    for row in rows[:limit]:
        out.append({key: row[key] for key in ("index", "token", "score", "matched_field", "matched_value")})
    return out
