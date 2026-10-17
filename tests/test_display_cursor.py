"""T-65：光标位置读取（并入 `winuse_display` 输出）的行为锁。"""

from __future__ import annotations

import pytest

from winuse.core import display as dsp


def test_cursor_position_gives_two_ints_or_explicit_none():
    """要么给两个整数（物理像素），要么显式 None——不编假坐标。

    真机调用，所以锁的是「形状与诚实」而不是具体数值：数值随鼠标动，锁不住也不该锁。
    锁屏 / 安全桌面下 `GetCursorPos` 失败回 None，是设计行为而不是缺陷。
    """
    c = dsp.cursor_position()
    if c is None:
        pytest.skip("当前会话拿不到光标（锁屏/安全桌面）——显式 None 是设计行为")
    assert set(c) == {"x", "y"}
    assert all(isinstance(v, int) for v in c.values())
