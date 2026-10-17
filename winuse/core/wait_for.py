"""winuse_wait_for：条件等待（轮询到条件成立，或超时返回结果）。

设计原则落点（与 act.py / state.py 同源）：
1. **超时是一种结果，不是异常**：agent 需要读着「没等到」自己决定重试/放弃/换法。
   让时间流逝把异常穿透整条调用栈没有意义，所以超时返回 `condition_met=False` +
   `code=WAIT_TIMEOUT`，而 `ok` 仍为 True（这次等待调用本身是成功的）。
2. **参数错误照样抛**：kind 拼错、缺 pattern、缺选取条件属于编程错误，当场可见。
3. **时间是注入的**：`time.sleep` / `time.monotonic` 默认真实现，测试注入假实现——
   否则单测会真等十几秒（AGENTS.md：单测不得依赖真实时钟与真实桌面）。
4. **窗口选取复用 state 的语义**：window_id > pid > app > 前台判定，交给
   `state.pick_window`，本模块不另写一份（多份实现必然漂移）。
5. **轮询次数有硬上界**：即便调用方注入的单调时钟不动，循环也不会无限跑。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import limit as limit_mod
from ..driver.errors import (
    InvalidParams,
    MissingParam,
    MissingTarget,
    ParamOutOfRange,
    SnapshotRequired,
    WinUseError,
    WindowAmbiguous,
    WindowNotFound,
)
from .state import StateOptions, pick_window
# 元素身份归一（序号/令牌）复用 treehash 的一份实现：跨快照认同一个元素只有"序号"
# 稳定，这条规则若在等待族与哈希/diff 各写一份，必然漂移成"还在"被报成"消失"。
from .treehash import element_index_of, element_token_of, token_index

#: 支持的等待条件
KINDS = (
    "window_appear",
    "window_gone",
    "title_match",
    "element_appear",
    # 等元素**消失**：驱动 0.28.2 的 verify_state 明确拒绝 element.exists=false
    # （只接受 true），所以"没了"这件事没法交给驱动判，只能在核心侧轮询元素树。
    "element_gone",
    "text_appear",
    # 纯延时（T-64）：无条件等 N 秒。对照物 windows-mcp / computer_use 都有这个形态，
    # 调用方用它做「等动画结束 / 等落盘」的粗同步。它不碰驱动、不轮询，只受时长上限保护。
    "sleep",
)

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_INTERVAL_SECONDS = 0.5

#: 超时码：等待没等到——调用方据它分支，而不是靠抓异常
WAIT_TIMEOUT = "WAIT_TIMEOUT"

#: 元素摘要里的标签截断长度（省 token，且与 state.py 的口径一致）
LABEL_MAX_CHARS = 120

#: 轮询次数硬上界（防注入时钟不动导致死循环）
MAX_POLLS = 10_000


class UnknownWaitKind(WinUseError):
    """等待条件名不在支持清单里。"""

    code = "UNKNOWN_WAIT_KIND"


@dataclass
class WaitSpec:
    """一次条件等待的完整描述（门面把它翻译成这个，业务逻辑只在这里）。

    app / pid / window_id 是**选取条件**（语义与 `state.pick_window` 一致）；
    title_match / element_appear / text_appear 还要靠 `pattern` 做子串匹配
    （不区分大小写）。

    `element_index` / `element_token` / `pattern` 三者是 element_gone 的**元素寻址**：
    三选一，按"明确度"取优先（令牌 > 索引 > 文本子串），见 `_element_address`。
    """

    kind: str
    app: str | None = None
    pid: int | None = None
    window_id: int | None = None
    pattern: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    #: 元素索引（element_gone 用）——与 act.py 同一条不变量：索引只与**同源快照**一起有效
    element_index: int | None = None
    #: 元素令牌（element_gone 用）——形如 `s00000001:0`，跨快照只有序号部分稳定
    element_token: str | None = None
    #: 元素索引的同源快照标识（缺了就是参数错，见 `_validate_element_gone`）
    snapshot_id: str | None = None
    #: sleep 用：要等的秒数（纯延时，不碰驱动、不轮询）
    seconds: float | None = None


def _default_sleep(seconds: float) -> None:
    """默认休眠：真实现（测试注入即时返回的假实现）。"""
    time.sleep(seconds)


def _default_monotonic() -> float:
    """默认单调时钟：真实现（测试注入假时钟）。"""
    return time.monotonic()


# ------------------------------------------------------------------ 小工具


def _truncate(text: Any, limit: int = LABEL_MAX_CHARS) -> Any:
    if not isinstance(text, str) or limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _window_digest(w: dict) -> dict:
    """窗口摘要：字段口径与 `state._slim_window` 对齐（上层不用记两套）。"""
    return {
        "app_name": w.get("app_name"),
        "pid": w.get("pid"),
        "window_id": w.get("window_id"),
        "title": w.get("title"),
        "minimized": w.get("minimized"),
        "on_screen": w.get("is_on_screen"),
        "z_index": w.get("z_index"),
    }


def _element_digest(e: dict) -> dict:
    return {
        "index": e.get("element_index"),
        "token": e.get("element_token"),
        "role": e.get("role"),
        "label": _truncate(e.get("label")),
        "value": _truncate(e.get("value")),
    }


def _has_selector(spec: WaitSpec) -> bool:
    return spec.window_id is not None or spec.pid is not None or bool(spec.app)


def _matches_selector(w: dict, spec: WaitSpec) -> bool:
    """窗口是否被选取条件命中（未给条件 = 命中一切）。"""
    if spec.window_id is not None:
        return w.get("window_id") == spec.window_id
    if spec.pid is not None:
        return w.get("pid") == spec.pid
    if spec.app:
        needle = spec.app.strip().lower()
        return needle in str(w.get("app_name") or "").lower() or needle in str(w.get("title") or "").lower()
    return True


def _list_windows(driver) -> list[dict]:
    resp = driver.call("list_windows", {})
    windows = resp.get("windows") if isinstance(resp, dict) else resp
    return [w for w in (windows or []) if isinstance(w, dict)]


def _pick_target(windows: list[dict], spec: WaitSpec) -> dict | None:
    """复用 state.pick_window 的选取语义；找不到就是「还没出现」，交回 None。

    `WindowAmbiguous` 刻意**不吞**：选取条件不足以判定目标是调用方的参数问题，
    继续等下去也不会变清楚，必须当场看见。
    """
    opts = StateOptions(
        app=spec.app,
        pid=spec.pid,
        window_id=spec.window_id,
        allow_minimized=True,  # 等待只看存在性与文本，最小化不影响判定
        include_windows=False,
    )
    try:
        return pick_window(windows, opts)
    except WindowNotFound:
        return None


def _hit_element(elements: list[dict], kind: str, pattern: str) -> dict | None:
    """在原始元素列表里找第一个命中项（不裁剪——纯文本元素也要能等）。"""
    for e in elements or []:
        if not isinstance(e, dict) or e.get("enabled") is False:
            continue
        label = str(e.get("label") or "").lower()
        if kind == "element_appear":
            if pattern in label or pattern in str(e.get("role") or "").lower():
                return e
        else:  # text_appear：label 或 value 都算文本
            if pattern in label or pattern in str(e.get("value") or "").lower():
                return e
    return None


# ------------------------------------------------------------------ 探测


def _probe_window_kind(driver, spec: WaitSpec) -> tuple[bool, dict]:
    windows = _list_windows(driver)
    selected = [w for w in windows if _matches_selector(w, spec)]
    pattern = (spec.pattern or "").strip().lower()

    if spec.kind == "window_gone":
        met = not selected
        matched = selected
    elif spec.kind == "title_match":
        matched = [w for w in selected if pattern in str(w.get("title") or "").lower()]
        met = bool(matched)
    else:  # window_appear：给了 pattern 就同时看标题与进程名
        matched = [
            w
            for w in selected
            if not pattern
            or pattern in str(w.get("title") or "").lower()
            or pattern in str(w.get("app_name") or "").lower()
        ]
        met = bool(matched)

    final: dict[str, Any] = {
        "windows_total": len(windows),
        "selected": len(selected),
        "matched": len(matched),
        "window": _window_digest(matched[0]) if matched else None,
    }
    return met, final


def _probe_element_kind(driver, spec: WaitSpec) -> tuple[bool, dict]:
    windows = _list_windows(driver)
    target = _pick_target(windows, spec)
    if target is None:
        return False, {
            "windows_total": len(windows),
            "reason": "目标窗口尚未出现",
            "target": None,
            "element": None,
        }

    args = {
        "pid": target.get("pid"),
        "window_id": target.get("window_id"),
        "include_screenshot": False,  # 纯文本闭环，不花截图与视觉推理的钱
    }
    raw = driver.call("get_window_state", args) or {}
    elements = [e for e in (raw.get("elements") or []) if isinstance(e, dict)]
    pattern = (spec.pattern or "").strip().lower()
    hit = _hit_element(elements, spec.kind, pattern)

    final = {
        "target": _window_digest(target),
        "snapshot_id": raw.get("snapshot_id"),
        "elements_count": len(elements),
        "matched": 1 if hit else 0,
        "element": _element_digest(hit) if hit else None,
    }
    return hit is not None, final


# --------------------------------------------------- element_gone（等元素消失）


def _element_address(spec: WaitSpec) -> dict:
    """本次等待"在等哪个元素"的寻址依据（回执里必须能看清等的是谁）。

    优先级 **令牌 > 索引 > 文本子串**，与 `core/act.py`（令牌优先于序号）和
    `state.pick_window`（window_id > pid > app）同一种风格：本项目对多个选取条件
    一律按**明确度**取优先，而不是拒绝——多给一个条件表达的是"更明确的意图"，
    不是错误；真缺条件时再报错（`_validate_element_gone`）。
    """
    if spec.element_token:
        return {"by": "element_token", "element_token": spec.element_token}
    if spec.element_index is not None:
        return {
            "by": "element_index",
            "element_index": spec.element_index,
            "snapshot_id": spec.snapshot_id,
        }
    return {"by": "text", "pattern": spec.pattern}


def _validate_element_gone(spec: WaitSpec) -> None:
    """element_gone 的参数守门：全部在碰驱动之前完成。

    为什么"缺参数"必须当场抛而不能表现为超时：调用方在超时里分不清"没等到的元素"
    与"我给错了参数"，只能盲目重试或加长超时——而错在调用方的参数，等多久都不会变对。
    """
    if spec.element_token:
        if token_index(spec.element_token) is None:
            raise InvalidParams(
                f"element_token 形态无法识别：{spec.element_token}",
                "驱动返回的令牌形如 s00000001:0（<快照号>:<序号>）。跨轮询认元素只能靠"
                "序号，认不出序号的令牌会让每个元素都立刻看似「已消失」；"
                "请原样传 winuse_state 返回的 element_token",
            )
    if spec.element_index is not None:
        if isinstance(spec.element_index, bool) or not isinstance(spec.element_index, int):
            raise InvalidParams(
                f"element_index 必须是整数，收到 {type(spec.element_index).__name__}",
                "索引取自 winuse_state 返回的元素 index（0 起）",
            )
        if not spec.snapshot_id:
            raise SnapshotRequired(
                "按元素索引等元素消失必须带同一份快照的 snapshot_id",
                "索引只在与它同源的快照内有效（先调 winuse_state 取 snapshot_id），"
                "否则拿到的是过期索引，认的是别的控件",
            )
    if not (spec.element_token or spec.element_index is not None or (spec.pattern or "").strip()):
        raise MissingTarget(
            "element_gone 必须给出元素寻址方式（三选一）",
            "element_index + snapshot_id（--index 配 --snapshot）、"
            "element_token（--token）、或 pattern 文本子串（--pattern）",
        )


def _match_element_anywhere(elements: list[dict], spec: WaitSpec) -> dict | None:
    """在当前元素树里找「目标元素」；返回 None 表示它**不在了**（条件成立）。

    为什么令牌寻址不能只比令牌字面（这是本条件最容易出的假阳性）：驱动每次读状态
    都会**刷新快照**，令牌前缀（`s00000039:0` 里的 `s00000039`）随之翻新——同一棵
    没变的树，两轮读到的令牌也不相等。于是令牌里跨快照唯一稳定的部分是**序号**，
    匹配退化为「先按令牌完全一致命中（同一份快照内），再按序号认」。

    刻意**不**跳过 `enabled=False` 的元素（与 `_hit_element` 的取舍相反）：那个跳过
    回答的是"能不能用"，这里回答的是"在不在树里"——被禁用的按钮仍然在树里。
    """
    if spec.element_token:
        for e in elements or []:
            if isinstance(e, dict) and element_token_of(e) == spec.element_token:
                return e
        want = token_index(spec.element_token)
        return next(
            (e for e in elements or [] if isinstance(e, dict) and element_index_of(e) == want),
            None,
        )

    if spec.element_index is not None:
        return next(
            (
                e
                for e in elements or []
                if isinstance(e, dict) and element_index_of(e) == spec.element_index
            ),
            None,
        )

    # 文本寻址：label 与 value 都算文本（与 text_appear 同口径），不区分大小写；
    # 任一元素仍带着这个子串 = 还没消失（不只看第一个）。
    pattern = (spec.pattern or "").strip().lower()
    for e in elements or []:
        if not isinstance(e, dict):
            continue
        if pattern in str(e.get("label") or "").lower() or pattern in str(e.get("value") or "").lower():
            return e
    return None


def _probe_element_gone(driver, spec: WaitSpec) -> tuple[bool, dict]:
    """等目标元素从元素树里消失：本轮读到的树里**找不到**它，条件即成立。

    只能在核心侧轮询的理由：驱动 0.28.2 的 `verify_state` 明确拒绝
    `element.exists=false`（只接受 true），"没了"这件事没有驱动侧谓词可用。
    """
    windows = _list_windows(driver)
    target = _pick_target(windows, spec)
    if target is None:
        # 窗口自己没了 → 它里面的元素当然也没了，条件成立。
        # 这与 `_probe_element_kind`（窗口还没出现 = 元素还没出现）**恰好相反**，
        # 因为两个条件的目标状态互为反面；回执里写清 reason，避免调用方把
        # "整窗消失"读成"元素精确地消失于某个仍在的窗口里"。
        return True, {
            "windows_total": len(windows),
            "reason": "目标窗口已不存在——其中的元素随之消失",
            "target": None,
            "snapshot_id": None,
            "elements_count": None,
            "address": _element_address(spec),
            "found": False,
            "element": None,
        }

    # 参数口径与 `_probe_element_kind` 完全一致：纯文本闭环，不要截图
    raw = driver.call(
        "get_window_state",
        {
            "pid": target.get("pid"),
            "window_id": target.get("window_id"),
            "include_screenshot": False,
        },
    ) or {}
    elements = [e for e in (raw.get("elements") or []) if isinstance(e, dict)]
    hit = _match_element_anywhere(elements, spec)

    final = {
        "target": _window_digest(target),
        "snapshot_id": raw.get("snapshot_id"),
        "elements_count": len(elements),
        # 寻址依据要回报：调用方（和排查的人）才知道"消失"说的是哪一个元素。
        # 尤其令牌寻址退化成按序号认时，这是唯一的线索。
        "address": _element_address(spec),
        "found": hit is not None,  # True = 它还在（条件尚未满足）
        "element": _element_digest(hit) if hit else None,
    }
    return hit is None, final


def _probe(driver, spec: WaitSpec) -> tuple[bool, dict]:
    """一次探测：条件是否成立 + 当次状态摘要。"""
    if spec.kind in ("window_appear", "window_gone", "title_match"):
        return _probe_window_kind(driver, spec)
    if spec.kind == "element_gone":
        return _probe_element_gone(driver, spec)
    return _probe_element_kind(driver, spec)


def effective_interval(spec: WaitSpec) -> float:
    """实际使用的轮询间隔（R-21）：零/负间隔会变成**忙等**，抬到最小值。

    为什么不在 `_validate` 里直接拒：`interval=0` 是调用方很自然的写法（"尽快"），
    抬到 50ms 既满足意图又不烧 CPU；但要在回执里说明被抬过。
    """
    return max(float(spec.interval_seconds or 0.0), limit_mod.MIN_WAIT_INTERVAL_SECONDS)


def _validate(spec: WaitSpec) -> None:
    """参数完整性守门：错在调用方，不该以「超时」的形式表现出来。"""
    timeout = float(spec.timeout_seconds or 0.0)
    if timeout > limit_mod.MAX_WAIT_SECONDS:
        raise ParamOutOfRange(
            f"等待超时 {timeout}s 超过上限 {limit_mod.MAX_WAIT_SECONDS}s",
            "长等待会占着服务端不放；要等价效果请轮询调用（每次都在上限内）",
        )
    if spec.kind not in KINDS:
        raise UnknownWaitKind(
            f"不支持的等待条件：{spec.kind}",
            "可用：" + "、".join(KINDS),
        )
    if spec.kind in ("title_match", "element_appear", "text_appear") and not (spec.pattern or "").strip():
        raise MissingParam(
            f"{spec.kind} 需要 pattern",
            "pattern 是标题/文本的子串（不区分大小写），例如 --pattern '保存成功'",
        )
    if spec.kind == "window_gone" and not _has_selector(spec):
        raise MissingTarget(
            "window_gone 必须给出选取条件（window_id / pid / app）",
            "否则语义退化成「所有窗口都消失」，几乎不是本意；请显式指定要等它消失的窗口",
        )
    if spec.kind == "window_appear" and not _has_selector(spec) and not (spec.pattern or "").strip():
        raise MissingTarget(
            "window_appear 至少要给一个选取条件或 pattern",
            "例如 --app notepad 或 --pattern '无标题'",
        )
    if spec.kind == "sleep":
        seconds = float(spec.seconds or 0.0)
        if seconds <= 0:
            raise MissingParam(
                "sleep 需要正数 seconds",
                "纯延时等待要明确等多久（秒），例如 --seconds 1.5",
            )
        if seconds > limit_mod.MAX_WAIT_SECONDS:
            raise ParamOutOfRange(
                f"等待时长 {seconds}s 超过上限 {limit_mod.MAX_WAIT_SECONDS}s",
                "长等待会占着服务端不放；更长的停顿请分次调用",
            )
    if spec.kind == "element_gone":
        _validate_element_gone(spec)


def _max_polls(timeout: float, interval: float) -> int:
    """轮询次数上界：正常路径由 deadline 收口，这里只兜住「时钟不动」的极端情况。"""
    if interval > 0:
        return max(1, min(MAX_POLLS, int(timeout / interval) + 2))
    return MAX_POLLS if timeout > 0 else 1


# ------------------------------------------------------------------ 入口


def wait_for(
    driver,
    spec: WaitSpec,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> dict:
    """轮询直到条件成立或超时。

    超时**不抛异常**：返回 `condition_met=False` + `code=WAIT_TIMEOUT`，让上层 agent
    读到结果再决策。参数错误（kind 非法、缺 pattern / 选取条件、窗口选取歧义）照抛。

    驱动侧调用异常（找不到驱动、超时、返回不可解析）不与「条件没满足」混为一谈，
    但也不中断轮询（一次抖动不该毁掉整次等待），而是记进 `final["last_error"]`。
    """
    _validate(spec)

    sleep = sleep or _default_sleep
    monotonic = monotonic or _default_monotonic

    timeout = max(float(spec.timeout_seconds or 0.0), 0.0)
    interval = effective_interval(spec)

    if spec.kind == "sleep":
        # 纯延时：无条件等 N 秒就返回（不轮询、不碰驱动）。时长上限已在上面的校验里把关，
        # 所以这里不会变成「挂住服务端」的后门。
        started = monotonic()
        sleep(float(spec.seconds))
        return {
            "ok": True,
            "condition_met": True,
            "waited_ms": int(round((monotonic() - started) * 1000)),
            "polls": 0,
            "kind": spec.kind,
            "final": {"reason": "纯延时（无条件等待），未探测任何条件"},
            "code": None,
        }

    started = monotonic()
    deadline = started + timeout
    max_polls = _max_polls(timeout, interval)

    polls = 0
    met = False
    final: dict = {"reason": "尚未探测"}

    while True:
        polls += 1
        try:
            met, final = _probe(driver, spec)
        except WindowAmbiguous:
            raise  # 选取条件不足：等下去也不会变清楚
        except WinUseError as exc:
            met = False
            final = {"reason": "驱动侧调用出错", "last_error": exc.to_dict()}

        if met:
            break

        now = monotonic()
        if now >= deadline or polls >= max_polls:
            break
        sleep(min(interval, max(deadline - now, 0.0)))

    return {
        "ok": True,
        "condition_met": met,
        "waited_ms": int(round((monotonic() - started) * 1000)),
        "polls": polls,
        "kind": spec.kind,
        "final": final,
        "code": None if met else WAIT_TIMEOUT,
    }
