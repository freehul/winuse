"""winuse_multi_input：一批动作的整批干跑与按序真发。

设计原则落点：
1. **整批先看后发**：`execute=False` 时整批只算不发，返回每一步将下发的调用 ——
   agent 可以先审一遍「这批动作长什么样」再决定放行（text-first + 门禁的落点）。
2. **翻译只有一份**：单步的守门与参数组装全部复用 `core.act.build_act_call` /
   `core.act.run_act`，本模块不重写动作映射（多份实现必然漂移）。
3. **失败策略显式**：默认 `stop_on_error=True`，首个失败即停并回 `stopped_at`——
   在状态未知的界面上继续猛点，比停下来报错危险得多。
4. **快照同源**：整批共用同一个 `snapshot_id`。驱动不变量要求元素索引与快照同源，
   所以批内不得插新的取状态；要换快照就分批调用。
"""

from __future__ import annotations

from typing import Any

from .. import limit as limit_mod
from ..driver.errors import ParamOutOfRange, WinUseError
from . import guard as guard_mod
from .act import ACTION_TOOLS, ActSpec, build_act_call, run_act

#: 步骤字典允许出现的字段（与 ActSpec 的定义域一致；多余字段一律拒，避免拼错被静默忽略）
STEP_FIELDS = frozenset(
    {
        "action",
        "element_index",
        "element_token",
        "value",
        "text",
        "key",
        "modifiers",
        "direction",
        "amount",
        "by",
        "button",
        "count",
        "x",
        "y",
    }
)

#: 兜底错误码：run_act 归一化必然带 code，这里只防「结构被外力改动」
FALLBACK_CODE = "WINUSE_ERROR"

#: 批内快照被刷新后，仍按索引下发的步骤会被拒（R-14）
INDEX_INVALIDATED_CODE = "INDEX_INVALIDATED_BY_WRITE"


def _snapshot_rotated(spec: ActSpec, out_act: dict) -> bool:
    """这一步是否刷新了驱动快照（写值门禁读状态 → 令牌变了）。"""
    fresh = out_act.get("element_token")
    return bool(fresh) and fresh != spec.element_token


class StepNotSupported(WinUseError):
    """步骤描述不是 dict / ActSpec，或含未知字段、缺 action。"""

    code = "STEP_NOT_SUPPORTED"


def _to_spec(step: Any) -> ActSpec:
    """把一步翻译成 ActSpec（dict 或已是 ActSpec 都收）。"""
    if isinstance(step, ActSpec):
        return step
    if not isinstance(step, dict):
        raise StepNotSupported(
            f"步骤必须是 dict 或 ActSpec，收到 {type(step).__name__}",
            "示例：{\"action\": \"click\", \"element_index\": 3}",
        )
    unknown = sorted(set(step) - STEP_FIELDS)
    if unknown:
        raise StepNotSupported(
            "步骤含未知字段：" + "、".join(unknown),
            "可用字段：" + "、".join(sorted(STEP_FIELDS)),
        )
    action = step.get("action")
    if not isinstance(action, str) or not action:
        raise StepNotSupported(
            "步骤缺少 action（或 action 不是字符串）",
            "可用动作：" + "、".join(sorted(ACTION_TOOLS)),
        )
    return ActSpec(**step)


def _step_result(tool: str, args: dict, out: dict, execute: bool) -> dict:
    """一步的结果摘要：先给「下发/将要下发的调用」，再按情况附驱动返回或错误。

    **参数以 `run_act` 的最终下发表为准**（R-14）：写值门禁可能把调用切到刷新后的
    令牌上，预组装的那份 args 就不再等于真发出去的东西——回执与审计不能描述
    "另一个调用"。
    """
    final_tool = out.get("tool") or tool
    final_args = out.get("args") if isinstance(out.get("args"), dict) else args
    result: dict[str, Any] = {"tool": final_tool, "args": final_args}
    if execute and out.get("ok"):
        payload = out.get("result")
        result["driver_result"] = payload if isinstance(payload, dict) else {"value": payload}
    if not out.get("ok"):
        result["error"] = out.get("error") or {}
    return result


def run_multi(
    driver,
    steps: list,
    target: dict,
    snapshot: str | None,
    execute: bool = False,
    stop_on_error: bool = True,
    confirmed: bool = False,
    audit=None,
) -> dict:
    """按序处理一批动作。

    `execute=False`（默认）整批干跑，只回「将要下发的调用」，不碰桌面；
    `execute=True` 才按序真发。整批共用 `target` 与 `snapshot`。

    批量确认（R-13）：批量输入**本身**就算危险动作（`guard.assess_batch`，1 步也算），
    未 `confirmed=True` 时整批拒绝、**一步都不下发**。这条闸门必须在核心：原先只在
    CLI/MCP 门面判，任何直接调核心的路径都能绕过一次性批量确认。

    为什么干跑也拦（与 `window_action` 同口径）：干跑放行、真跑才拦会给调用方
    挖陷阱——"先审计划"通过后真执行却被拒。确认是对"批量"这一形态的授权，
    与是否真发无关。

    确认的**范围**：`confirmed` 表示"我知道这是一批动作"，因此会一并透传给每步
    `run_act(confirm=...)`（否则批量里带危险动作时，批层确认了、步骤层又拦，
    诊断困难——R-5 就是这个现象）。但**不等于**授权覆盖内容：
    `overwrite` 仍然独立，写值门禁照旧生效。

    返回：`{"ok", "dry_run", "steps": [...], "stopped_at"}`。
    - 每步：`{"index", "action", "ok", "code", "result"}`
    - `stopped_at`：`stop_on_error=True` 时首个失败的下标（即中断位置）；
      整批继续跑完（`stop_on_error=False`）或全成功时为 None——
      继续模式下失败位置看各步的 `ok`，不是「停在哪」。
    - 参数拼错（未知动作、缺 target、缺快照）**直接抛**：编程错误必须当场可见，
      不该被写成「这一步失败了」混进正常失败里。
    """
    target = target or {}
    batch = list(steps or [])

    # 步数上限（R-21）：一次请求就能让服务端跑几千步，必须在**任何驱动调用之前**拒
    if len(batch) > limit_mod.MAX_BATCH_STEPS:
        raise ParamOutOfRange(
            f"批量步数 {len(batch)} 超过上限 {limit_mod.MAX_BATCH_STEPS}",
            "请拆成多次调用；单次批量过大时，中途失败也难定位到具体哪一步",
        )

    # 先全量校验/组装再判批量确认：参数拼错属于编程错误，必须当场抛，
    # 不能被"未确认"这条闸门提前收成一个普通失败（否则调用方以为是授权问题）。
    specs = [_to_spec(raw) for raw in batch]
    plans = [build_act_call(spec, target, snapshot) for spec in specs]

    verdict = guard_mod.assess_batch(batch)
    if verdict.dangerous and not confirmed:
        written = _batch_audit(
            audit, "batch.blocked", batch, target,
            ok=False, code=verdict.code, reason=verdict.reason, confirmed=False,
        )
        out = {
            "ok": False,
            "dry_run": not execute,
            "code": verdict.code or "CONFIRM_REQUIRED",
            "requires_confirm": True,
            "danger": verdict.to_dict(),
            "steps": [],
            "stopped_at": None,
            "error": {
                "code": verdict.code or "CONFIRM_REQUIRED",
                "message": verdict.reason or "批量输入需确认后执行",
                "detail": "确认方式：confirmed=True（CLI --confirm / MCP confirm=true）",
            },
        }
        if written is not None:
            out["audit_written"] = written
        return out

    records: list[dict] = []
    stopped_at: int | None = None

    rotated_at: int | None = None
    for index, raw in enumerate(batch):
        spec = specs[index]
        tool, args = plans[index]

        # 快照轮换后的索引步骤：直接拒绝（R-14）。写值门禁在批内读状态会刷新驱动
        # 快照，旧索引可能指向**同序号的另一个控件**——猜不得，宁可让 agent 分批。
        # 用 element_token 的步骤不受影响（令牌自带快照信息）。
        if (
            rotated_at is not None
            and spec.element_index is not None
            and not spec.element_token
        ):
            records.append(
                {
                    "index": index,
                    "action": spec.action,
                    "ok": False,
                    "code": INDEX_INVALIDATED_CODE,
                    "result": {
                        "error": {
                            "code": INDEX_INVALIDATED_CODE,
                            "message": (
                                f"批内第 {rotated_at} 步（{specs[rotated_at].action}）刷新了驱动快照，"
                                "元素索引已失效"
                            ),
                            "detail": (
                                "改用 element_token（令牌自带快照信息），或把这批拆成两次调用重新取状态"
                            ),
                        }
                    },
                }
            )
            if stop_on_error:
                stopped_at = index
                break
            continue

        # 审计逐步透传：批层的 sink 同样覆盖每一步（原先 run_multi 不传 sink，
        # 批量里的动作在审计里完全消失——R-1）
        out_act = run_act(
            driver, spec, target, snapshot,
            execute=execute, confirm=confirmed, audit=audit,
        )

        # 门禁刷新过快照：把它记下来，后续索引步骤要拦（见上）
        if _snapshot_rotated(spec, out_act):
            rotated_at = index if rotated_at is None else rotated_at

        ok = bool(out_act.get("ok"))
        error = out_act.get("error") or {}
        records.append(
            {
                "index": index,
                "action": spec.action,
                "ok": ok,
                "code": None if ok else (error.get("code") or FALLBACK_CODE),
                "result": _step_result(tool, args, out_act, execute),
            }
        )

        if not ok and stop_on_error:
            stopped_at = index
            break

    ok = all(r["ok"] for r in records)
    written = _batch_audit(
        audit,
        "batch.executed" if execute else "batch.dry_run",
        batch, target,
        ok=ok, code=None if ok else records[-1]["code"], reason=None, confirmed=confirmed,
        stopped_at=stopped_at,
    )
    out = {
        "ok": ok,
        "dry_run": not execute,
        "steps": records,
        "stopped_at": stopped_at,
    }
    if rotated_at is not None:
        out["snapshot_rotated_at"] = rotated_at
    if written is not None:
        out["audit_written"] = written
    return out


def _batch_audit(audit, event: str, batch: list, target: dict, *, ok: bool,
                 code: str | None, reason: str | None, confirmed: bool,
                 stopped_at: int | None = None) -> bool | None:
    """批层审计：一步一条（`act.*`）+ 批层一条（`batch.*`），两条不互相替代。

    为什么批层还要单独一条：只看到 N 条 act 记录时，无法判断"这是一次批量提交"
    还是"N 次独立调用"——批量的一次性确认语义正是安全层要留痕的地方。
    """
    from ..audit import sink_event

    detail: dict = {
        "steps": len(batch),
        "confirmed": bool(confirmed),
        "actions": [str((_as_action(s))) for s in batch],
    }
    if reason:
        detail["reason"] = reason
    if stopped_at is not None:
        detail["stopped_at"] = stopped_at
    return sink_event(audit, event=event, ok=ok, code=code, target=target, detail=detail)


def _as_action(step) -> str:
    """从步骤里取动作名（dict / ActSpec / 其它一律空串，审计不该因脏输入炸）。"""
    if isinstance(step, ActSpec):
        return step.action
    if isinstance(step, dict):
        return str(step.get("action") or "")
    return ""
