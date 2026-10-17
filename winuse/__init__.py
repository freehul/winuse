"""WinUse —— 桌面能力的统一门面。

定位：cua-driver 是引擎，WinUse 是方向盘。
本包只做一件事——把 cua-driver 的驱动级原语，翻译成 agent 能安全、省钱、
准确调用的粗粒度工具。

零第三方依赖：只用 Python 标准库（Windows API 走 ctypes）。
设计依据见项目内部设计契约（不随开源仓库发布）。
"""

#: 对外版本号——**唯一出处**（pyproject 通过 dynamic version 从这里读，
#: 只改这一处）。推进规则：每合入一个 PR 批次，按实施计划的变更记录同步递增。
__version__ = "0.12.0"
__all__ = ["__version__"]
