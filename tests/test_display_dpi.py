"""R-7：原生原型声明 + DPI 口径不许"嘴上说物理坐标"。

报告 R-7 的三点：
1. `display.py` 对 `EnumDisplayMonitors` / `GetMonitorInfoW` / `GetDpiForMonitor`
   **一条原型都没声明**（`desktop.py` 早就声明了）——64 位上句柄会被默认 `c_int` 截断。
2. `GetDpiForMonitor` 不是 DPI-aware 版本，官方明确说 per-monitor aware 的线程应改
   用 `GetDpiForWindow`。
3. `coordinate_note` 把"已声明 Per-Monitor v2、bounds 是物理像素"写死——声明失败时
   那句话就是假的，而调用方会照着它换算坐标。

本文件钉住 1 与 3（2 落在 `dpi_for_window` 的存在与接线里）。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from winuse.core import display as display_mod


class StubDriver:
    def call(self, tool, args=None):
        return {"windows": []}


class TestPrototypes(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "原型声明只对 Windows 有意义")
    def test_all_prototypes_are_declared(self):
        u, sh = display_mod._user32, display_mod._shcore
        self.assertTrue(u.EnumDisplayMonitors.argtypes, "EnumDisplayMonitors 必须声明原型")
        self.assertTrue(u.GetMonitorInfoW.argtypes, "GetMonitorInfoW 必须声明原型")
        self.assertTrue(u.GetDpiForWindow.argtypes, "GetDpiForWindow 必须声明原型")
        if sh is not None:
            self.assertTrue(sh.GetDpiForMonitor.argtypes, "GetDpiForMonitor 必须声明原型")

    @unittest.skipUnless(sys.platform == "win32", "同上")
    def test_handle_return_types_are_pointer_width(self):
        """返回值类型不能用 c_int：64 位上句柄会被截断。"""
        import ctypes

        from ctypes import wintypes

        restype = display_mod._user32.EnumDisplayMonitors.restype
        self.assertIs(restype, wintypes.BOOL)
        self.assertNotEqual(display_mod._user32.GetDpiForWindow.restype, ctypes.c_int)


class TestWindowDpiHelper(unittest.TestCase):
    def test_none_and_zero_return_none(self):
        self.assertIsNone(display_mod.dpi_for_window(None))
        self.assertIsNone(display_mod.dpi_for_window(0))

    @unittest.skipUnless(sys.platform == "win32", "需要真 user32")
    def test_real_window_dpi_is_readable(self):
        """真机自证：拿不到句柄时不该崩，拿到时给出正数 DPI。"""
        from winuse.core import foreground as fg_mod

        hwnd = fg_mod.foreground_window_id()
        if not hwnd:
            self.skipTest("当前没有前台窗口")
        dpi = display_mod.dpi_for_window(hwnd)
        self.assertIsNotNone(dpi)
        self.assertGreater(int(dpi), 0)


class TestCoordinateNoteHonesty(unittest.TestCase):
    def test_v2_claims_physical(self):
        note = display_mod.coordinate_note_for("per-monitor-v2")
        self.assertEqual(note, display_mod.COORDINATE_NOTE)
        self.assertNotIn("未拿到", note)
        self.assertTrue(display_mod.bounds_are_physical("per-monitor-v2"))

    def test_unchanged_warns_about_virtualized_coordinates(self):
        note = display_mod.coordinate_note_for("unchanged")
        self.assertIn("未拿到 Per-Monitor v2", note)
        self.assertIn("逻辑坐标", note)
        self.assertFalse(display_mod.bounds_are_physical("unchanged"))

    def test_unknown_state_is_also_not_physical(self):
        for state in (None, "per-monitor", "unsupported", "unknown"):
            with self.subTest(state=state):
                self.assertFalse(display_mod.bounds_are_physical(state))
                self.assertIn("未拿到", display_mod.coordinate_note_for(state))


class TestDescribeCarriesAwareness(unittest.TestCase):
    def _describe(self, awareness):
        with mock.patch.object(display_mod, "enum_monitors", return_value=[]):
            with mock.patch.object(display_mod, "current_dpi_awareness", return_value=awareness):
                return display_mod.describe(StubDriver())

    def test_output_declares_awareness_and_physicality(self):
        out = self._describe("per-monitor-v2")
        self.assertEqual(out["dpi_awareness"], "per-monitor-v2")
        self.assertIs(out["bounds_are_physical"], True)

    def test_output_refuses_physical_claim_when_not_v2(self):
        out = self._describe("unchanged")
        self.assertIs(out["bounds_are_physical"], False)
        self.assertIn("逻辑坐标", out["coordinate_note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
