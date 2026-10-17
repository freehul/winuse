"""winuse_state：文本优先的本机状态（元素索引为主路径，像素坐标降为兜底）。

设计原则落点（P1 文本优先 / P2 元素索引优先 / P3 按需裁剪）：
1. 取窗口状态时显式带 `include_screenshot:false`——纯文本闭环，不花截图与视觉推理的钱
2. 驱动的原始返回里有 base64 截图、Markdown 树、冗余计数字段，**一律不进对外输出**
3. 元素按"能不能操作"裁剪：只留有 invoke/select/set_value/toggle/expand 的
   仅可滚动（scroll）与纯文本（text）的丢弃，但**逐类计数**，避免静默丢信息
4. 产物自解释：附 stats（保留/丢弃/是否被截断）与 coordinate_note
"""

from __future__ import annotations

from dataclasses import dataclass, replace, field
from typing import Any

from .. import limit as limit_mod
from ..driver.errors import WindowAmbiguous, WindowMinimized, WindowNotFound
from . import foreground as fg_mod

#: 判定"元素可操作"的动作集合（UIA pattern 名，来自驱动实测采样）
ACTIVE_ACTIONS = frozenset({"invoke", "select", "set_value", "toggle", "expand"})

#: 弱动作：仅含这些的元素默认丢弃（scroll 可由窗口级滚动工具替代；text 只是可读）
WEAK_ACTIONS = frozenset({"scroll", "text"})

COORDINATE_NOTE = (
    "元素建议用 index/token 走元素动作（走窗口消息，与像素坐标/DPI/屏幕无关，可后台调用）；"
    "frame 是原生桌面坐标，仅自绘界面等无法走元素时兜底使用。"
)

#: 两个窗口"最前"口径的差别（随输出一起给，避免上层把 z_order_top 当"用户在看哪个"）
WINDOW_ORDER_NOTE = (
    "focus = 系统前台窗口（GetForegroundWindow，用户真正在看哪个）；"
    "z_order_top = z_index 最大（叠放最靠前）。二者可以不同：驱动自带的悬浮层 "
    "z_index 最大但不是前台，用 z_order_top 当「当前窗口」会选错目标。"
)


@dataclass
class StateOptions:
    """winuse_state 的可选项（门面翻译成它，业务逻辑只在这里）。"""

    app: str | None = None
    pid: int | None = None
    window_id: int | None = None
    allow_minimized: bool = False
    include_frames: bool = False
    max_elements: int = 200
    label_max_chars: int = 120
    value_max_chars: int = 120
    keep_weak_only: bool = False
    include_windows: bool = True
    #: 驱动侧投影：只回匹配行 + 其祖先链（原始索引保留）——"按需取"比"全取再裁"省得多
    query: str | None = None
    #: 系统层面的真前台窗口标识（来自 GetForegroundWindow）——比 z_index 可靠：
    #: 驱动自带悬浮层也是顶层窗口且 z_index 最大，会骗走"取最前"的默认判定
    foreground_window_id: int | None = None
    #: 附 UI 树指纹（**整窗口径**）：在裁剪与 query 投影**之前**算，因为指纹要回答的是
    #: 「整窗变了没」——若基于裁剪后的元素算，改一次 `max_elements`、加一个 `query`
    #: 就会让同一个界面给出不同指纹，而这类判断**不会报错**，只会静默答错
    include_tree_hash: bool = False


def _truncate(text: Any, limit: int) -> Any:
    if not isinstance(text, str) or limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def focus_window(windows: list[dict]) -> dict | None:
    """**叠放顺序最靠前**的窗口 = z_index 最大（不是"系统前台"，见 `describe_focus`）。

    驱动文档明示：`z_index` 越大越靠前；**null 表示堆叠顺序不可用，调用方不得推断顺序**。
    因此全为 null 时返回 None（让上层要求显式指定），而不是拿数组顺序凑一个。

    名字保留是历史原因；对外输出请用 `z_order_top`，别把它当"用户在看哪个窗口"。
    """
    candidates = [w for w in windows if isinstance(w.get("z_index"), int)]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w["z_index"])


def foreground_window_of(windows: list[dict]) -> dict | None:
    """**系统前台**窗口（从清单里按 `GetForegroundWindow` 的句柄认领）。

    为什么不能只看 z_index：驱动自带的 `Cua.AgentCursorOverlay` 悬浮层也是顶层窗口
    且 z_index 最大，实测会骗走"取最前"的判定；用户真正在看哪个窗口只有系统知道。
    """
    hwnd = fg_mod.foreground_window_id()
    if not hwnd:
        return None
    return next((w for w in windows if w.get("window_id") == hwnd), None)


def describe_focus(windows: list[dict]) -> tuple[dict | None, dict | None]:
    """一次性给出 `(foreground, z_order_top)` 两个口径，避免调用方各自推断。

    - `foreground`：系统前台（`GetForegroundWindow`），拿不到时回落为 None
    - `z_order_top`：z_index 最大（叠放最靠前，**不是**前台）

    两者常常不同（悬浮层场景），所以输出里必须分开给，不能只给一个叫 `focus`。
    """
    return foreground_window_of(windows), focus_window(windows)


def _slim_window(w: dict) -> dict:
    return {
        "app_name": w.get("app_name"),
        "pid": w.get("pid"),
        "window_id": w.get("window_id"),
        "title": w.get("title"),
        "bounds": w.get("bounds"),
        "minimized": w.get("minimized"),
        "on_screen": w.get("is_on_screen"),
        "z_index": w.get("z_index"),
    }


def _default_target(windows: list[dict], foreground_window_id: int | None = None) -> dict:
    """无显式指定时选目标：系统前台 → z 可判取最前 → 唯一可见窗口 → 报歧义。

    `foreground_window_id=None` 表示"调用方没提供"——**这时自己去问系统**（R-2）。
    原先只有调用方显式传才会用到前台，没人传就一路回落到 z_index（悬浮层会骗走）。
    要表达"不要前台"就传 `NO_FOREGROUND`(0)：0 不等于任何真实句柄，自然落到 z_index。
    """
    if foreground_window_id is None:
        foreground_window_id = fg_mod.foreground_window_id()
    if foreground_window_id:
        hit = next((w for w in windows if w.get("window_id") == foreground_window_id), None)
        if hit is not None and not hit.get("minimized"):
            return hit
    focus = focus_window(windows)
    if focus is not None:
        return focus
    visible = [w for w in windows if w.get("is_on_screen") and not w.get("minimized")]
    if len(visible) == 1:
        return visible[0]
    raise WindowAmbiguous(
        f"无法判定目标窗口（z_index 不可用，可见窗口 {len(visible)} 个）",
        "请显式指定 --pid 或 --window-id",
    )


def pick_window(windows: list[dict], opts: StateOptions) -> dict:
    """按 window_id > pid > app 名 > 前台 的顺序选目标窗口。"""
    if opts.window_id is not None:
        for w in windows:
            if w.get("window_id") == opts.window_id:
                return _finalize(w, opts)
        raise WindowNotFound(f"未找到 window_id={opts.window_id} 的窗口")

    if opts.pid is not None:
        same = [w for w in windows if w.get("pid") == opts.pid]
        if not same:
            raise WindowNotFound(f"未找到 pid={opts.pid} 的窗口")
        return _finalize(_prefer_usable(same), opts)

    if opts.app:
        needle = opts.app.strip().lower()
        matched = [
            w
            for w in windows
            if needle in str(w.get("app_name") or "").lower() or needle in str(w.get("title") or "").lower()
        ]
        if not matched:
            raise WindowNotFound(
                f"未找到匹配「{opts.app}」的窗口（比对 app_name 与 title 的子串）"
            )
        return _finalize(_prefer_usable(matched), opts)

    return _finalize(_default_target(windows, opts.foreground_window_id), opts)


def _finalize(chosen: dict, opts: StateOptions) -> dict:
    """统一收口：最小化窗口的像素坐标是哨兵值，默认不静默返回。"""
    if chosen.get("minimized") and not opts.allow_minimized:
        raise WindowMinimized(
            f"目标窗口处于最小化状态（pid={chosen.get('pid')}, window_id={chosen.get('window_id')}）",
            "最小化窗口报的像素坐标是哨兵值，坐标类动作不可用；"
            "可先激活窗口（bring_to_front），或加 --allow-minimized 明确接受（元素类动作仍可用）",
        )
    return chosen


def _prefer_usable(candidates: list[dict]) -> dict:
    """多个候选里优先挑"能用"的：非最小化 > 最靠前 > 在屏。"""
    usable = [w for w in candidates if not w.get("minimized")] or candidates
    focus = focus_window(usable)
    if focus is not None:
        return focus
    on_screen = [w for w in usable if w.get("is_on_screen")]
    return (on_screen or usable)[0]


def trim_elements(elements: list[dict], opts: StateOptions) -> tuple[list[dict], dict]:
    """裁剪元素并逐类计数（不静默丢信息）。"""
    kept: list[dict] = []
    dropped_text_only = 0
    dropped_scroll_only = 0
    dropped_disabled = 0
    truncated_by_cap = False

    for e in elements or []:
        raw_actions = [a for a in (e.get("actions") or []) if isinstance(a, str)]
        active = [a for a in raw_actions if a in ACTIVE_ACTIONS]

        if not e.get("enabled", True):
            dropped_disabled += 1
            continue
        if not active:
            if opts.keep_weak_only:
                active = raw_actions
            elif "scroll" in raw_actions:
                dropped_scroll_only += 1
                continue
            else:
                dropped_text_only += 1
                continue

        if len(kept) >= _effective_cap(opts):
            truncated_by_cap = True
            continue

        kept.append(_slim_element(e, active, opts))

    stats = {
        "elements_total": len(elements or []),
        "elements_returned": len(kept),
        "dropped_text_only": dropped_text_only,
        "dropped_scroll_only": dropped_scroll_only,
        "dropped_disabled": dropped_disabled,
        "cap": _effective_cap(opts),
        "truncated_by_cap": truncated_by_cap,
    }
    return kept, stats


def _effective_cap(opts: StateOptions) -> int:
    """元素条数上限的实际取值（Optional-1）：夹进 `[0, MAX_STATE_ELEMENTS]`。

    为什么在**核心**夹而不是只靠 schema：负数会让切片行为诡异（`kept[: -3]` 这类
    语义与"最多 N 条"完全不符），超大值则让一次调用就把驱动回包整份物化出来
    （门面裁剪发生在拿到全部数据**之后**，等于没防）。
    """
    try:
        value = int(opts.max_elements)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, limit_mod.MAX_STATE_ELEMENTS))


def _slim_element(e: dict, active: list[str], opts: StateOptions) -> dict:
    """只留对外有用的字段：省 token 也避免把驱动内部结构泄漏给上层。"""
    out: dict[str, Any] = {
        "index": e.get("element_index"),
        "token": e.get("element_token"),
        "role": e.get("role"),
    }
    label = _truncate(e.get("label"), opts.label_max_chars)
    if label:
        out["label"] = label
    value = _truncate(e.get("value"), opts.value_max_chars)
    if value:
        out["value"] = value
    out["actions"] = active
    if e.get("selected"):
        out["selected"] = True
    if e.get("depth") is not None:
        out["depth"] = e.get("depth")
    if e.get("in_web_content"):
        out["web"] = True
    if opts.include_frames and e.get("frame"):
        out["frame"] = e.get("frame")
    return out


#: 下推 `max_elements` 时给驱动的**放大系数**（Optional-6）
#:
#: 为什么不能原样下推：驱动的截断口径是"前 N 个元素"，而核心的裁剪口径是
#: "优先留可操作元素"——两者的"第 N 个"不是同一批。留 4 倍余量，通常足以让核心
#: 从下推回来的子集里挑满用户要的条数；极端情况下（前 4N 个里可操作元素不足 N）
#: 结果会比不下推少几条，但换来的是**不会先把 872 个元素整份物化进内存**。
DOWNSTREAM_HEADROOM = 4

#: 下推值的硬上限（防峰值不能被自己放大：窗口再大也只让驱动回这么多）
MAX_PUSHED_ELEMENTS = 20000


def _pushed_max_elements(opts: StateOptions) -> int:
    """给驱动的 `max_elements`：核心上限 × 余量，再夹进硬上限。"""
    return min(_effective_cap(opts) * DOWNSTREAM_HEADROOM, MAX_PUSHED_ELEMENTS)


def _hash_opts(opts: StateOptions) -> StateOptions:
    """算指纹用的取状态参数：**固定**且与用户参数无关（不带 `query`、上限放到硬上限）。

    为什么必须单独一份：指纹要回答「整窗变了没」，而用户给的 `query` 与 `max_elements`
    会被下推给驱动、让驱动**先**把树裁掉——真机实测同一个没变的界面，默认 / 带 query /
    上限 1 三次调用给出三个不同指纹（其中上限 1 那次是空树的哈希）。这种失灵不会报错，
    只会让调用方把「参数变了」当成「界面变了」。
    """
    return replace(opts, query=None, max_elements=limit_mod.MAX_STATE_ELEMENTS)


def _driver_args(target: dict, opts: StateOptions) -> dict:
    """下发给驱动的取状态参数（纯树路径；query 与 max_elements 按需带上）。

    `max_elements` 下推的理由（Optional-6）：核心侧裁剪发生在**解析完整个回包之后**，
    不下推就是"先物化再裁"——真机实测同一个窗口，不下推回 872 个元素、
    带 `max_elements=5` 只回 4 个。防内存峰值只能靠让驱动少回。
    """
    args: dict[str, Any] = {
        "pid": target["pid"],
        "window_id": target["window_id"],
        "include_screenshot": False,
        "max_elements": _pushed_max_elements(opts),
    }
    if opts.query:
        args["query"] = opts.query
    return args


def build_state(driver, opts: StateOptions | None = None) -> dict:
    """取一份文本优先的本机状态快照。"""
    opts = opts or StateOptions()

    resp = driver.call("list_windows", {})
    windows = resp.get("windows") if isinstance(resp, dict) else resp
    windows = windows or []
    target = pick_window(windows, opts)

    raw = driver.call("get_window_state", _driver_args(target, opts))
    raw = raw or {}
    elements, stats = trim_elements(raw.get("elements") or [], opts)
    stats["driver_element_count"] = raw.get("element_count")
    stats["driver_elements_complete"] = raw.get("elements_complete")

    foreground, z_order_top = describe_focus(windows)
    out: dict[str, Any] = {
        "ok": True,
        "target": {
            "app_name": target.get("app_name"),
            "pid": target.get("pid"),
            "window_id": target.get("window_id"),
            "title": target.get("title"),
            "bounds": target.get("bounds"),
            "minimized": target.get("minimized"),
        },
        "snapshot_id": raw.get("snapshot_id"),
        "elements": elements,
        "stats": stats,
        "coordinate_note": COORDINATE_NOTE,
    }
    if opts.include_tree_hash:
        # 指纹单独发一次「不带 query、上限放到硬上限」的取状态：上面那份 raw 已被驱动
        # 按用户参数裁过，拿它算指纹等于照镜子（实测三个参数给出三个指纹）。
        from .treehash import SCOPE_NOTE_FULL, SCOPE_NOTE_PARTIAL, compute_tree_hash

        full = driver.call("get_window_state", _driver_args(target, _hash_opts(opts))) or {}
        out["tree_hash"] = compute_tree_hash(full.get("elements") or [])
        # 只有驱动自己回不全（元素数到它自己的上限）时，指纹才真的只覆盖一部分
        out["tree_hash_note"] = (
            SCOPE_NOTE_FULL if full.get("elements_complete") else SCOPE_NOTE_PARTIAL
        )
    if opts.include_windows:
        out["windows"] = [_slim_window(w) for w in windows]
        # 两个口径分开给（R-2）：`focus` = 系统前台（用户真正在看哪个），
        # `z_order_top` = z_index 最大（叠放最靠前）。二者常常不同——
        # 驱动悬浮层 z_index 最大但不是前台，混成一个字段会误导上层。
        out["focus"] = _slim_window(foreground) if foreground else None
        out["z_order_top"] = _slim_window(z_order_top) if z_order_top else None
        out["window_order_note"] = WINDOW_ORDER_NOTE
    return out
