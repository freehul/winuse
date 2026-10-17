"""测试全局隔离。

审计文件默认落在 `<用户目录>/.winuse/audit.jsonl`——单测若走真实路径，会把假目标
（Demo.exe 之类）的记录混进真实审计流，破坏"审计可回溯"。这里统一改成每次用例的临时目录。
"""

from __future__ import annotations

import pytest

from winuse import audit as audit_mod

_real_record = audit_mod.record


@pytest.fixture(autouse=True)
def _isolate_audit(tmp_path, monkeypatch):
    """所有用例默认写临时审计文件；调用方显式给 path 时不干预。

    只改写 `record()` 的默认落盘目标，`default_path()` 语义保持原样——
    这样"默认路径规则"与"不写真实审计流"两件事可以同时成立。
    """

    def _record(entry, path=None, env_path=None):
        # 显式配了 `WINUSE_AUDIT_PATH` 就**按配置走**：那正是一些用例要验的东西
        # （写入端与查询端同源）。配的路径也在 tmp 里，同样不污染真实审计流。
        if path is None and env_path and audit_mod._env_value(env_path, "WINUSE_AUDIT_PATH"):
            return _real_record(entry, env_path=env_path)
        return _real_record(entry, path=path or (tmp_path / "audit.jsonl"), env_path=env_path)

    monkeypatch.setattr(audit_mod, "record", _record)
