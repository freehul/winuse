"""CLI `state` / `measure` 单测（打桩 Config.load，不碰真实驱动）。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse import cli  # noqa: E402


def win(pid, wid, title, app="Demo.exe", z=5, minimized=False):
    return {
        "app_name": app,
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        "is_on_screen": True,
        "layer": 0,
        "minimized": minimized,
        "pid": pid,
        "title": title,
        "window_id": wid,
        "z_index": z,
    }


def element(i, role, label, actions, enabled=True):
    return {
        "actions": list(actions),
        "depth": 1,
        "element_index": i,
        "element_token": f"sX:{i}",
        "enabled": enabled,
        "frame": {"x": i, "y": i, "h": 10, "w": 10},
        "label": label,
        "role": role,
    }


class StubDriver:
    def __init__(self):
        self.calls = []
        self.elements = [
            element(0, "Button", "确定", ["invoke"]),
            element(1, "Text", "只读说明", ["text"]),
            element(2, "Button", "禁用按钮", ["invoke"], enabled=False),
            element(3, "Pane", "滚动区", ["scroll"]),
            element(4, "Edit", "输入框", ["set_value"]),
        ]

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if tool == "list_windows":
            return {"windows": [win(100, 200, "Demo")], "current_space_id": None}
        if tool == "get_window_state":
            state = {
                "app_name": "Demo.exe",
                "element_count": len(self.elements),
                "elements": self.elements,
                "elements_complete": True,
                "pid": 100,
                "returned_element_count": len(self.elements),
                "screenshot_height": 600,
                "screenshot_mime_type": "image/png",
                "screenshot_png_b64": "iVBORw0KGgo" + "A" * 4000,
                "screenshot_width": 800,
                "snapshot_id": "sX",
                "total_element_count": len(self.elements),
                "tree_markdown": "- Document\n" * 40,
                "window_bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
                "window_id": 200,
                "window_title": "Demo",
            }
            # 真驱动：include_screenshot=false 时不下发截图（省的是大头）
            if not (args or {}).get("include_screenshot", True):
                state.pop("screenshot_png_b64", None)
                state.pop("screenshot_mime_type", None)
            return state
        raise AssertionError(f"未预期工具：{tool}")


class FakeConfig:
    def __init__(self, driver, max_elements=200):
        self._driver = driver
        self.state_max_elements = max_elements

    def driver(self):
        return self._driver


def run_cli(argv, cfg):
    buf = io.StringIO()
    with mock.patch("winuse.cli.Config.load", return_value=cfg):
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv)
    return code, buf.getvalue()


class TestStateCommand(unittest.TestCase):
    def test_prints_trimmed_state_with_size(self):
        cfg = FakeConfig(StubDriver())
        code, out = run_cli(["state", "--app", "Demo"], cfg)
        self.assertEqual(code, cli.EXIT_OK)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual([e["label"] for e in data["elements"]], ["确定", "输入框"])
        self.assertIn("size", data)
        self.assertGreater(data["size"]["chars"], 0)
        self.assertNotIn("iVBORw0KGgo", out)
        self.assertNotIn("tree_markdown", out)

    def test_respects_max_from_config(self):
        cfg = FakeConfig(StubDriver(), max_elements=1)
        code, out = run_cli(["state"], cfg)
        self.assertEqual(code, cli.EXIT_OK)
        data = json.loads(out)
        self.assertEqual(len(data["elements"]), 1)
        self.assertTrue(data["stats"]["truncated_by_cap"])

    def test_no_windows_flag_drops_window_list(self):
        cfg = FakeConfig(StubDriver())
        _, out = run_cli(["state", "--no-windows"], cfg)
        data = json.loads(out)
        self.assertNotIn("windows", data)
        self.assertNotIn("focus", data)

    def test_unknown_app_fails_with_stable_code(self):
        cfg = FakeConfig(StubDriver())
        code, out = run_cli(["state", "--app", "不存在的应用"], cfg)
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertEqual(json.loads(out)["error"]["code"], "WINDOW_NOT_FOUND")

    def test_asks_driver_for_lean_tree(self):
        driver = StubDriver()
        run_cli(["state"], FakeConfig(driver))
        args = [a for name, a in driver.calls if name == "get_window_state"][0]
        self.assertIs(args["include_screenshot"], False)
        self.assertNotIn("query", args)

    def test_query_is_passed_through_to_driver(self):
        driver = StubDriver()
        run_cli(["state", "--query", "确定"], FakeConfig(driver))
        args = [a for name, a in driver.calls if name == "get_window_state"][0]
        self.assertEqual(args["query"], "确定")


class TestMeasureCommand(unittest.TestCase):
    def test_reports_all_four_buckets(self):
        cfg = FakeConfig(StubDriver())
        code, out = run_cli(["measure"], cfg)
        self.assertEqual(code, cli.EXIT_OK)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        for key in ("raw_driver_response", "tree_only", "winuse_state", "ratio", "trim_stats"):
            self.assertIn(key, data)
        # 逐个查"体量递减"关系：原始（含截图）> 纯树 > 裁剪后
        self.assertGreater(data["raw_driver_response"]["chars"], data["tree_only"]["chars"])
        self.assertLess(data["ratio"]["winuse_vs_tree"], 1.0)

    def test_vision_equivalent_scales_with_area(self):
        cfg = FakeConfig(StubDriver())
        _, out = run_cli(["measure"], cfg)
        vision = json.loads(out)["screenshot_vision_equivalent"]
        self.assertEqual(vision["pixels"], "800x600")
        lo, hi = vision["estimated_vision_tokens_range"]
        self.assertLess(lo, hi)
        # 800×600 约为基线面积的 24%，故应明显低于基线区间
        self.assertLess(hi, cli.VISION_BASELINE_TOKENS[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
