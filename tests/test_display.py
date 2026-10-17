"""winuse_display 单测：显示器/DPI/窗口归属的纯逻辑部分。

原生枚举（ctypes）不在这里测——那部分由 `python -m winuse display` 在真机上验
（对照三屏已知事实）。这里只测可以穷举断言的纯函数。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.display import (  # noqa: E402
    assign_windows_to_monitors,
    is_sentinel_bounds,
    pick_monitor,
    scale_from_dpi,
)


def mon(idx, x, y, w, h, dpi=96, primary=False):
    return {
        "index": idx,
        "bounds": {"x": x, "y": y, "width": w, "height": h},
        "work": {"x": x, "y": y, "width": w, "height": h - 40},
        "dpi": dpi,
        "scale": scale_from_dpi(dpi),
        "primary": primary,
    }


def win(pid, wid, x, y, w=800, h=600, minimized=False):
    return {
        "pid": pid,
        "window_id": wid,
        "title": f"W{wid}",
        "bounds": {"x": x, "y": y, "width": w, "height": h},
        "minimized": minimized,
        "is_on_screen": True,
    }


class TestScaleFromDpi(unittest.TestCase):
    def test_common_scales(self):
        self.assertEqual(scale_from_dpi(96), 1.0)
        self.assertEqual(scale_from_dpi(144), 1.5)
        self.assertEqual(scale_from_dpi(192), 2.0)

    def test_non_standard_dpi_is_kept_as_ratio(self):
        # 125% 这类非整倍数也要如实换算，不能四舍五入成 1.0
        self.assertAlmostEqual(scale_from_dpi(120), 1.25, places=4)

    def test_zero_dpi_falls_back_to_one(self):
        self.assertEqual(scale_from_dpi(0), 1.0)


class TestSentinelBounds(unittest.TestCase):
    def test_minimized_sentinel_is_detected(self):
        self.assertTrue(is_sentinel_bounds({"x": -32000, "y": -32000, "width": 160, "height": 30}))

    def test_normal_offscreen_left_is_not_sentinel(self):
        # 左竖屏原点在负坐标区（-2160,-890），这是**正常**的，不能当成哨兵
        self.assertFalse(is_sentinel_bounds({"x": -2160, "y": -890, "width": 1440, "height": 2560}))

    def test_missing_bounds_is_not_sentinel(self):
        self.assertFalse(is_sentinel_bounds(None))


class TestPickMonitor(unittest.TestCase):
    def setUp(self):
        # 三屏布局（与真机一致：左竖 150% / 主横 150% / 右竖 100%）
        self.monitors = [
            mon(0, -2160, -890, 2160, 3840, dpi=144),
            mon(1, 0, 0, 3840, 2160, dpi=144, primary=True),
            mon(2, 3840, 0, 1080, 1920, dpi=96),
        ]

    def test_hit_first_monitor(self):
        self.assertEqual(pick_monitor(self.monitors, -1000, 0)["index"], 0)

    def test_hit_primary_center(self):
        self.assertEqual(pick_monitor(self.monitors, 1920, 1080)["index"], 1)

    def test_hit_right_portrait(self):
        self.assertEqual(pick_monitor(self.monitors, 4300, 900)["index"], 2)

    def test_point_outside_any_monitor_returns_primary(self):
        self.assertEqual(pick_monitor(self.monitors, 99999, 99999)["index"], 1)

    def test_point_on_boundary_belongs_to_the_monitor_it_starts(self):
        # 右竖屏起点 x=3840 属于右屏（左闭右开）
        self.assertEqual(pick_monitor(self.monitors, 3840, 10)["index"], 2)


class TestAssignWindows(unittest.TestCase):
    def setUp(self):
        self.monitors = [
            mon(0, -2160, -890, 2160, 3840, dpi=144),
            mon(1, 0, 0, 3840, 2160, dpi=144, primary=True),
            mon(2, 3840, 0, 1080, 1920, dpi=96),
        ]

    def test_windows_get_monitor_and_scale(self):
        windows = [
            win(10, 100, 3844, 159),  # 右竖屏上的窗口
            win(11, 101, 100, 100),  # 主屏
            win(12, 102, -2000, 0),  # 左竖屏
        ]
        got = assign_windows_to_monitors(windows, self.monitors)
        by_id = {w["window_id"]: w for w in got}
        self.assertEqual(by_id[100]["monitor"]["index"], 2)
        self.assertEqual(by_id[100]["scale"], 1.0)
        self.assertEqual(by_id[101]["monitor"]["index"], 1)
        self.assertEqual(by_id[101]["scale"], 1.5)
        self.assertEqual(by_id[102]["monitor"]["index"], 0)

    def test_minimized_window_has_no_coordinates(self):
        windows = [win(10, 100, -32000, -32000, minimized=True)]
        got = assign_windows_to_monitors(windows, self.monitors)
        self.assertIsNone(got[0]["monitor"])
        self.assertIsNone(got[0]["scale"])
        self.assertEqual(got[0]["placement"], "minimized")

    def test_offscreen_window_marked_offscreen(self):
        windows = [win(10, 100, 99999, 99999)]
        got = assign_windows_to_monitors(windows, self.monitors)
        self.assertEqual(got[0]["placement"], "offscreen")

    def test_spanning_window_uses_center(self):
        # 横跨主屏中线的窗口：按中心点判屏
        windows = [win(10, 100, 1800, 100, w=400, h=300)]
        got = assign_windows_to_monitors(windows, self.monitors)
        self.assertEqual(got[0]["monitor"]["index"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
