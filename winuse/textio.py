"""标准输入输出的编码口径：一律 UTF-8。

为什么单独一个模块：`python -m winuse` 既是 CLI 本体，也是 MCP stdio 服务（外部 agent
用它拉起子进程）——两条路都必须走同一个编码口径，所以放一处。

为什么必须显式设置：MCP 官方规范（2025-06-18）要求 stdio 消息为 UTF-8，但 Windows 上
Python 对**管道**默认用系统本地编码（cp936）；于是中文被写成 GBK 字节，客户端按 UTF-8
解出来就是乱码（真机实测：WorkBuddy 的工具描述中文全乱）。反过来客户端发来的 UTF-8
中文参数，被我们按 cp936 解也会乱——收发两头都要钉死。
"""

from __future__ import annotations

from typing import Any


def force_utf8(stream: Any, errors: str = "replace") -> bool:
    """把某个文本流切成 UTF-8；流不支持（测试替身/已关闭）就跳过，返回是否成功。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(encoding="utf-8", errors=errors)
    except (ValueError, OSError, AttributeError):
        return False
    return True


def force_utf8_streams() -> None:
    """把标准三流都切成 UTF-8（CLI 与 MCP 服务的共同入口调用）。"""
    import sys

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        force_utf8(stream)
