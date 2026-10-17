"""R-9：探针子进程的管道必须被显式关掉（不靠 GC 兜底）。

报告 R-9：`pytest -W error` 唯一暴露的失败是
`ResourceWarning: subprocess ... pipes still open`，指向 `mcp/probe.py` ——
探针启动子进程后开了两个读线程，却从没 `close()` 过 `proc.stdout`/`proc.stderr`，
也没 join 读线程。反复健康探测 / 插件重连 / 多 agent 并用时会积累句柄。

本文件不碰真实桌面、也不真起进程：把 `subprocess.Popen` 换成假对象，
既能断言"流被关闭"，又能在超时路径上验清理。
"""

from __future__ import annotations

import io
import threading
import time
from unittest import mock

from winuse.mcp import probe as probe_mod


class FakeStream:
    """可迭代的假管道：迭代完即结束；记录是否被 close。"""

    def __init__(self, lines=()):
        self._lines = list(lines)
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def write(self, _text):
        return len(_text)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self):
        self.stdin = FakeStream()
        self.stdout = FakeStream()
        self.stderr = FakeStream([" [winuse-mcp] 启动\n"])
        self.returncode = 0
        self.killed = False

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True


def test_probe_closes_all_pipes_and_joins_readers():
    proc = FakeProc()
    with mock.patch.object(probe_mod.subprocess, "Popen", return_value=proc):
        report = probe_mod.probe(tool="winuse_display", timeout=0.2)

    assert proc.stdin.closed, "stdin 必须关闭（那也是让服务端正常退出的规范姿势）"
    assert proc.stdout.closed, "stdout 管道必须显式关闭（原先只靠 GC → ResourceWarning）"
    assert proc.stderr.closed, "stderr 管道同理"
    assert report["ok"] is False  # 假进程不会有回包


def test_probe_leaves_no_reader_threads_behind():
    proc = FakeProc()
    before = {t.ident for t in threading.enumerate()}
    with mock.patch.object(probe_mod.subprocess, "Popen", return_value=proc):
        probe_mod.probe(tool="winuse_display", timeout=0.2)
    time.sleep(0.05)  # 给线程退出一点时间（join 正常时这里无需等待）
    after = {t.ident for t in threading.enumerate()}
    leftover = after - before
    alive = [t for t in threading.enumerate() if t.ident in leftover and t.is_alive() and "probe" in t.name]
    assert not alive, f"读线程必须被 join，实际残留：{alive}"


def test_no_resource_warning_is_raised():
    """用 warnings 当错误跑一遍：这正是报告里 `pytest -W error` 的失败点。"""
    import warnings

    proc = FakeProc()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with mock.patch.object(probe_mod.subprocess, "Popen", return_value=proc):
            probe_mod.probe(tool="winuse_display", timeout=0.2)
    offenders = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not offenders, f"不该再有 ResourceWarning：{[str(w.message) for w in offenders]}"
