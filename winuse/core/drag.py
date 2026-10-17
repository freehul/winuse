"""winuse_drag：拖拽手势（Press-drag-release）——元素到元素 / 坐标到坐标。

驱动侧工具 `drag` 的坐标口径是 **window-local screenshot pixels**（窗口截图的
局部像素）——不是原生桌面坐标。而 `get_window_state` 回的**元素 `frame` 是原生
桌面坐标**。两套口径混用是这类动作最容易踩的坑（点错位置、拖到屏幕外），所以
本模块把换算做成一个**纯函数** `frame_center_in_screenshot`，单独可穷举断言。

坐标口径实测依据（2026-09-27 真机采样，cua-driver 0.28.2）：

1. **`frame` 与 `window_bounds` 同处一个空间**（原生桌面坐标）。实测某窗口
   `window_bounds = (2027, 21, 1813, 2058)`，其 Document 元素
   `frame = (2027, 21, 1814, 2058)` —— 原点一致，可直接相减。
2. **`screenshot_width/height` 才是截图口径**，且**逐窗口不同**。实测：
   普通窗口 `1811/1813 ≈ 0.9989`（截图比窗口小 1~2 px 的边框），
   而铺满桌面的超大窗口 `4096/7080 ≈ 0.5785`（驱动对超大截图做了降采样）。
   → 换算必须用**逐窗口** scale = `screenshot 尺寸 ÷ window_bounds 尺寸`；
     拿全局估值或假设 1.0 都会在降采样/高 DPI 窗口上拖错位置。
3. **没有 screenshot 尺寸就不换算**（fail-closed）：宁可报
   `DRAG_COORD_SPACE_UNKNOWN`，也不拿 `window_bounds` 顶替——那等于假设 scale=1.0。

三条硬规则落地：
- **要么两个元素、要么两个坐标**：混合或缺失一律抛明确错误码（`DRAG_TARGET_MIXED` /
  `DRAG_TARGET_INCOMPLETE`），绝不"猜一半"。
- **默认干跑**：`execute=False` 只回将下发的调用；真动作必须显式开启（与 `core.act` 同口径）。
- **不抢焦点**：显式 `delivery_mode: background`（驱动文档写明 background 是**必须的
  第一次尝试**，只有驱动回 `background_unavailable` 时才升到 foreground——例如从标题栏
  起手的拖拽要走 OS 的移动/缩放循环，只能在真实指针输入下完成）。

复用而非重写（AGENTS.md「业务逻辑只写在核心」，多份实现必然漂移）：
审计落盘封装、驱动拒绝归一（`ok=false` / `refused` / 空回执三形态）直接取自
`core.act` 的同一份实现。
"""

from __future__ import annotations

from typing import Any

from ..driver.errors import MissingTarget, SnapshotRequired, WinUseError
# 刻意复用 act 的收纳/归一辅助：审计落盘与"驱动没成功"的归一必须全项目一份
# （两份必然漂移，而漂移的表现是"某个门面把没做成的动作报告成做成了"）。
from .act import _audit, _driver_refusal, _mark_audit, _token_index
from . import guard as guard_mod

#: 驱动工具名
DRAG_TOOL = "drag"

#: 允许的驱动侧可选项（**额外字段一律拒**：拼错的字段被静默忽略比报错更危险）
DRAG_OPTION_FIELDS = frozenset(
    {
        "duration_ms",  # 手势的墙钟预算（驱动默认 500）
        "steps",  # 中间 WM_MOUSEMOVE 事件数（驱动默认 20）
        "button",  # left / right / middle
        "modifier",  # 手势期间按住的修饰键
        "delivery_mode",  # background（默认，不抢焦点）/ foreground
        "scope",  # window（默认）/ desktop
        "from_zoom",  # 坐标取自上一次 zoom 图时为 true
        "session",  # 多调用工作流的公共会话标号
    }
)

#: 目标模式
ELEMENT_MODE = "element"
POINT_MODE = "point"

#: 读状态时给驱动的元素上限：按索引定位目标必须让目标落在返回的这一片里
DRAG_STATE_MAX_ELEMENTS = 5000


class DragError(WinUseError):
    """拖拽相关的错误基类。"""

    code = "DRAG_ERROR"


class DragTargetMixed(DragError):
    """一半给元素、一半给坐标——两套口径不能混用（坐标空间不同，混着算必错）。"""

    code = "DRAG_TARGET_MIXED"


class DragTargetIncomplete(DragError):
    """目标不成对（只给了一端，或什么都没给）——缺什么补什么，不许猜另一端。"""

    code = "DRAG_TARGET_INCOMPLETE"


class DragTargetInvalid(DragError):
    """目标形状不对（既不是元素引用也不是坐标点）。"""

    code = "DRAG_TARGET_INVALID"


class DragOptionUnknown(DragError):
    """`spec_options` 里含驱动不认识的字段。"""

    code = "DRAG_OPTION_UNKNOWN"


class DragCoordinateSpaceUnknown(DragError):
    """拿不到截图坐标空间（缺 frame / window_bounds / screenshot 尺寸）——fail-closed。"""

    code = "DRAG_COORD_SPACE_UNKNOWN"


class DragElementNotFound(DragError):
    """元素引用在当前状态树里找不到（换过快照 / 传了过期令牌）。"""

    code = "DRAG_ELEMENT_NOT_FOUND"


# --------------------------------------------------------------------------- 纯函数


def _positive(value: Any) -> float | None:
    """取一个 > 0 的数（尺寸类字段）；取不到返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if float(value) > 0 else None


def frame_center_in_screenshot(
    frame: dict | None,
    window_bounds: dict | None,
    screenshot_width: Any,
    screenshot_height: Any,
) -> tuple[float, float]:
    """**纯函数**：元素 `frame`（原生桌面坐标）的中心 → window-local screenshot 像素。

    换算（逐窗口取值，绝不用全局 scale）：

        scale_x = screenshot_width  ÷ window_bounds.width
        scale_y = screenshot_height ÷ window_bounds.height
        local_x = (frame.x + frame.w/2 − window_bounds.x) × scale_x
        local_y = (frame.y + frame.h/2 − window_bounds.y) × scale_y

    为什么是"先减窗口原点、再乘 scale"：`frame` 与 `window_bounds` 同在原生桌面
    空间（实测同原点），相减得到**窗口内原生偏移**；再乘逐窗口 scale 才落到截图口径。

    任何一个输入缺失/非法都抛 `DRAG_COORD_SPACE_UNKNOWN`——**不猜**。特别是
    screenshot 尺寸：没有它就只能假设 scale=1.0，而降采样窗口实测是 0.5785，
    猜错就等于拖到别处。
    """
    if not isinstance(frame, dict) or not isinstance(window_bounds, dict):
        raise DragCoordinateSpaceUnknown(
            "缺少元素 frame 或窗口 bounds，无法把元素中心换算到截图坐标",
            "改用坐标形式（from_point/to_point）直接给截图局部像素",
        )
    width = _positive(window_bounds.get("width"))
    height = _positive(window_bounds.get("height"))
    shot_w = _positive(screenshot_width)
    shot_h = _positive(screenshot_height)
    if width is None or height is None or shot_w is None or shot_h is None:
        raise DragCoordinateSpaceUnknown(
            "缺少窗口尺寸或截图尺寸，无法换算坐标口径（不假设 scale=1.0）",
            "确认窗口未最小化，且取状态时带 include_screenshot=true（截图尺寸来自它）",
        )

    for key in ("x", "y", "w", "h"):
        value = frame.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DragCoordinateSpaceUnknown(
                f"元素 frame 缺少数值字段 {key}",
                "驱动未回该元素的像素坐标；改用坐标形式或换用其他元素",
            )

    scale_x = shot_w / width
    scale_y = shot_h / height
    origin_x = float(window_bounds.get("x") or 0)
    origin_y = float(window_bounds.get("y") or 0)
    center_x = float(frame["x"]) + float(frame["w"]) / 2.0
    center_y = float(frame["y"]) + float(frame["h"]) / 2.0
    return (center_x - origin_x) * scale_x, (center_y - origin_y) * scale_y


def coordinate_space(
    window_bounds: dict | None, screenshot_width: Any, screenshot_height: Any
) -> dict:
    """**纯函数**：把这次读数用到的坐标空间摊平（逐窗口 scale + 原始尺寸）。

    同一份尺寸既用于换算、也放进回执，调用方才能复核"拖到哪了、按什么比例算的"。
    缺少窗口尺寸或截图尺寸一律 `DRAG_COORD_SPACE_UNKNOWN`（不假设 scale=1.0：
    实测降采样窗口是 0.5785，猜 1.0 会拖到别处）。
    """
    if not isinstance(window_bounds, dict):
        raise DragCoordinateSpaceUnknown(
            "驱动没回 window_bounds，无法确定截图坐标空间",
            "确认目标窗口存在且未最小化；或改用坐标形式（from_point/to_point）",
        )
    width = _positive(window_bounds.get("width"))
    height = _positive(window_bounds.get("height"))
    shot_w = _positive(screenshot_width)
    shot_h = _positive(screenshot_height)
    if width is None or height is None or shot_w is None or shot_h is None:
        raise DragCoordinateSpaceUnknown(
            "缺少窗口尺寸或截图尺寸，无法换算坐标口径（不假设 scale=1.0）",
            "确认窗口未最小化，且取状态时带 include_screenshot=true（截图尺寸来自它）",
        )
    return {
        "screenshot_width": shot_w,
        "screenshot_height": shot_h,
        "window_bounds": window_bounds,
        "scale_x": round(shot_w / width, 6),
        "scale_y": round(shot_h / height, 6),
    }


def _as_point(value: Any, label: str) -> tuple[float, float]:
    """坐标点归一：字典 `{\"x\",\"y\"}` 或二元序列（tuple/list）。"""
    if isinstance(value, dict):
        x, y = value.get("x"), value.get("y")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        x, y = value[0], value[1]
    else:
        raise DragTargetInvalid(
            f"{label} 不是坐标点（收到 {type(value).__name__}）",
            "坐标写法：{\"x\": 10, \"y\": 20} 或 (10, 20)",
        )
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        raise DragTargetInvalid(
            f"{label} 的 x/y 必须是数字",
            "坐标写法：{\"x\": 10, \"y\": 20} 或 (10, 20)",
        )
    return float(x), float(y)


def _as_element_ref(value: Any, label: str) -> tuple[int | None, str | None]:
    """元素引用归一 → `(index, token)`（二选一）。接受 int / str / dict。"""
    if isinstance(value, bool):
        raise DragTargetInvalid(
            f"{label} 不是元素引用（收到 bool）",
            "元素写法：索引 3 / 令牌 \"s00000001:3\" / {\"index\": 3} / {\"token\": \"s00000001:3\"}",
        )
    if isinstance(value, int):
        return value, None
    if isinstance(value, str):
        return None, value
    if isinstance(value, dict):
        index = value.get("element_index", value.get("index"))
        token = value.get("element_token", value.get("token"))
        if isinstance(index, int) and not isinstance(index, bool):
            return index, None
        if isinstance(token, str):
            return None, token
    raise DragTargetInvalid(
        f"{label} 不是元素引用（收到 {type(value).__name__}）",
        "元素写法：索引 3 / 令牌 \"s00000001:3\" / {\"index\": 3} / {\"token\": \"s00000001:3\"}",
    )


def validate_options(spec_options: dict | None) -> dict:
    """校验驱动侧可选项：非 dict 或含未知字段一律抛。"""
    if spec_options is None:
        return {}
    if not isinstance(spec_options, dict):
        raise DragOptionUnknown(
            f"spec_options 必须是字典，收到 {type(spec_options).__name__}",
            "可用字段：" + "、".join(sorted(DRAG_OPTION_FIELDS)),
        )
    unknown = sorted(set(spec_options) - DRAG_OPTION_FIELDS)
    if unknown:
        raise DragOptionUnknown(
            "spec_options 含驱动不认识的字段：" + "、".join(unknown),
            "可用字段：" + "、".join(sorted(DRAG_OPTION_FIELDS)),
        )
    return dict(spec_options)


def build_drag_call(
    target: dict,
    from_xy: tuple[float, float],
    to_xy: tuple[float, float],
    spec_options: dict | None = None,
) -> dict:
    """**纯函数**：组装 `drag` 的驱动参数（坐标取整到截图像素）。

    驱动 schema 是 `additionalProperties: false`，所以这里**只**放驱动认识的字段：
    `pid` / `window_id` / `from_x` / `from_y` / `to_x` / `to_y` + 可选项。
    元素索引/令牌**绝不**带进 drag 调用——坐标已经换算好了，带进去只会被驱动拒。
    """
    options = validate_options(spec_options)
    args: dict[str, Any] = {
        "pid": target.get("pid"),
        "window_id": target.get("window_id"),
        "from_x": int(round(from_xy[0])),
        "from_y": int(round(from_xy[1])),
        "to_x": int(round(to_xy[0])),
        "to_y": int(round(to_xy[1])),
    }
    args.update(options)
    # 不抢焦点：驱动默认即 background，这里**显式**写出来，让干跑回执自解释；
    # 调用方要用 foreground 必须自己在 spec_options 里显式覆盖。
    args.setdefault("delivery_mode", "background")
    return args


# --------------------------------------------------------------------------- 元素定位


def _element_index_of(element: dict) -> int | None:
    """取元素序号（驱动原始回执叫 `element_index`，core.state 投影成 `index`）。"""
    index = element.get("element_index")
    return index if index is not None else element.get("index")


def _element_token_of(element: dict) -> str | None:
    """取元素令牌（驱动原始回执叫 `element_token`，core.state 投影成 `token`）。"""
    return element.get("element_token") or element.get("token")


def find_element(elements: list[dict], index: int | None, token: str | None) -> dict | None:
    """在状态树里定位元素（与 core.act 的 `_match_element` 同口径）。

    两条路径，顺序刻意如此：

    1. **令牌完全一致**优先——最精确。
    2. 令牌对不上时，退化为**令牌里编码的序号**（`s00000072:3` → 3）。

    为什么必须有第 2 条（实测踩到）：驱动的 `element_token` 前缀**带快照标识**，
    本模块为了拿坐标空间必然要读一次状态，这次读会刷新快照——调用方手上的令牌
    前缀随即失效（就是 act.py 记录过的 `stale_element_token` 现象）。只按"完全一致"
    找，等于把**所有**用令牌的调用都判死。序号在换快照后保持不变（act.py 同款兜底）。

    残余风险（与 act.py 一致）：两次读之间界面结构变了，可能认到同序号的另一个控件。
    """
    if token is not None:
        for element in elements:
            if _element_token_of(element) == token:
                return element
        index = _token_index(token)  # 兜底：令牌前缀已过期时按编码序号认
        if index is None:
            return None
    for element in elements:
        if _element_index_of(element) == index:
            return element
    return None


# --------------------------------------------------------------------------- 业务


def _classify(
    from_element: Any, to_element: Any, from_point: Any, to_point: Any
) -> tuple[str, Any, Any]:
    """判定目标模式：两个元素 / 两个坐标。混合或不成对**当场抛**，不猜。"""
    has_element = from_element is not None or to_element is not None
    has_point = from_point is not None or to_point is not None

    if has_element and has_point:
        raise DragTargetMixed(
            "拖拽目标混用了元素与坐标",
            "要么 from_element+to_element，要么 from_point+to_point——"
            "不要一半元素一半坐标（两套坐标空间不同）",
        )
    if has_element:
        if from_element is None or to_element is None:
            raise DragTargetIncomplete(
                "元素拖拽必须同时给起点与终点元素（只给了一端）",
                "补全 from_element 与 to_element；不确定另一端就改用坐标形式",
            )
        return ELEMENT_MODE, from_element, to_element
    if has_point:
        if from_point is None or to_point is None:
            raise DragTargetIncomplete(
                "坐标拖拽必须同时给起点与终点坐标（只给了一端）",
                "补全 from_point 与 to_point（window-local screenshot 像素）",
            )
        return POINT_MODE, from_point, to_point
    raise DragTargetIncomplete(
        "拖拽必须指定目标：两个元素或两个坐标",
        "元素形式 from_element/to_element，或坐标形式 from_point/to_point",
    )


def _require_window(target: dict) -> None:
    """拖拽坐标是**窗口局部**口径，缺 pid/window_id 无从谈起。"""
    if target.get("window_id") is None or target.get("pid") is None:
        raise MissingTarget(
            "拖拽需要窗口（pid + window_id）——坐标是窗口截图局部像素",
            "先用 winuse_state / winuse_window 选定目标窗口，再把 pid/window_id 传进来",
        )


def _resolve_elements(driver, target: dict, from_ref: Any, to_ref: Any) -> tuple[dict, tuple[float, float], tuple[float, float], dict]:
    """元素模式：读一次状态 → 定位两个元素 → 换算成截图局部坐标。

    为什么要**读一次状态**：元素 `frame` 是活的（原生桌面坐标），驱动没有
    "按旧快照读 frame" 的接口。这次读同时也给出截图尺寸——它才是坐标口径的定义者，
    所以只能 `include_screenshot=true`（截图本体丢弃，绝不进回执）。
    """
    raw = driver.call(
        "get_window_state",
        {
            "pid": target.get("pid"),
            "window_id": target.get("window_id"),
            "include_screenshot": True,
            "max_elements": DRAG_STATE_MAX_ELEMENTS,
        },
    )
    raw = raw if isinstance(raw, dict) else {}
    elements = [e for e in (raw.get("elements") or []) if isinstance(e, dict)]

    index_a, token_a = _as_element_ref(from_ref, "from_element")
    index_b, token_b = _as_element_ref(to_ref, "to_element")

    from_el = find_element(elements, index_a, token_a)
    if from_el is None:
        raise DragElementNotFound(
            "状态树里找不到起点元素" + (f"（令牌 {token_a}）" if token_a else f"（序号 {index_a}）"),
            "元素索引绑定快照——重新取一次状态再用新的索引/令牌；拖拽按序号定位有换快照后认错元素的残余风险",
        )
    to_el = find_element(elements, index_b, token_b)
    if to_el is None:
        raise DragElementNotFound(
            "状态树里找不到终点元素" + (f"（令牌 {token_b}）" if token_b else f"（序号 {index_b}）"),
            "元素索引绑定快照——重新取一次状态再用新的索引/令牌",
        )

    bounds = raw.get("window_bounds")
    shot_w, shot_h = raw.get("screenshot_width"), raw.get("screenshot_height")
    # 先验证坐标空间（缺尺寸就 fail-closed），再换算，避免"假设 scale=1.0"
    space = coordinate_space(bounds, shot_w, shot_h)
    space["snapshot_id"] = raw.get("snapshot_id")
    from_xy = frame_center_in_screenshot(from_el.get("frame"), bounds, shot_w, shot_h)
    to_xy = frame_center_in_screenshot(to_el.get("frame"), bounds, shot_w, shot_h)
    return from_el, from_xy, to_xy, space


def run_drag(
    driver,
    *,
    target: dict,
    from_element: Any = None,
    to_element: Any = None,
    from_point: Any = None,
    to_point: Any = None,
    snapshot_id: str | None = None,
    spec_options: dict | None = None,
    execute: bool = False,
    confirm: bool = False,
    audit=None,
) -> dict:
    """执行（或干跑）一次拖拽。

    两种目标形态（**二选一，不许混合**）：

    - **元素到元素**：`from_element` / `to_element`（索引或令牌）→ 读一次窗口状态，
      取两个元素 `frame` 的中心，按**逐窗口** scale 换算到 window-local screenshot
      像素（见 `frame_center_in_screenshot`）。
    - **坐标到坐标**：`from_point` / `to_point` —— 直接透传（已是截图局部像素）。

    守门：混合/缺端 → `DRAG_TARGET_MIXED` / `DRAG_TARGET_INCOMPLETE`；
    形状不对 → `DRAG_TARGET_INVALID`；索引引用缺 `snapshot_id` → `SNAPSHOT_REQUIRED`
    （与 `core.act` 同一条纪律）；系统级窗口未确认 → 拒发并要求 `confirm`。

    干跑语义（与 `core.act` 一致）：`execute=False` **不下发任何动作**，只回
    `would_call`。坐标模式**连读都不做**；元素模式需要读一次状态才能算出坐标，
    因此干跑会做**一次只读**的 `get_window_state`（绝不含任何动作）——回执里
    `coordinate_space` 会说明这次读的来源。

    返回：`{"ok", "action", "dry_run", "target", "coordinate_mode", "from", "to",
    "tool", "snapshot_id", ...}`；干跑附 `would_call`，真发附 `args` + `result`。
    """
    target = target or {}
    _require_window(target)

    mode, from_ref, to_ref = _classify(from_element, to_element, from_point, to_point)
    options = validate_options(spec_options)

    # 索引绑定快照（与 core.act 同纪律）：拿旧索引去拖，可能拖到同序号的另一个控件
    if mode == ELEMENT_MODE and snapshot_id is None:
        for ref, label in ((from_ref, "from_element"), (to_ref, "to_element")):
            # 行尾 datascan:ok：这一行的形状会被 pre-commit 的「凭据赋值」启发式命中
            # （`token = <12+ 字符标识符>`，而右侧只是个函数名），属假阳性——用行内豁免，
            # 不为了绕门禁把代码写成别扭的形状
            index, token = _as_element_ref(ref, label)  # datascan:ok
            if token is None:
                raise SnapshotRequired(
                    f"按元素索引拖拽必须带同一份快照的 snapshot_id（{label} 给的是索引 {index}）",
                    "先调 winuse_state 取 snapshot_id，再用它返回的索引；"
                    "或改用元素令牌（element_token，自带快照信息）",
                )

    base: dict[str, Any] = {
        "action": DRAG_TOOL,
        "dry_run": not execute,
        "target": {"pid": target.get("pid"), "window_id": target.get("window_id")},
        "coordinate_mode": mode,
    }

    # 安全层：系统级窗口上的拖拽（任务栏/开始菜单/设置页）可能触发系统级行为
    verdict = guard_mod.assess_action(action=DRAG_TOOL, target=target)
    if verdict.dangerous and not confirm:
        written = _audit(
            audit,
            event="drag.blocked",
            action=DRAG_TOOL,
            ok=False,
            code=verdict.code,
            target=target,
            detail={"danger": verdict.to_dict(), "coordinate_mode": mode},
        )
        return _mark_audit(
            {
                "ok": False,
                **base,
                "requires_confirm": True,
                "danger": verdict.to_dict(),
                "error": {
                    "code": verdict.code,
                    "message": verdict.reason,
                    "detail": "确认方式：CLI 加 --confirm / MCP 传 confirm=true",
                },
            },
            written,
        )

    snapshot_rotated = False
    if mode == POINT_MODE:
        from_xy = _as_point(from_ref, "from_point")
        to_xy = _as_point(to_ref, "to_point")
        space: dict | None = None
    else:
        try:
            _from_el, from_xy, to_xy, space = _resolve_elements(driver, target, from_ref, to_ref)
        except DragError:
            # 本模块自己的守门/换算错误（元素找不到、坐标空间拿不到）是**调用方**该看见的
            # 结果——直接抛，别收成一句 ok=false 混进"驱动失败"里（与 core.act 的分工一致：
            # 组装期错误抛，驱动侧故障回收成结构）。
            raise
        except WinUseError as exc:  # 读状态失败是运行期故障，回收成结构（不穿层）
            written = _audit(
                audit,
                event="drag.failed",
                action=DRAG_TOOL,
                ok=False,
                code=exc.code,
                target=target,
                detail={"stage": "read_state", "coordinate_mode": mode},
            )
            return _mark_audit({**base, "ok": False, "error": exc.to_dict()}, written)
        fresh = space.get("snapshot_id")
        snapshot_rotated = bool(snapshot_id and fresh and fresh != snapshot_id)

    args = build_drag_call(target, from_xy, to_xy, options)

    out: dict[str, Any] = {
        "ok": True,
        **base,
        "tool": DRAG_TOOL,
        "from": {"x": args["from_x"], "y": args["from_y"]},
        "to": {"x": args["to_x"], "y": args["to_y"]},
        "snapshot_id": snapshot_id,
        "args": args,
    }
    if verdict.dangerous:
        out["danger"] = verdict.to_dict()
    if space is not None:
        out["coordinate_space"] = space
        if snapshot_rotated:
            # 读状态必然产生新快照：明说"你给的快照已经不是这次读数用的那份"
            out["snapshot_rotated"] = True
            out["note"] = (
                "元素坐标取自一次新读数（快照已轮换，与你给的 snapshot_id 不同）；"
                "按序号定位在换快照后有认到同序号其它控件的残余风险，建议改用元素令牌"
            )
    if mode == ELEMENT_MODE:
        out["coordinate_source"] = "get_window_state(frame 中心按逐窗口 scale 换算)"

    if not execute:
        out.pop("args")
        out["would_call"] = {"tool": DRAG_TOOL, "args": args}
        out.setdefault("note", "干跑：未下发任何动作（加 --execute 才真实执行）")
        written = _audit(
            audit,
            event="drag.dry_run",
            action=DRAG_TOOL,
            ok=True,
            target=target,
            detail={"tool": DRAG_TOOL, "coordinate_mode": mode, "confirmed": bool(confirm)},
        )
        return _mark_audit(out, written)

    try:
        out["result"] = driver.call(DRAG_TOOL, args)
    except WinUseError as exc:
        out["ok"] = False
        out["error"] = exc.to_dict()
    else:
        # 驱动可能"不抛异常但拒绝"（如从标题栏起手 → background_unavailable）：
        # 必须在 ok 上体现，并给出升到 foreground 的明确下一步
        refusal = _driver_refusal(out["result"])
        if refusal is not None:
            out["ok"] = False
            out["error"] = _with_escalation_hint(refusal)
    written = _audit(
        audit,
        event="drag.executed",
        action=DRAG_TOOL,
        ok=bool(out["ok"]),
        code=(out.get("error") or {}).get("code"),
        target=target,
        detail={
            "tool": DRAG_TOOL,
            "coordinate_mode": mode,
            "confirmed": bool(confirm),
            "dry_run": False,
        },
    )
    return _mark_audit(out, written)


def _with_escalation_hint(refusal: dict) -> dict:
    """给"后台投递送不到"的拒绝补一条可执行的下一步（驱动文档口径）。

    背景：从标题栏 / 缩放边框起手的拖拽（等于移动或缩放窗口本身）走的是 OS 的
    移动/缩放循环，**只能在真实指针输入下完成**，后台投递必然失败。驱动为此回
    `background_unavailable`——此时才该显式升到 `foreground`（且**只**该此时）。
    """
    code = str(refusal.get("code") or "")
    if "BACKGROUND_UNAVAILABLE" not in code.upper():
        return refusal
    return {
        **refusal,
        "detail": (
            str(refusal.get("detail") or "")
            + "；该拖拽起点落在标题栏/缩放边框（等于移动或缩放窗口本身），"
            "后台投递送不到——确需继续时在 spec_options 里显式给 "
            'delivery_mode="foreground"（会短暂切换前台，驱动会还原）'
        ).strip("；"),
    }


__all__ = [
    "DRAG_OPTION_FIELDS",
    "DRAG_STATE_MAX_ELEMENTS",
    "DRAG_TOOL",
    "ELEMENT_MODE",
    "POINT_MODE",
    "DragCoordinateSpaceUnknown",
    "DragElementNotFound",
    "DragError",
    "DragOptionUnknown",
    "DragTargetIncomplete",
    "DragTargetInvalid",
    "DragTargetMixed",
    "build_drag_call",
    "coordinate_space",
    "find_element",
    "frame_center_in_screenshot",
    "run_drag",
    "validate_options",
]
