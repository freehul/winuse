"""结果限流（**共享实现**，R-20）。

为什么单独一个模块：MCP 门面与 Hermes 桥接都要限流，各写一份必然漂移
（预算数字、裁剪字段、标注字段名都可能不一致），而这种漂移的表现是
"某个门面偶发断连"——最难查的一类问题。

两条口径：

1. **按最终线路消息算**（`enforce_wire_budget`）：客户端的限额是给"一条消息"的，
   不是给内层 payload 的。原先只量 payload，套上 `jsonrpc`/`result`/`content`/`text`
   四层包装后仍可能超限——实测能构造出这种包。按 UTF-8 **字节数**算，因为限额
   通常以字节给。
2. **裁剪要留痕**：裁掉多少、为什么裁，必须写在回包里（`truncated` /
   `returned` / `total_before_truncate` / `truncate_hint`），让 agent 知道
   "这不是全部"而不是以为数据就这么点。

纯 stdlib，不 import 项目内其它模块——门面（含独立部署的插件）都能安全引用。
"""

from __future__ import annotations

import json
from typing import Any

#: 单条工具结果的字符上限（沿用真机实测值：WorkBuddy 收到 5.1 万字符后断连）
MAX_RESULT_CHARS = 24000

#: 单条**线路消息**（最终 JSON-RPC 序列化后）的 UTF-8 字节预算
MAX_WIRE_BYTES = 24000

# ---------------------------------------------------------------------------
# 外来请求的资源上限（R-21）
#
# 为什么要有：一次请求可以让服务端花掉远超它"看起来"的成本——批量 1 万步、
# 零间隔轮询（忙等烧 CPU）、注册表递归到天荒地老、一条超长输入行吃满内存。
# 更糟的是**结果裁剪发生在昂贵调用之后**：先花完资源再裁，等于没防。
#
# schema 里声明是给守规矩客户端的提示；核心入口的检查才是真边界（见各模块）。
# ---------------------------------------------------------------------------

#: 单次批量最多多少步
MAX_BATCH_STEPS = 50

#: 单次等待的最长超时（秒）
MAX_WAIT_SECONDS = 600.0

#: 等待轮询的最小间隔（秒）：0 会变成忙等，必须抬起来
MIN_WAIT_INTERVAL_SECONDS = 0.05

#: 注册表递归的最大深度 / 最大子键条数
MAX_REGISTRY_DEPTH = 8
MAX_REGISTRY_SUBKEYS = 5000

#: stdio 单行输入的最大字节数（超长行直接拒，不进解析器）
MAX_STDIO_LINE_BYTES = 1_000_000

#: `winuse_state` 单次最多返回多少元素（= 驱动侧投影的硬上限）
MAX_STATE_ELEMENTS = 2000

#: `winuse_system` 最多返回多少进程（top_n）
MAX_TOP_PROCESSES = 200

#: OCR 输入图片的最大字节数（超大会让 OCR 引擎吃内存/超时）
MAX_OCR_IMAGE_BYTES = 20 * 1024 * 1024

#: OCR 允许的图片扩展名（WinRT OCR 能吃的主流格式）
OCR_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")


#: 可裁剪的列表字段（挑容纳条数最多的那个裁），保前 N 条并标注裁剪信息
TRUNCATABLE_FIELDS = (
    "elements",
    "entries",
    "lines",
    "windows",
    "results",
    "items",
    "subkeys",
    "processes",
    "disks",
)


def dumps(obj: Any) -> str:
    """与协议层同口径的紧凑序列化（保留中文不转义）。"""
    return json.dumps(obj, ensure_ascii=False)


def limit_payload(payload: Any, limit: int = MAX_RESULT_CHARS) -> Any:
    """把工具结果压进 `limit` 字符：裁最长的可裁列表并标注（JSON 始终合法）。

    裁的是**回包**不是驱动快照——元素索引仍然有效，调用方用 query / max_elements 再取即可。
    """
    if not isinstance(payload, dict) or len(dumps(payload)) <= limit:
        return payload

    key: str | None = None
    total = 0
    for name in TRUNCATABLE_FIELDS:
        value = payload.get(name)
        if isinstance(value, list) and len(value) > total:
            key, total = name, len(value)
    if key is None:
        return too_large(payload, limit)

    kept = list(payload[key])
    while len(kept) > 1 and len(dumps({**payload, key: kept})) > limit:
        kept = kept[: len(kept) // 2]
    if len(dumps({**payload, key: kept})) > limit:
        return too_large(payload, limit)

    out = {**payload, key: kept}
    out["truncated"] = True
    out["returned"] = len(kept)
    out["total_before_truncate"] = total
    out["truncate_hint"] = (
        f"结果超过 {limit} 字符上限，{key} 已从 {total} 条裁到 {len(kept)} 条；"
        "要精确结果请缩小范围（winuse_state 用 query/max_elements，winuse_audit 用 recent）"
    )
    return out


def too_large(payload: dict, limit: int) -> dict:
    """裁不动（没有可裁列表）→ 回结构化错误，别让客户端收到超长消息后断连。"""
    return {
        "ok": False,
        "truncated": True,
        "error": {
            "code": "RESULT_TOO_LARGE",
            "message": f"工具结果超过 {limit} 字符且无法自动裁剪",
            "detail": "请用更精确的参数缩小结果（如 winuse_state 的 query / max_elements）",
        },
        "payload_keys": sorted(payload),
    }


def wire_bytes(message: Any) -> int:
    """一条线路消息的 UTF-8 字节数。"""
    return len(dumps(message).encode("utf-8"))


def enforce_wire_budget(reply: dict, budget: int = MAX_WIRE_BYTES) -> dict:
    """把**整条 JSON-RPC 回包**压进字节预算内（R-20）。

    顺序刻意如此：先量最终消息 → 只对内层 payload 做裁剪 → 再量一次。
    这样"payload 不到上限但包装后超了"的包也会被处理，而且不会为了省字节去动
    协议信封本身（`jsonrpc`/`id` 不许被裁——那是回包能否被识别的唯一依据）。
    """
    if not isinstance(reply, dict) or wire_bytes(reply) <= budget:
        return reply
    result = reply.get("result")
    if not isinstance(result, dict):
        return reply
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return reply
    text_item = content[0]
    if not isinstance(text_item, dict) or not isinstance(text_item.get("text"), str):
        return reply

    try:
        payload = json.loads(text_item["text"])
    except (json.JSONDecodeError, ValueError):
        return reply  # 不是 JSON（比如纯文本回执）→ 不猜，原样返回

    wrapper = wire_bytes({**reply, "result": {**result, "content": [{**text_item, "text": ""}]}})
    inner_budget = max(1000, budget - wrapper - 256)  # 256 给裁剪标注留位置
    trimmed = limit_payload(payload, inner_budget)
    new_content = [{**text_item, "text": dumps(trimmed)}] + list(content[1:])
    out = {**reply, "result": {**result, "content": new_content}}
    if wire_bytes(out) <= budget:
        return out

    # 裁到最小仍超预算 → 回一个"结果太大"的结构化错误（绝不发超长消息）
    fallback = too_large(payload if isinstance(payload, dict) else {"payload": payload}, inner_budget)
    return {**reply, "result": {**result, "content": [{**text_item, "text": dumps(fallback)}]}}
