"""winuse.mcp —— MCP（Model Context Protocol）stdio 门面。

设计定位：门面 2（与 Hermes 插件门面、CLI 门面并列），只做协议翻译，
业务逻辑一律调 `winuse.core`，不在此另写一份实现。

为什么是 stdio 而不是 HTTP/SSE：用户明确要求不开常驻网络监听（避免绕过宿主审批门禁）。
官方规范（2025-06-18）对 stdio 的要求也正好契合本机场景：
客户端把服务端作为子进程启动，stdin 收 / stdout 发，消息以换行分隔。
"""

from . import protocol, server

__all__ = ["protocol", "server"]
