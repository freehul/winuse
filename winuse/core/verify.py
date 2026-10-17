"""winuse_verify：对**一个确切窗口**做确定性断言（封装驱动的 `verify_state`）。

为什么要有它：动作下发之后"看起来动了"不算验证——agent 需要一条**确定性**的回执
（而不是自己截图猜或再读一遍状态树自我安慰）。驱动给的正是这条通道：它在窗口上
按 `expect` 里的谓词采样，返回三态结果。

要点（依据驱动 0.28.2 `describe verify_state` 的原文）：

1. **三态，且 unknown 绝不等于成功**：驱动原文 *"Predicate results are satisfied,
   unsatisfied, or unknown; unknown never implies success."* 并且
   *"absence remains unknown unless the observed search domain is proven exhaustive"*
   —— 元素遍历不保证穷尽，所以「找不到」只能报 unknown。
   本层的映射（关键设计）：
   - `satisfied` → `ok=True` + `verified=True`
   - `unsatisfied` → `ok=True` + `verified=False`（**没断言通过是有效结论**，
     像 `winuse_wait` 的超时一样不该当异常）
   - `unknown` → `ok=False` + `code=VERIFY_UNKNOWN`（**未证实**，fail-closed：
     让 agent 没法把"没验证出来"读成"验证通过"）
2. **`element.exists=false` 被驱动直接拒**（它明说无法证明不存在）——本地先拦，
   错误信息能讲清原因，不把 schema 报错丢给上层。
3. **选择器只认 `role` + `label_contains`**：没有行/列/单元格维度
   （详见项目内部调研报告的 Grid 结论）。
   选择器的**唯一性要求随断言类型变化**——真机实测（0.28.2）：
   - 只断言存在（`{"element": {"selector": {"role": "Button"}}}`）：
     23 个 Button 命中照样 `satisfied`（`observed_json={"matches":23}`）；
   - 一旦要断言**属性**（`enabled` / `selected` / `value_equals`）：
     目标必须唯一命中，否则 `unknown` + `unknown_reason=multi_match`
     （实测 8 个 ListItem + `enabled:true` 正是这个结果，且还会把默认 5 秒超时耗满）。
   所以"按属性断言"要么把选择器收窄到唯一，要么接受 unknown 而不要当成功。
4. **采样是要花时间的**：`stable_samples`（默认 2，最多 5）× `timeout_ms`
   （默认 5000，最多 10000，0 = 只采一次）。所以保留干跑：先把确切调用看清楚，
   再决定要不要花这 5 秒（真机实测一次 satisfied 采了 5 次、5.4 秒）。
5. **只读**：驱动把它分类为 `read_only=true, destructive=false` —— 因此**不设
   confirm 门禁**（它不改任何桌面状态），只保留干跑开关与审计。
6. **文本优先**：默认 `include_screenshot=false`。截图是给多模态调用方兜底的
   视觉证据，驱动自己也不会去解释它（原话："the driver does not interpret that image"）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..driver.errors import InvalidParams, MissingParam, MissingTarget, ParamOutOfRange, WinUseError

# 拒绝语义只有一份实现（`core.act`）——本模块不复制。
from .act import _driver_refusal  # noqa: F401  (re-exported for callers/tests)

#: 驱动工具名 / 对外回执里的动作名
TOOL = "verify_state"
ACTION = "verify"

#: 驱动 schema 上界（`expect`: minItems 1 / maxItems 8）
MAX_PREDICATES = 8
#: 采样与超时的取值域（驱动 schema）
DEFAULT_STABLE_SAMPLES = 2
MIN_STABLE_SAMPLES = 1
MAX_STABLE_SAMPLES = 5
DEFAULT_TIMEOUT_MS = 5000
MIN_TIMEOUT_MS = 0
MAX_TIMEOUT_MS = 10000
#: 窗口几何容差上界（驱动 schema: maximum 100）
MAX_BOUNDS_TOLERANCE_PX = 100

#: 驱动返回的三态
SATISFIED = "satisfied"
UNSATISFIED = "unsatisfied"
UNKNOWN = "unknown"
STATUSES = (SATISFIED, UNSATISFIED, UNKNOWN)

#: 本层的错误码
CODE_UNSATISFIED = "VERIFY_UNSATISFIED"
CODE_UNKNOWN = "VERIFY_UNKNOWN"
CODE_UNSTABLE = "VERIFY_UNSTABLE"

#: 谓词的字段域（与驱动 schema 对齐，本地先拦，别让驱动替我们做参数校验）
PREDICATE_KEYS = ("window", "element")
WINDOW_KEYS = ("exists", "bounds")
BOUNDS_KEYS = ("x", "y", "width", "height", "tolerance_px")
BOUNDS_REQUIRED = ("x", "y", "width", "height")
ELEMENT_KEYS = ("selector", "exists", "enabled", "selected", "value_equals")
SELECTOR_KEYS = ("role", "label_contains")

DRY_RUN_NOTE = "干跑：未下发断言（execute=True 才真采样；采样会花 stable_samples×timeout_ms 的时间）"
EXECUTED_NOTE = "已通过 verify_state 采样断言（只读，未改动被断言窗口）"


def _is_int(value: Any) -> bool:
    """整数判定：`bool` 是 `int` 的子类，但 True/False 当采样次数是明显的调用方错误。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_unknown_keys(where: str, given: Mapping, allowed: Sequence[str]) -> None:
    unknown = [k for k in given if k not in allowed]
    if unknown:
        raise InvalidParams(
            f"{where} 里有不支持的字段：{'、'.join(str(k) for k in unknown)}",
            f"可用字段：{'、'.join(allowed)}",
        )


def _selector(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise InvalidParams("element.selector 要是对象", "例如 {'role': 'Button', 'label_contains': '保存'}")
    _reject_unknown_keys("element.selector", raw, SELECTOR_KEYS)
    out: dict[str, str] = {}
    for key in SELECTOR_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise InvalidParams(f"selector.{key} 要是字符串", "例如 role='Button' / label_contains='保存'")
        if not value.strip():
            raise MissingParam(f"selector.{key} 是空白", "空白条件等于没条件；去掉它或补上真实取值")
        out[key] = value
    if not out:
        raise MissingParam(
            "element.selector 至少要给一个条件（role 或 label_contains）",
            "空选择器会命中大量元素；要断言元素属性（enabled/selected/value_equals）时"
            "驱动只会回 multi_match 的 unknown",
        )
    return out


def _bounds(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise InvalidParams("window.bounds 要是对象", "例如 {'x': 0, 'y': 0, 'width': 800, 'height': 600}")
    _reject_unknown_keys("window.bounds", raw, BOUNDS_KEYS)
    missing = [k for k in BOUNDS_REQUIRED if raw.get(k) is None]
    if missing:
        raise MissingParam(
            f"window.bounds 缺字段：{'、'.join(missing)}",
            "驱动要求 x/y/width/height 四个都给（可加 tolerance_px，0..100）",
        )
    out: dict[str, Any] = {}
    for key in BOUNDS_REQUIRED:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidParams(f"window.bounds.{key} 要是数字", "像素/点坐标，允许小数")
        out[key] = value
    if raw.get("tolerance_px") is not None:
        tol = raw["tolerance_px"]
        if isinstance(tol, bool) or not isinstance(tol, (int, float)):
            raise InvalidParams("window.bounds.tolerance_px 要是数字", f"取值域 0..{MAX_BOUNDS_TOLERANCE_PX}")
        if not 0 <= tol <= MAX_BOUNDS_TOLERANCE_PX:
            raise ParamOutOfRange(
                f"tolerance_px={tol} 超出 0..{MAX_BOUNDS_TOLERANCE_PX}",
                "容差是像素级判定阈值，驱动上限 100",
            )
        out["tolerance_px"] = tol
    return out


def _element(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise InvalidParams("element 要是对象", "例如 {'selector': {'role': 'Button'}, 'enabled': True}")
    _reject_unknown_keys("element", raw, ELEMENT_KEYS)
    if raw.get("selector") is None:
        raise MissingParam(
            "element 谓词必须给 selector",
            "驱动 schema 里 selector 是必填；没有它无法确定断言对象",
        )
    if raw.get("exists") is False:
        raise MissingParam(
            "不支持断言「元素不存在」（exists=false）",
            "驱动原话：元素遍历不保证穷尽，不存在无法证明；它直接拒收 false。"
            "要等元素消失请用 winuse_wait 的 element 类条件",
        )
    for key in ("enabled", "selected"):
        value = raw.get(key)
        if value is not None and not isinstance(value, bool):
            raise InvalidParams(f"element.{key} 要是布尔", "true / false；给 None 表示不关心该项")
    value_equals = raw.get("value_equals")
    if value_equals is not None and not isinstance(value_equals, str):
        raise InvalidParams("element.value_equals 要是字符串", "驱动按字符串比较元素 value")

    out: dict[str, Any] = {"selector": _selector(raw["selector"])}
    for key in ("enabled", "selected", "value_equals"):
        if raw.get(key) is not None:
            out[key] = raw[key]
    # 给了选择器就显式声明"要它存在"：驱动只接受 true，缺省时也按匹配处理，
    # 显式写出来让回执自解释（这条断言要的是存在性，不是别的东西）。
    out["exists"] = True
    return out


def _predicate(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise InvalidParams(
            f"第 {index} 条谓词要是对象（收到 {type(raw).__name__}）",
            "形如 {'window': {'exists': true}} 或 {'element': {'selector': {'role': 'Button'}}}",
        )
    _reject_unknown_keys(f"第 {index} 条谓词", raw, PREDICATE_KEYS)
    if not raw:
        raise MissingParam(f"第 {index} 条谓词是空的", f"至少要给 {' / '.join(PREDICATE_KEYS)} 之一")

    out: dict[str, Any] = {}
    if raw.get("window") is not None:
        window = raw["window"]
        if not isinstance(window, Mapping):
            raise InvalidParams(f"第 {index} 条谓词的 window 要是对象", "例如 {'exists': true}")
        _reject_unknown_keys("window", window, WINDOW_KEYS)
        if not window:
            raise MissingParam(f"第 {index} 条谓词的 window 是空的", "至少要给 exists 或 bounds")
        built: dict[str, Any] = {}
        if window.get("exists") is not None:
            if not isinstance(window["exists"], bool):
                raise InvalidParams("window.exists 要是布尔", "true / false")
            built["exists"] = window["exists"]
        if window.get("bounds") is not None:
            built["bounds"] = _bounds(window["bounds"])
        out["window"] = built
    if raw.get("element") is not None:
        out["element"] = _element(raw["element"])
    return out


def build_verify_call(
    target: dict,
    predicates: Any,
    *,
    include_screenshot: bool = False,
    stable_samples: int = DEFAULT_STABLE_SAMPLES,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> tuple[str, dict[str, Any]]:
    """纯函数：把「目标窗口 + 谓词列表」翻译成驱动 `expect` 参数（本地先过一遍 schema）。"""
    pid, window_id = target.get("pid"), target.get("window_id")
    if pid is None or window_id is None:
        raise MissingTarget(
            "verify_state 需要目标窗口的 pid 与 window_id",
            "断言是相对某个窗口做的：先 winuse_state 选窗口（它会给 pid/window_id）",
        )

    if isinstance(predicates, str) or not isinstance(predicates, Sequence):
        raise MissingParam(
            "谓词要是一个列表（每条一个断言，条与条之间取与）",
            "例如 [{'window': {'exists': true}}]",
        )
    items = list(predicates)
    if not items:
        raise MissingParam("谓词列表不能为空（驱动要求 1..8 条）", "例如 [{'window': {'exists': true}}]")
    if len(items) > MAX_PREDICATES:
        raise ParamOutOfRange(
            f"谓词最多 {MAX_PREDICATES} 条，收到 {len(items)} 条",
            "条与条之间是与关系：想覆盖更多条件就拆成多次断言（或去掉不必要的）",
        )

    if not _is_int(stable_samples):
        raise InvalidParams("stable_samples 要是整数", f"取值域 {MIN_STABLE_SAMPLES}..{MAX_STABLE_SAMPLES}")
    if not MIN_STABLE_SAMPLES <= stable_samples <= MAX_STABLE_SAMPLES:
        raise ParamOutOfRange(
            f"stable_samples={stable_samples} 超出 {MIN_STABLE_SAMPLES}..{MAX_STABLE_SAMPLES}",
            "连续满足多少次采样才算成立；驱动上限 5",
        )
    if not _is_int(timeout_ms):
        raise InvalidParams("timeout_ms 要是整数", f"取值域 {MIN_TIMEOUT_MS}..{MAX_TIMEOUT_MS}")
    if not MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ParamOutOfRange(
            f"timeout_ms={timeout_ms} 超出 {MIN_TIMEOUT_MS}..{MAX_TIMEOUT_MS}",
            "0 = 只采一次；驱动上限 10 秒",
        )
    if not isinstance(include_screenshot, bool):
        raise InvalidParams("include_screenshot 要是布尔", "默认 false：断言走文本闭环")

    expect = [_predicate(raw, idx) for idx, raw in enumerate(items, start=1)]
    return TOOL, {
        "pid": pid,
        "window_id": window_id,
        "expect": expect,
        # 显式下发每个字段（不靠驱动缺省）：干跑回执看着就是实际会发的那一份
        "include_screenshot": include_screenshot,
        "stable_samples": stable_samples,
        "timeout_ms": timeout_ms,
    }


def _observed(raw: Any) -> Any:
    """`observed_json` 是 JSON **字符串**——能解就解成结构给上层用，解不动就留原串。"""
    if not isinstance(raw, str):
        return None
    import json

    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _project_predicates(raw: Any) -> list[dict]:
    out: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        observed_json = item.get("observed_json")
        out.append(
            {
                "index": item.get("index"),
                "status": item.get("status"),
                "unknown_reason": item.get("unknown_reason"),
                "observed": _observed(observed_json),
                "observed_json": observed_json,
            }
        )
    return out


def _failure(code: str, message: str, detail: str = "") -> dict:
    return {
        "ok": False,
        "verified": False,
        "status": UNKNOWN,
        "code": code,
        "error": {"code": code, "message": message, "detail": detail},
    }


def parse_verify_result(result: Any) -> dict:
    """把驱动的断言回执翻成 `{ok, verified, status, code, predicates, ...}`。

    三态映射见模块头 ①；`unknown` 与「自相矛盾的回执」一律 fail-closed。
    """
    if result is None or result == {} or not isinstance(result, dict):
        return _failure(
            "DRIVER_EMPTY_RESPONSE",
            "驱动没有返回任何断言结果，无法确认断言是否成立",
            "空回执按未证实处理；可用 winuse --unsafe-driver-call 看原始返回排查",
        )
    refusal = _driver_refusal(result)
    if refusal is not None:
        return _failure(refusal["code"], refusal["message"], refusal.get("detail") or "")

    status = str(result.get("status") or "").strip().lower()
    stable = result.get("stable")
    predicates = _project_predicates(result.get("predicates"))
    base = {
        "status": status if status in STATUSES else UNKNOWN,
        "samples": result.get("samples"),
        "stable": stable,
        "elapsed_ms": result.get("elapsed_ms"),
        "predicates": predicates,
    }

    if status == SATISFIED:
        if stable is False:
            # 驱动契约里 satisfied 要求 stable_samples 次连续满足；说满足又说没稳定
            # 是自相矛盾的回执 —— 宁可报未证实，也不许把矛盾读成成功。
            # 注意 status 保留**驱动原话**（satisfied），不让本层的结论去改写它：
            # 回执要能同时回答"驱动说了什么"（status）与"我们因此怎么判"（ok/verified）。
            return {
                **base,
                "ok": False,
                "verified": False,
                "code": CODE_UNSTABLE,
                "error": {
                    "code": CODE_UNSTABLE,
                    "message": "驱动回执自相矛盾（status=satisfied 但 stable=false）",
                    "detail": "按 fail-closed 处理：本次不视为验证通过",
                },
            }
        return {"ok": True, "verified": True, **base, "code": None}

    if status == UNSATISFIED:
        return {
            "ok": True,
            "verified": False,
            **base,
            "code": CODE_UNSATISFIED,
            "error": {
                "code": CODE_UNSATISFIED,
                "message": "断言未通过（驱动明确判定 unsatisfied）",
                "detail": "这是有效结论：目标此刻确实不满足该谓词；要等它成立请用 winuse_wait",
            },
        }

    reasons = sorted({str(p.get("unknown_reason")) for p in predicates if p.get("unknown_reason")})
    return {
        **base,
        **_failure(
            CODE_UNKNOWN,
            "断言未能证实（驱动回 status=unknown，未证实不等于成立）",
            "unknown_reason：" + ("、".join(reasons) if reasons else "未给出；常见为 multi_match（选择器不唯一）/ observation_unavailable（观测面不完整）"),
        ),
    }


def _audit(sink, **fields) -> bool | None:
    from ..audit import sink_event

    return sink_event(sink, **fields)


def _mark_audit(out: dict, written: bool | None) -> dict:
    if written is not None:
        out["audit_written"] = written
    return out


def _slim_target(target: dict) -> dict:
    return {"pid": target.get("pid"), "window_id": target.get("window_id")}


def run_verify(
    driver,
    *,
    target: dict,
    predicates: Any,
    execute: bool = False,
    include_screenshot: bool = False,
    stable_samples: int = DEFAULT_STABLE_SAMPLES,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    audit=None,
) -> dict:
    """断言（或干跑）一组谓词。

    只读能力（驱动分类 `read_only=true`），因此**没有 confirm 门禁**：确认语义留给
    改桌面状态的动作。默认干跑只为让调用方先看清将要下发的确切参数（采样会花时间，
    真机实测一次 satisfied 采 5 次、约 5.4 秒）。
    """
    tool, args = build_verify_call(
        target,
        predicates,
        include_screenshot=include_screenshot,
        stable_samples=stable_samples,
        timeout_ms=timeout_ms,
    )

    if not execute:
        written = _audit(
            audit,
            event="verify.dry_run",
            action=ACTION,
            ok=True,
            target=target,
            detail={"tool": tool, "predicates": len(args["expect"]), "timeout_ms": timeout_ms},
        )
        return _mark_audit(
            {
                "ok": True,
                "dry_run": True,
                "action": ACTION,
                "target": _slim_target(target),
                "would_call": {"tool": tool, "args": args, "note": DRY_RUN_NOTE},
                "code": None,
            },
            written,
        )

    out: dict[str, Any] = {
        "dry_run": False,
        "action": ACTION,
        "target": _slim_target(target),
        "tool": tool,
        "args": args,
    }
    try:
        result = driver.call(tool, args)
    except WinUseError as exc:
        out.update({"ok": False, "verified": False, "status": UNKNOWN, "code": exc.code, "error": exc.to_dict()})
    else:
        parsed = parse_verify_result(result)
        out.update(parsed)
        out["result"] = result

    written = _audit(
        audit,
        event="verify.executed",
        action=ACTION,
        ok=bool(out["ok"]),
        code=out.get("code"),
        target=target,
        detail={"tool": tool, "status": out.get("status"), "verified": bool(out.get("verified"))},
    )
    return _mark_audit(out, written)
