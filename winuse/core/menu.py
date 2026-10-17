"""winuse_menu：应用菜单路径调用（封装驱动的 `invoke_menu`）。

为什么值得单列一个能力：菜单里的功能**大多没有可用元素指代**——工具栏按钮能找到，
但「文件 → 导出 → XML」这种深层项在 UIA 树里要么不存在、要么与同名项歧义；
`invoke_menu` 是驱动给的一条**按路径逐层解析**的确定性通道（一层一层走真实原生菜单，
最终项走无障碍 API 调用），比"点像素坐标碰运气"可靠得多。

要点（依据驱动 0.28.2 `describe invoke_menu` 的原文）：

1. **逐层解析、fail-closed**：驱动原文 *"Missing, ambiguous, disabled, or structurally
   mismatched segments fail closed; this tool never falls back to pixels."* ——
   所以标签必须给准；含糊的路径不会"尽力而为"。
2. **匹配口径**：只 trim 两端空白，其余**大小写敏感**；路径 1..16 段
   （`minItems: 1, maxItems: 16`），每段 1..200 字（`minLength/maxLength: 200`）。
   本模块在下发前先把每段 trim 一遍：驱动反正是 trim 后匹配，我们提前规范化，
   回执里给的路径就是**实际参与匹配**的标签。
3. **这是会改应用状态的动作**：驱动把它分类成 `read_only=false, destructive=true`，
   最终项可能是「保存 / 删除 / 退出」。因此未确认时**连干跑都不给调用计划**
   —— 与 `core.desktop` 的窗口动作门禁同口径（闸门在参数校验与干跑分支之前），
   也避免了拿一份"会下发什么"被误读成"已经下发过"。
4. **不抢前台**：`invoke_menu` 的参数里**没有** `delivery_mode`，它走无障碍 API，
   不会把目标应用换到前台（这也是它相对像素点击的优势）。
5. **数据卫生**：回执只留路径**段数**，不回标签正文——菜单标签经常就等于用户内容
   （「文件 → 最近使用的文件 → 客户合同.docx」），回带等于把用户数据写进审计。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..driver.errors import MissingParam, MissingTarget, ParamOutOfRange, WinUseError

# 驱动"没说成功也没说失败"的归一只有一份实现（`core.act`）——本模块不复制：
# 复制一份的下场是两处对拒绝形态的判断迟早漂移（act 的注释里记着踩过的坑）。
from .act import _driver_refusal  # noqa: F401  (re-exported for callers/tests)

#: 驱动工具名
TOOL = "invoke_menu"

#: 对外回执里的动作名（审计与门面共用）
ACTION = "invoke_menu"

#: 驱动 schema 上界（`path`: minItems 1 / maxItems 16；每段 maxLength 200）
MAX_PATH_ITEMS = 16
MIN_PATH_ITEMS = 1
MAX_ITEM_CHARS = 200

#: 危险判定规则名（菜单调用恒为危险动作，见模块头 ③）
RULE = "menu_invoke"

DRY_RUN_NOTE = "干跑：未下发任何菜单调用（加 --execute 才真实执行；本动作会改变应用状态）"
EXECUTED_NOTE = "已通过 invoke_menu 下发（逐层解析菜单路径并调用最终项）"

#: 驱动拒绝码：路径里某一段在当前窗口解析不到（驱动的匹配口径是"精确、逐层实时解析"）
PATH_UNAVAILABLE_CODE = "MENU_PATH_UNAVAILABLE"

#: 被拒后取一次窗口状态、找真实菜单文本时用的元素上限。
#: 只在这一条失败路径上付成本；菜单项通常排在无障碍树前部，给宽一点免得漏掉。
MENU_SCAN_ELEMENTS = 400

#: 回执里候选菜单文本的条数与单条长度上限（回的是窗口**顶层**菜单名，通常是应用
#: 自带的固定标签；限制这两个数字是为了万一某个菜单恰好展开着、树里混进了子项
#: ——子项标签可能等于用户内容，不能让它们整片进回执）
MENU_CANDIDATE_LIMIT = 15
MENU_CANDIDATE_CHARS = 40

#: 加速键后缀：结尾括号里**单个**字母或数字（"帮助(H)"）。括号里若是词组
#: （"导出(CSV)"）那是标签本体，剥掉只会得到一个窗口里根本不存在的名字。
_ACCEL_SUFFIX = re.compile(r"\s*\([A-Za-z0-9]\)\s*$")


def _slim_target(target: dict) -> dict:
    """回执里的目标只留定位用的两个字段（app_name/title 不进回执，见数据卫生）。"""
    return {"pid": target.get("pid"), "window_id": target.get("window_id")}


def _path_len(path: Any) -> int:
    """尽量数出路径段数（只用于给"被拦"回执一个可读的规模，不校验）。"""
    if isinstance(path, (list, tuple)):
        return len(path)
    return 0


def build_menu_call(target: dict, path: Any) -> tuple[str, dict[str, Any]]:
    """纯函数：把「目标窗口 + 菜单路径」翻译成 (驱动工具名, 参数)。

    参数不全/不合法就抛（编程错误应当被看见，不该到驱动那边变成一次难懂的报错）。
    """
    pid, window_id = target.get("pid"), target.get("window_id")
    if pid is None or window_id is None:
        raise MissingTarget(
            "invoke_menu 需要目标窗口的 pid 与 window_id",
            "菜单路径是相对某个窗口解析的：先 winuse_state 选窗口（它会给 pid/window_id）",
        )

    if isinstance(path, str) or not isinstance(path, Sequence):
        raise MissingParam(
            "菜单路径要是字符串列表（每段一层菜单）",
            "例如 --menu-path '文件,导出,XML'",
        )
    items = list(path)
    if not items:
        raise MissingParam(
            "菜单路径不能为空（驱动要求至少 1 段）",
            "例如 --menu-path '文件,导出,XML'",
        )
    if len(items) > MAX_PATH_ITEMS:
        raise ParamOutOfRange(
            f"菜单路径最多 {MAX_PATH_ITEMS} 段，收到 {len(items)} 段",
            "深层功能请检查是否把整条快捷键说明也当成了一层菜单",
        )

    normalized: list[str] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, str):
            raise MissingParam(
                f"菜单路径第 {idx} 段不是字符串（收到 {type(item).__name__}）",
                "每段一层菜单标签，例如 ['文件', '导出', 'XML']",
            )
        label = item.strip()
        if not label:
            raise MissingParam(
                f"菜单路径第 {idx} 段是空白",
                "空白段永远匹配不到菜单项；去掉它或补上真实标签",
            )
        if len(label) > MAX_ITEM_CHARS:
            raise ParamOutOfRange(
                f"菜单路径第 {idx} 段 {len(label)} 字，超过驱动上界 {MAX_ITEM_CHARS} 字",
                "驱动 schema 对每段有 maxLength 限制；请确认给的是标签而不是整段说明文字",
            )
        normalized.append(label)

    return TOOL, {"pid": pid, "window_id": window_id, "path": normalized}


def _strip_accelerator(label: str) -> str:
    """剥掉 Win32 菜单标签结尾的加速键后缀（"帮助(H)" → "帮助"）。

    只认"结尾括号里单个字母/数字"这一种形态：`导出(CSV)` 的括号是标签本体的一部分，
    剥掉会得到一个窗口里根本不存在的名字，反而把调用引到失败。
    """
    return _ACCEL_SUFFIX.sub("", label).strip()


def _match_menu_label(want: str, actuals: Sequence[str]) -> str | None:
    """把调用方写的标签映射到窗口里**真实存在**的菜单文本。

    由严到宽：整串相等 → 忽略首尾空白与大小写 → 再忽略加速键后缀。都不匹配返回
    None —— **绝不**挑一个"看起来像"的：菜单里有「删除」「退出」这类项，猜错不是
    调用失败，而是事故。同理，同一层里若有**两个不同项重名**（`关于(A)` / `关于(B)`），
    也返回 None：那种情况下无法确定用户指的是哪一个，宁可不动。
    """

    def _unique(hits: list[str]) -> str | None:
        """命中的文本去重后只剩一个才算数。"""
        distinct = list(dict.fromkeys(hits))
        return distinct[0] if len(distinct) == 1 else None

    target = want.strip()
    if not target:
        return None
    exact = _unique([label for label in actuals if label == want])
    if exact is not None:
        return exact
    folded = target.casefold()
    spaced = _unique([label for label in actuals if label.strip().casefold() == folded])
    if spaced is not None:
        return spaced
    bare = _strip_accelerator(target).casefold()
    unique = _unique([label for label in actuals if _strip_accelerator(label).casefold() == bare])
    if unique is not None:
        return unique
    # 最后一级：允许"简称"。真实菜单项常比调用方写的长得多——`关于 WinRAR(A)...` 而
    # 人只会写「关于」。仍然要求**唯一命中**：同一层里有两个项都以前缀吻合时宁可不动
    # （那正是"点错功能"的场景，不是"匹配不上"的场景）。
    return _unique([label for label in actuals if _strip_accelerator(label).casefold().startswith(bare)])


def _window_menu_items(driver, target: dict) -> list[tuple[str, str]]:
    """取一次窗口状态，收集当下可见的菜单项 `(完整标签, 元素令牌)`。

    **只在路径需要解析时调用**（正常路径零额外开销）。驱动是"逐层实时解析"的：它只
    在真正展开某一层时才读那一层的项，所以核心拿不到整棵菜单树，只能一层一层取。
    返回**完整**标签：截断版只用于回执（见 MENU_CANDIDATE_CHARS），拿来映射或下发
    会因为少了尾巴而匹配不上。
    """
    pid, window_id = target.get("pid"), target.get("window_id")
    if pid is None or window_id is None:
        return []
    try:
        result = driver.call(
            "get_window_state",
            {"pid": pid, "window_id": window_id, "max_elements": MENU_SCAN_ELEMENTS},
        )
    except WinUseError:
        return []
    elements = result.get("elements") if isinstance(result, dict) else None
    if not isinstance(elements, list):
        return []
    items: list[tuple[str, str]] = []
    for element in elements:
        if not isinstance(element, dict) or element.get("role") != "MenuItem":
            continue
        label = element.get("label")
        # 驱动原始字段是 element_token（core/state.py 才把它归一成 token）；这里直连
        # 驱动，必须按驱动的名字读。读错整条解析链会静默拿到空样本——曾经因此连候选
        # 都不回，外面看起来"功能没生效"，实际是读错了字段。
        token = element.get("element_token") or element.get("token")
        if isinstance(label, str) and label.strip() and isinstance(token, str) and token:
            items.append((label.strip(), token))
        if len(items) >= MENU_CANDIDATE_LIMIT * 4:
            break
    return items


def _expand_menu_item(driver, target: dict, token: str) -> bool:
    """展开一个菜单项，好让它的子项出现在无障碍树里。

    走 `core.act` 的通道（而不是自己 driver.call）：展开的参数校验、拒绝归一、审计
    口径都只有那一份实现，绕过去等于在核心层开第二个后门。
    """
    from .act import ActSpec, run_act

    result = run_act(
        driver,
        spec=ActSpec(action="expand", element_token=token),
        target=target,
        snapshot_id=None,
        execute=True,
    )
    return bool(result.get("ok"))


def _resolve_from_pairs(
    driver, target: dict, items: Sequence[Any], pairs: list[tuple[str, str]]
) -> list[str] | None:
    """用首批样本 `pairs` 起手，逐层展开，把 `items` 每段映射成窗口里真实的文本。

    为什么只认"这一层新出现的"：某一层展开后，树里会同时有它的子项与本层/上层还在的
    项。若直接在整棵树里找，就可能把**别层**的同名项错认成这一层的子项——认错不是调用
    失败，而是点到别的功能（菜单里有「删除」「退出」）。所以每层只用"上一批样本里没
    出现过的标签"；单段路径连展开都不做。

    返回**最长可解析前缀**（后面几段原样保留）；一段都解析不出时返回 None，表示"没有
    任何纠正"。保留前缀是有意的：调用方写的路径常只有中间某段写法不对，纠对了前缀、
    后面原样交给驱动，比整条放弃更可能成功——驱动本来就是逐层实时解析的，前面段纠对
    之后后面那些往往本来就是对的，它报的"第几段没找到"也因此更有用。
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for depth, want in enumerate(items):
        if not pairs:
            break
        labels = [label for label, _ in pairs]
        fresh = [label for label in labels if label not in seen] or labels
        actual = _match_menu_label(str(want), fresh)
        if actual is None:
            break
        resolved.append(actual)
        seen.update(labels)
        if depth < len(items) - 1:
            token = next((tok for label, tok in pairs if label == actual), None)
            if token is None or not _expand_menu_item(driver, target, token):
                break
            pairs = _window_menu_items(driver, target)
    if not resolved:
        return None
    return [*resolved, *[str(item) for item in items[len(resolved) :]]]


def _resolve_path_by_expanding(driver, target: dict, items: Sequence[Any]) -> list[str] | None:
    """便捷入口：自己先取一次样本，再交给 `_resolve_from_pairs`。"""
    return _resolve_from_pairs(driver, target, items, _window_menu_items(driver, target))


def _retry_with_window_labels(driver, target: dict, path: Any, out: dict) -> None:
    """路径被拒之后：读窗口里的真实菜单文本，映射**首段**并重试一次（原地改 out）。

    为什么只映射首段：驱动的解析逐层实时，第二层往后的实际文本要等那一层真被展开才
    存在，核心此刻取不到。而首段恰好是调用方最容易凭直觉写错的一层（写"帮助"而真实
    标签是"帮助(H)"）——映射它就能救回绝大多数调用；更深层仍要求精确，但候选文本
    随回执一起回去，调用方照着改即可。

    重试成功时把 `args` 换成实际下发的路径、清掉旧的错误，并留一条 `menu_label_retry`
    记录发生过替换（可溯源，不静默换标签）。
    """
    items = list(path) if isinstance(path, (list, tuple)) else []
    pairs = _window_menu_items(driver, target)
    if pairs:
        # 候选回执给的是**截断版**标签：这里只需要让调用方认出正确写法，条数与长度
        # 上限见 MENU_CANDIDATE_* 的注释。
        out["menu_candidates"] = [label[:MENU_CANDIDATE_CHARS] for label, _ in pairs][:MENU_CANDIDATE_LIMIT]
    if not items or not pairs:
        return

    resolved = _resolve_from_pairs(driver, target, items, pairs)
    if resolved is None or resolved == [str(item).strip() for item in items]:
        return

    _, retry_args = build_menu_call(target, resolved)
    try:
        result = driver.call(TOOL, retry_args)
    except WinUseError as exc:
        out["error"] = exc.to_dict()
        out["code"] = exc.code
        return

    out["menu_path_resolved"] = {"from": [str(item) for item in items], "to": resolved}
    out["result"] = result
    refusal = _driver_refusal(result)
    if refusal is None:
        out["ok"] = True
        out["code"] = None
        out["args"] = retry_args
        out.pop("error", None)
    else:
        out["code"] = refusal["code"]
        out["error"] = refusal


def _audit(sink, **fields) -> bool | None:
    """写一条审计事件（与 `core.act._audit` 同协议：只有显式返回 False 才算写失败）。"""
    from ..audit import sink_event

    return sink_event(sink, **fields)


def _mark_audit(out: dict, written: bool | None) -> dict:
    if written is not None:
        out["audit_written"] = written
    return out


def _blocked(target: dict, execute: bool, items: int, written: bool | None) -> dict:
    """危险且未确认 → 结构化拒绝（形状与 `core.desktop._blocked` / `core.act.run_act` 一致）。

    拒绝时**不给调用计划**：计划没有价值，agent 该做的是先确认或换个做法。
    """
    from .guard import CONFIRM_CODE, RiskVerdict

    verdict = RiskVerdict.danger(
        f"调用应用菜单的最终项（路径 {items} 段）会改变应用状态或触发业务动作",
        RULE,
    )
    return _mark_audit(
        {
            "ok": False,
            "dry_run": not execute,
            "action": ACTION,
            "target": _slim_target(target),
            "would_call": None,
            "code": verdict.code or CONFIRM_CODE,
            "requires_confirm": True,
            "danger": verdict.to_dict(),
            "error": {
                "code": verdict.code or CONFIRM_CODE,
                "message": f"菜单调用被安全层拦截：{verdict.reason}",
                "detail": f"规则：{verdict.rule}；确认方式：CLI 加 --confirm / MCP 传 confirm=true",
            },
        },
        written,
    )


def run_menu(
    driver,
    *,
    target: dict,
    path: Any,
    execute: bool = False,
    confirm: bool = False,
    audit=None,
) -> dict:
    """调用（或干跑）一条应用菜单路径。

    - 未确认 → 拦，`ok=False` + `requires_confirm=True`，**一个字节都不下发**
      （连干跑也不给计划；见模块头 ③）。
    - `confirm=True` 且 `execute=False` → 干跑，回 `would_call`（要下发的确切调用）。
    - `confirm=True` 且 `execute=True` → 真下发；驱动侧失败/拒绝回收成 `ok=False` +
      结构化 `error`（不抛），参数类错误照旧抛（编程错误要被看见）。
    """
    items = _path_len(path)
    if not confirm:
        written = _audit(
            audit,
            event="menu.blocked",
            action=ACTION,
            ok=False,
            code="CONFIRM_REQUIRED",
            target=target,
            detail={"rule": RULE, "path_items": items, "execute": bool(execute)},
        )
        return _blocked(target, execute, items, written)

    tool, args = build_menu_call(target, path)

    if not execute:
        written = _audit(
            audit,
            event="menu.dry_run",
            action=ACTION,
            ok=True,
            target=target,
            detail={"tool": tool, "path_items": items, "confirmed": True},
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
        "ok": True,
        "dry_run": False,
        "action": ACTION,
        "target": _slim_target(target),
        "tool": tool,
        "args": args,
        "code": None,
    }
    try:
        out["result"] = driver.call(tool, args)
    except WinUseError as exc:
        out["ok"] = False
        out["code"] = exc.code
        out["error"] = exc.to_dict()
    else:
        refusal = _driver_refusal(out["result"])
        if refusal is not None:
            out["ok"] = False
            out["code"] = refusal["code"]
            out["error"] = refusal

    # 路径没被驱动解析上时，用窗口里真实的菜单文本再试一次（T-61）。放在审计**之前**：
    # 审计要记的是最终结果，而这次重试可能把它从失败翻成成功。
    if not out["ok"] and out.get("code") == PATH_UNAVAILABLE_CODE:
        _retry_with_window_labels(driver, target, path, out)

    written = _audit(
        audit,
        event="menu.executed",
        action=ACTION,
        ok=bool(out["ok"]),
        code=out.get("code"),
        target=target,
        detail={"tool": tool, "path_items": items, "confirmed": True},
    )
    return _mark_audit(out, written)
