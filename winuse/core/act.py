"""winuse_act：把高层动作翻译成驱动的元素级原语（PR-3）。

要点（依据驱动 0.28.2 的实测参数表）：
1. **元素索引必须绑定快照**：驱动不变量是"每 (pid, window_id) 每轮先取一次状态"，
   索引只在同一份快照内有效。缺 `snapshot_id` 直接拒（`SNAPSHOT_REQUIRED`），
   以免拿着过期索引点到别处。
2. **语义动作归一到元素点击**：驱动没有分开的 toggle/select/expand 工具，
   统一走 `click` 的元素索引形式，由驱动按元素能力选 UIA 模式。
3. **默认后台投递**：`delivery_mode: background` —— 不抢前台、不打断用户，
   这也是 text-first 相对截图+坐标的核心优势之一。
4. **滚动是窗口级**：驱动文档写明 `scroll` 的 `element_index` 在 Windows 是 no-op，
   所以不传元素参数，免得让上层以为能定向滚某个子区域。
5. **默认干跑**：`execute=False` 只回"将要下发的调用"，真动作必须显式开启。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..driver.errors import MissingParam, MissingTarget, SnapshotRequired, UnknownAction

#: 高层动作 → 驱动工具
ACTION_TOOLS: dict[str, str] = {
    "click": "click",
    "invoke": "click",
    "toggle": "click",
    "select": "click",
    "expand": "click",
    "set_value": "set_value",
    "type": "type_text",
    "key": "press_key",
    "scroll": "scroll",
    "activate": "bring_to_front",
}

#: 支持后台投递的工具（其余工具没有 delivery_mode 参数，传了会报错）
BACKGROUND_TOOLS = frozenset({"click", "type_text", "press_key", "scroll"})

SCROLL_DIRECTIONS = ("up", "down", "left", "right")

#: 滚动粒度（驱动 `scroll.by` 的取值域）：line=按行（默认）、page=按页
SCROLL_GRANULARITIES = ("line", "page")

#: 鼠标键（驱动 `click.button` 的取值域）——Windows 侧驱动原生接受 right/middle，
#: 门面此前只暴露左键，右键/中键的能力一直躺在核心与 CLI 里没人用得到（P0-1）
MOUSE_BUTTONS = ("left", "right", "middle")


@dataclass
class ActSpec:
    """一个动作的完整描述（门面把它翻译成这个，业务逻辑只在这里）。"""

    action: str
    element_index: int | None = None
    element_token: str | None = None
    value: str | None = None
    text: str | None = None
    key: str | None = None
    modifiers: list[str] = field(default_factory=list)
    direction: str | None = None
    amount: int = 3
    by: str = "line"
    button: str = "left"
    count: int = 1
    #: 坐标寻址（T-63）：窗口内截图像素，左上角原点；与元素寻址**互斥**
    x: int | None = None
    y: int | None = None


def build_act_call(
    spec: ActSpec, target: dict, snapshot_id: str | None
) -> tuple[str, dict[str, Any]]:
    """纯函数：把动作翻译成 (驱动工具名, 参数)。参数不全就抛，不猜。"""
    tool = ACTION_TOOLS.get(spec.action)
    if tool is None:
        raise UnknownAction(
            f"不支持的动作：{spec.action}",
            "可用：" + "、".join(sorted(ACTION_TOOLS)),
        )

    pid, window_id = target.get("pid"), target.get("window_id")

    if spec.action == "activate":
        return tool, {"pid": pid, "window_id": window_id}

    if spec.action == "scroll":
        if spec.direction not in SCROLL_DIRECTIONS:
            raise MissingParam(
                "scroll 需要 direction",
                "可选：" + "、".join(SCROLL_DIRECTIONS),
            )
        if spec.by not in SCROLL_GRANULARITIES:
            raise MissingParam(
                f"scroll 的 by 只支持 {' / '.join(SCROLL_GRANULARITIES)}",
                "line=按行（默认）、page=按页",
            )
        return tool, {
            "pid": pid,
            "window_id": window_id,
            "direction": spec.direction,
            "amount": spec.amount,
            "by": spec.by,
            "delivery_mode": "background",
        }

    # 坐标寻址（T-63）：canvas / 视频 / 自绘控件等「不在元素树里」的目标只能走像素。
    # 坐标是**窗口内截图像素**（左上角原点，与 `winuse_screenshot` 的 window 模式同源），
    # 不是桌面坐标——桌面坐标属于 `winuse_drag` 的 from_point / to_point 口径。
    wants_point = spec.x is not None or spec.y is not None
    if wants_point:
        if spec.x is None or spec.y is None:
            raise MissingParam(
                "坐标寻址要同时给 x 与 y",
                "只给一半等于坐标不完整——另一半不猜。",
            )
        if spec.element_token or spec.element_index is not None:
            raise MissingParam(
                "坐标寻址与元素寻址互斥",
                "二选一：给 element_token/element_index 走元素，或给 x/y 走坐标。",
            )
        if tool != "click":
            raise MissingParam(
                f"{spec.action} 不支持坐标寻址",
                "坐标寻址只用于点击类动作（click / invoke / toggle / select / expand）",
            )

    args: dict[str, Any] = {"pid": pid, "window_id": window_id}
    if wants_point:
        args["x"], args["y"] = spec.x, spec.y
    elif spec.element_token:
        args["element_token"] = spec.element_token
    elif spec.element_index is not None:
        if not snapshot_id:
            raise SnapshotRequired(
                "按元素索引下动作必须带同一份快照的 snapshot_id",
                "先调 winuse_state（其 snapshot_id 字段），再用返回的索引动作",
            )
        args["element_index"] = spec.element_index
        args["snapshot_id"] = snapshot_id
    else:
        raise MissingTarget(
            f"动作 {spec.action} 没有作用对象",
            "请给元素索引（--index）或元素令牌（--token）",
        )

    if tool == "set_value":
        if spec.value is None:
            raise MissingParam("set_value 需要 value", "用法示例：--value '新内容'")
        args["value"] = spec.value
    elif tool == "type_text":
        if spec.text is None:
            raise MissingParam("type 需要 text", "用法示例：--text '要输入的文字'")
        args["text"] = spec.text
    elif tool == "press_key":
        if not spec.key:
            raise MissingParam("key 需要按键名", "例如 return、tab、escape、down")
        args["key"] = spec.key
        if spec.modifiers:
            args["modifiers"] = list(spec.modifiers)
    elif tool == "click":
        if spec.button not in MOUSE_BUTTONS:
            raise MissingParam(
                f"click 的 button 只支持 {' / '.join(MOUSE_BUTTONS)}",
                "left=左键（默认）、right=右键、middle=中键",
            )
        if spec.button != "left":
            args["button"] = spec.button
        if spec.count != 1:
            args["count"] = spec.count
        if spec.modifiers:
            args["modifier"] = list(spec.modifiers)

    if tool in BACKGROUND_TOOLS:
        args["delivery_mode"] = "background"
    return tool, args


def _audit(sink, **fields) -> bool | None:
    """写一条审计事件（薄封装 `audit.sink_event`，保持本模块调用点简洁）。

    返回 `True`/`False`（写了 / 写失败）、`None`（没有 sink）——调用方把它放进回执
    `audit_written`，让"审计是否真的落盘"可观察（R-1）。
    """
    from ..audit import sink_event

    return sink_event(sink, **fields)


def _mark_audit(out: dict, written: bool | None) -> dict:
    """把落盘结果挂进回执：没有 sink 时不加字段（避免污染所有回执形状）。"""
    if written is not None:
        out["audit_written"] = written
    return out


#: 写值类动作：会**整段替换**目标内容（记事本事故的形态），必须过内容门禁
WRITE_ACTIONS = ("set_value",)
GATE_EMPTY = "empty"
GATE_UNVERIFIED = "unverified"
GATE_OVERWRITE = "overwrite_authorized"
#: 双授权（overwrite + allow_unverified）放行 unverified 写入时的状态值：
#: 明说"这一条根本没查门禁"，别让回执看起来像查过了、确认过内容了
GATE_UNVERIFIED_BYPASS = "unverified_bypassed"
EXISTING_CONTENT_CODE = "EXISTING_CONTENT"
#: 写值门禁"确认不了目标内容"时的错误码（R-3：fail-closed）
UNVERIFIED_CODE = "WRITE_GATE_UNVERIFIED"


def _gate_state_args(target: dict) -> dict:
    """取状态的驱动参数（与 core.state 同口径：只要纯树，不要截图）。"""
    args: dict[str, Any] = {"window_id": target.get("window_id"), "include_screenshot": False}
    if target.get("pid") is not None:
        args["pid"] = target["pid"]
    return args


def _element_token(element: dict) -> str | None:
    """取元素令牌——驱动原始回执叫 `element_token`，core.state 投影成 `token`。"""
    return element.get("element_token") or element.get("token")


def _element_index_of(element: dict) -> int | None:
    """取元素序号——驱动原始回执叫 `element_index`，core.state 投影成 `index`。"""
    idx = element.get("element_index")
    return idx if idx is not None else element.get("index")


def _token_index(token: str | None) -> int | None:
    """从令牌里取元素序号（`s00000039:0` → 0）。驱动换快照后前缀会变，序号不变。"""
    if not token or ":" not in token:
        return None
    tail = token.rsplit(":", 1)[1]
    return int(tail) if tail.isdigit() else None


def _match_element(elements: list[dict], spec: ActSpec) -> dict | None:
    """在状态树里定位动作目标：令牌完全一致优先，其次退化为序号。"""
    for element in elements:
        if spec.element_token and _element_token(element) == spec.element_token:
            return element
    want = spec.element_index if spec.element_index is not None else _token_index(spec.element_token)
    if want is None:
        return None
    for element in elements:
        if _element_index_of(element) == want:
            return element
    return None


def check_write_gate(driver, spec: ActSpec, target: dict) -> dict:
    """写值前的"既有内容"门禁——**核心自己查**，不靠调用方自觉。

    背景：记事本事故的门禁当年只写在验收脚本里，产品路径裸奔 →
    对非空编辑框 `set_value` 会整段替换用户草稿。这里把它放进产品路径。

    ⚠️ 驱动的快照是"一次性"的：这次读会刷新当前快照，让调用方手上的
    `element_token` 立刻失效（实测回 `stale_element_token`）。所以判完必须把
    写值调用也切到刚读到的这份快照上（返回 `fresh_token` + `refreshed`，
    由调用方接线）。**不知道这一点就会把门禁写成"把所有正常写值都打死"**。

    判定三态（R-3 之后第三态是**拒绝**，不是放行）：
    - 读到目标元素且内容非空 → blocked（除非显式 overwrite）
    - 读到且为空 → empty（放行；换了快照的话带新令牌回去）
    - 读不到/读取失败 → **unverified → 由调用方 fail-closed**：确认不了目标内容
      就不写。原先放行的理由是"避免误伤正常写值"，但"确认不了"时写入等于赌一把，
      而这个门禁存在的全部意义就是防整段覆盖用户草稿。

      确需写读取不到状态的特殊控件（自绘 / 虚拟列表等）时，走**两把钥匙**的
      高级授权：`allow_unverified=True` **且** `overwrite=True`——单独给
      `allow_unverified` 不够，因为那只表达"我知道可能读不到"，不表达
      "我接受覆盖内容"。

    残余风险（已在 Backlog 记录）：换快照后只能按**序号**认元素，若两次读之间
    界面结构变了，理论上可能认到同序号的另一个控件；但"非空即拦"仍会挡住
    绝大多数误覆盖。
    """
    try:
        raw = driver.call("get_window_state", _gate_state_args(target)) or {}
    except Exception as exc:  # noqa: BLE001 - 拿不准时不阻断，但要留痕
        return {"status": GATE_UNVERIFIED, "reason": f"读取目标元素失败：{exc}"}

    elements = [e for e in (raw.get("elements") or []) if isinstance(e, dict)]
    match = _match_element(elements, spec)
    if match is None:
        return {"status": GATE_UNVERIFIED, "reason": "状态树里没有目标元素，无法确认是否为空"}

    existing = str(match.get("value") or "")
    if existing.strip():
        # 只回长度：**不回正文也不回标签**——控件标签在真实场景常就等于内容本身，
        # 回带等于把用户草稿写进审计/会话日志（样例即泄露）。
        return {"status": "blocked", "blocked": True, "existing_chars": len(existing)}
    fresh = _element_token(match)
    return {
        "status": GATE_EMPTY,
        "existing_chars": 0,
        "fresh_token": fresh,
        "refreshed": bool(fresh) and fresh != spec.element_token,
    }


#: 驱动"没说成功也没说失败"时的兜底错误码（空回执 / null 回执）
EMPTY_RESPONSE_CODE = "DRIVER_EMPTY_RESPONSE"


def _normalize_code(value: Any, fallback: str) -> str:
    """错误码归一：驱动给什么就大写成什么，缺省用 fallback。"""
    text = str(value or "").strip()
    return text.upper() if text else fallback


def _driver_refusal(result: Any) -> dict | None:
    """驱动"不抛异常但没成功"的三种形态，统一成核心错误（R-15）。

    为什么要三种都认：agent 只看 `ok` 字段决定要不要继续，回执形态漏一种，
    就等于把"动作没做"报告成"做完了"——实测踩到过（回 `ok=true` 而回执其实是
    `stale_element_token` 拒绝，控件内容一个字节没变）。

    1. `ok=false`：驱动明说失败（**新增**，原先不认这一形态）
    2. `status="refused"` / `refusal` 字段：驱动用专门字段表达拒绝
    3. `{}` / `null`：驱动什么都没说——"没报错"不等于"成功了"，一律按失败处理

    不认的形态照旧放行（如只回业务字段、不带 `ok` 的回执），避免把正常回执判死。
    """
    if result is None or result == {}:
        return {
            "code": EMPTY_RESPONSE_CODE,
            "message": "驱动没有返回任何内容，无法确认动作是否生效",
            "detail": "回执为空；请联系驱动侧排查，或改用 winuse --unsafe-driver-call 看原始返回",
        }
    if not isinstance(result, dict):
        return None
    if result.get("ok") is False:
        return {
            "code": _normalize_code(result.get("code"), "DRIVER_REFUSED"),
            "message": str(result.get("message") or "驱动回执 ok=false"),
            "detail": "驱动明确报告失败（ok=false）",
        }
    refusal = result.get("refusal")
    if result.get("status") != "refused" and not isinstance(refusal, dict):
        return None
    info = refusal if isinstance(refusal, dict) else {}
    return {
        "code": _normalize_code(info.get("code"), "DRIVER_REFUSED"),
        "message": info.get("message") or "驱动拒绝了该动作",
        "detail": f"驱动回执状态：{result.get('status')}",
    }


def run_act(
    driver,
    spec: ActSpec,
    target: dict,
    snapshot_id: str | None,
    execute: bool = False,
    confirm: bool = False,
    overwrite: bool = False,
    allow_unverified: bool = False,
    audit=None,
) -> dict:
    """执行（或干跑）一个动作。

    参数/守门类错误直接抛（编程错误应当被看见）；驱动侧的失败回收成 `ok: false`，
    让上层 agent 能读 code 决定重试还是放弃。

    安全层（PR-7）：危险动作（结束进程 / 系统级窗口 / 电源类）必须先确认，
    否则返回 `ok: false` + `requires_confirm: true`，**不下发任何动作**。
    内容门禁：`set_value` 会整段替换目标内容——执行前先查目标是否非空，
    非空则拦（要 `overwrite=True` 才放行），防止覆盖用户草稿（记事本事故形态）。
    审计：每次动作尝试都记一条（blocked / dry_run / executed）。
    """
    from .guard import assess_action

    verdict = assess_action(action=spec.action, target=target)
    base = {
        "action": spec.action,
        "dry_run": not execute,
        "target": {"pid": target.get("pid"), "window_id": target.get("window_id")},
    }

    if verdict.dangerous and not confirm:
        written = _audit(
            audit,
            event="act.blocked",
            action=spec.action,
            ok=False,
            code=verdict.code,
            target=target,
            detail={"danger": verdict.to_dict()},
        )
        return _mark_audit({
            "ok": False,
            **base,
            "requires_confirm": True,
            "danger": verdict.to_dict(),
            "error": {
                "code": verdict.code,
                "message": verdict.reason,
                "detail": "确认方式：CLI 加 --confirm / MCP 传 confirm=true",
            },
        }, written)

    tool, args = build_act_call(spec, target, snapshot_id)

    out: dict[str, Any] = {"ok": True, **base, "tool": tool, "snapshot_id": snapshot_id}
    if verdict.dangerous:
        out["danger"] = verdict.to_dict()
    if not execute:
        out["would_call"] = {"tool": tool, "args": args}
        out["note"] = "干跑：未下发任何动作（加 --execute 才真实执行）"
        written = _audit(
            audit,
            event="act.dry_run",
            action=spec.action,
            ok=True,
            target=target,
            detail={"tool": tool, "confirmed": bool(confirm)},
        )
        return _mark_audit(out, written)

    # 内容门禁：只有**真执行**的写值动作才判（干跑不下发，不拦）
    #
    # 两把钥匙（R-3）：
    # - `overwrite` 单独给 = "我知道可能覆盖既有内容" → 已确认非空时放行；
    #   **但读不到状态（unverified）时仍然拦**——授权覆盖不等于授权"在不知道
    #   内容的情况下覆盖"
    # - `overwrite + allow_unverified` = 两把钥匙，才放行 unverified 写入，
    #   并且这条路径完全不读状态（特殊控件读不到，白读一次还多一次驱动往返）
    if execute and spec.action in WRITE_ACTIONS:
        if overwrite and allow_unverified:
            out["write_gate"] = GATE_UNVERIFIED_BYPASS
            out["write_gate_override"] = "allow_unverified+overwrite"
        else:
            gate = check_write_gate(driver, spec, target)
            if gate.get("blocked") and overwrite:
                # 已确认非空 + 显式覆盖授权 → 放行（既有能力保留）
                out["write_gate"] = GATE_OVERWRITE
            elif gate.get("blocked"):
                _audit(
                    audit,
                    event="act.blocked",
                    action=spec.action,
                    ok=False,
                    code=EXISTING_CONTENT_CODE,
                    target=target,
                    detail={"write_gate": gate, "tool": tool},
                )
                return {
                    "ok": False,
                    **base,
                    "requires_confirm": True,
                    "write_gate": gate,
                    "error": {
                        "code": EXISTING_CONTENT_CODE,
                        "message": (
                            f"目标元素非空（{gate.get('existing_chars')} 字符），"
                            "set_value 会整段替换其内容"
                        ),
                        "detail": (
                            "如确要覆盖，请带 overwrite=true 显式授权"
                            "（CLI --overwrite / MCP overwrite=true）；不确定就先重读一遍状态再决定"
                        ),
                    },
                }
            if gate and gate["status"] == GATE_UNVERIFIED:
                # fail-closed（R-3）：确认不了目标内容就不写。
                # 走到这里必然不是双授权（双授权在上面已短路），所以直接拦。
                _audit(
                    audit,
                    event="act.blocked",
                    action=spec.action,
                    ok=False,
                    code=UNVERIFIED_CODE,
                    target=target,
                    detail={"write_gate": gate, "tool": tool},
                )
                return {
                    "ok": False,
                    **base,
                    "requires_confirm": True,
                    "write_gate": gate,
                    "error": {
                        "code": UNVERIFIED_CODE,
                        "message": (
                            "无法确认目标元素的既有内容（"
                            + str(gate.get("reason") or "读取目标状态失败")
                            + "），已按 fail-closed 拒绝写入"
                        ),
                        "detail": (
                            "先重读一遍状态（winuse_state）再决定；若目标确实是读不到状态的"
                            "特殊控件，需同时带 allow_unverified=true 与 overwrite=true "
                            "两把钥匙显式授权（CLI --allow-unverified --overwrite）"
                        ),
                    },
                }

            if not gate.get("blocked"):
                # blocked 的那条已经在上面写成 GATE_OVERWRITE（overwrite 授权放行），
                # 不能被门禁自己的 "blocked" 覆盖掉
                out["write_gate"] = gate["status"]
            if gate.get("refreshed") and gate.get("fresh_token"):
                # 门禁这次读刷新了驱动快照 → 旧令牌已失效，把写值调用切到新令牌上
                spec = ActSpec(
                    **{**spec.__dict__, "element_token": gate["fresh_token"], "element_index": None}
                )
                snapshot_id = None  # 令牌自带快照信息，不再依赖调用方给的旧快照
                tool, args = build_act_call(spec, target, snapshot_id)
                out["tool"] = tool
                out["snapshot_id"] = snapshot_id
                out["element_token"] = gate["fresh_token"]

    out["args"] = args
    try:
        out["result"] = driver.call(tool, args)
    except Exception as exc:  # noqa: BLE001 - 归一化成结构，不让异常穿层
        from ..driver.errors import WinUseError

        if isinstance(exc, WinUseError):
            out["ok"] = False
            out["error"] = exc.to_dict()
        else:
            raise
    else:
        # 驱动可能"不抛异常但拒绝"（stale_element_token 等）——必须在 ok 上体现
        refusal = _driver_refusal(out["result"])
        if refusal is not None:
            out["ok"] = False
            out["error"] = refusal
    detail: dict[str, Any] = {"tool": tool, "confirmed": bool(confirm), "dry_run": False}
    # 记下写值门禁的实际结论：status=unverified 时说明这次写入走的是**双授权逃生门**
    # （事后想追"谁在确认不了内容的情况下还写了"就靠这条）
    if "write_gate" in out or "write_gate_override" in out:
        detail["write_gate"] = out.get("write_gate")
        if "write_gate_override" in out:
            detail["write_gate_override"] = out["write_gate_override"]
    written = _audit(
        audit,
        event="act.executed",
        action=spec.action,
        ok=bool(out["ok"]),
        code=(out.get("error") or {}).get("code"),
        target=target,
        detail=detail,
    )
    return _mark_audit(out, written)
