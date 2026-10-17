"""体量估算：只用于横向比较（裁剪前 vs 裁剪后 vs 一张截图），不是精确计费口径。

口径明写在函数里，避免"凭感觉给个倍数"：
- CJK 字符：约 1 token / 字（主流分词器）
- 其余字符（ASCII 等）：约 4 字符 / token
真实 token 数随分词器而变，所以调用方应**同时**看原始字符数。
"""

from __future__ import annotations

import unicodedata


def is_cjk(ch: str) -> bool:
    """CJK 统一表意文字 / 扩展 A / 兼容表意文字 / 全角标点。"""
    code = ord(ch)
    if 0x4E00 <= code <= 0x9FFF:  # 常用汉字
        return True
    if 0x3400 <= code <= 0x4DBF:  # 扩展 A
        return True
    if 0xF900 <= code <= 0xFAFF:  # 兼容表意文字
        return True
    if 0x3000 <= code <= 0x303F:  # CJK 标点
        return True
    if 0xFF00 <= code <= 0xFFEF:  # 全角字符
        return True
    return unicodedata.east_asian_width(ch) in ("W", "F")


def estimate_tokens(text: str) -> int:
    """估算 token 数（CJK 1/字，其余 4 字符/1）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if is_cjk(ch))
    other = len(text) - cjk
    return cjk + max(0, other) // 4


def size_report(text: str) -> dict:
    """给出一段文本的体量报告（字符数 + 估算 token）。"""
    return {
        "chars": len(text),
        "estimated_tokens": estimate_tokens(text),
        "method": "CJK≈1token/字，其余≈4字符/token（仅用于横向比较）",
    }
