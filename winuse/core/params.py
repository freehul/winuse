"""严格参数解析：门面收进来的值必须先过这一层再做判定。

为什么要有这个模块（C-1）：门面原先一律 `bool(args.get("execute", False))`，
而 Python 里 `bool("false") is True`。上游模型把布尔写成字符串、客户端序列化出错
或被恶意构造时，`"false"` 会被当成 `true` —— 干跑门、确认门、覆盖授权门**全部**
可被绕过。这类错误必须在门面边界就拒绝，而不是"先判定再执行"。

为什么放在核心而不是各门面各写一份：三个门面（MCP / Hermes 桥接 / CLI）都要用，
多份实现必然漂移（项目已有前车之鉴：危险动作清单曾出现两份）。这里只做**类型
校验**，不做业务判定——判据仍然只有 `core.guard` 一份。
"""

from __future__ import annotations

from typing import Any

from ..driver.errors import InvalidParams

__all__ = ["InvalidParams", "strict_bool"]


def strict_bool(value: Any, *, field: str, default: bool = False) -> bool:
    """只接受 JSON 原生布尔：其余类型一律 `InvalidParams`。

    - `None`（未传）→ `default`
    - `True` / `False` → 原值（**显式 `False` 不能被 default 顶掉**）
    - 其它（`"false"`、`""`、`0`、`1`、`[]`……）→ `InvalidParams`

    为什么 `0` / `1` 也拒：JSON 里 `1` 是数字不是布尔，且"数字当布尔"在
    `amount=0` 之类的字段上就是合法值——一旦开了口子，判定语义会随字段漂移。
    宁可让调用方显式传 `false`，也不要猜。
    """
    if value is None:
        return default
    if value is True or value is False:
        return value
    raise InvalidParams(
        f"参数 {field} 必须是布尔值（true/false），收到 {type(value).__name__}：{value!r}",
        f"正确写法：\"{field}\": false（不要写成字符串 \"{field}\": \"false\"）",
    )
