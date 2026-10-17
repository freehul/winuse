"""T-63：坐标寻址的行为锁。

canvas / 视频 / 自绘控件不在无障碍树里，只能走像素；所以四条不变量必须锁住：
① 给定 x/y 时下发的是**坐标**而不是元素；② 只给一半 → 拒（另一半不猜）；
③ 与元素寻址同时给 → 拒（避免歧义、避免点到别处）；④ 非点击类动作给坐标 → 拒。
"""

from __future__ import annotations

import pytest

from winuse.core import act as a

T = {"pid": 1, "window_id": 2}


def test_point_is_sent_as_xy_and_stays_background():
    tool, args = a.build_act_call(a.ActSpec(action="click", x=10, y=20), T, None)
    assert (tool, args["x"], args["y"]) == ("click", 10, 20)
    assert "element_token" not in args and "element_index" not in args
    assert args["delivery_mode"] == "background"  # 后台投递：不移动光标、不抢焦点


def test_half_a_point_is_rejected():
    for spec in (a.ActSpec(action="click", x=10), a.ActSpec(action="click", y=20)):
        with pytest.raises(a.MissingParam):
            a.build_act_call(spec, T, None)


def test_point_and_element_are_mutually_exclusive():
    with pytest.raises(a.MissingParam):
        a.build_act_call(a.ActSpec(action="click", x=1, y=2, element_token="s1:0"), T, None)


def test_point_only_for_click_like_actions():
    with pytest.raises(a.MissingParam):
        a.build_act_call(a.ActSpec(action="set_value", x=1, y=2, value="v"), T, None)
