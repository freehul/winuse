"""T-64：纯延时等待（`--kind sleep`）的行为锁。

对照物 windows-mcp / computer_use 都有无条件停顿；我们把它并进等待族，所以两条
不变量必须锁住：① 正常路径真的等满时长、不轮询、不碰驱动；② 参数不对（缺时长 /
超上限）在碰任何东西之前就被拒——错在调用方的参数不该以「超时」的形式表现出来。
"""

from __future__ import annotations

import pytest

from winuse.core import wait_for as w


def test_sleep_waits_once_and_never_touches_driver():
    calls: list[float] = []
    clock = {"t": 0.0}

    def fake_sleep(sec: float) -> None:
        calls.append(sec)
        clock["t"] += sec

    out = w.wait_for(None, w.WaitSpec(kind="sleep", seconds=1.5), sleep=fake_sleep, monotonic=lambda: clock["t"])

    assert out["ok"] is True and out["condition_met"] is True
    assert out["code"] is None and out["polls"] == 0
    assert calls == [1.5]  # 只睡一次、睡够时长（driver 传 None 也能跑 = 确实没碰驱动）
    assert out["waited_ms"] == 1500


def test_sleep_rejects_missing_seconds():
    with pytest.raises(w.MissingParam):
        w.wait_for(None, w.WaitSpec(kind="sleep"))


def test_sleep_rejects_over_limit():
    with pytest.raises(w.ParamOutOfRange):
        w.wait_for(None, w.WaitSpec(kind="sleep", seconds=99_999))


def test_sleep_is_a_known_kind():
    assert "sleep" in w.KINDS
