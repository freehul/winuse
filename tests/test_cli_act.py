"""CLI `act` 单测（打桩 Config.load；默认必须是干跑）。"""

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


def win(pid=100, wid=200, title="Demo", z=5):
    return {
        "app_name": "Demo.exe",
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        "is_on_screen": True,
        "layer": 0,
        "minimized": False,
        "pid": pid,
        "title": title,
        "window_id": wid,
        "z_index": z,
    }


class StubDriver:
    def __init__(self):
        self.calls = []
        self.action_result = {"_note": "动作已下发"}

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if tool == "list_windows":
            return {"windows": [win()]}
        return self.action_result


class FakeConfig:
    state_max_elements = 200

    def __init__(self, driver):
        self._driver = driver

    def driver(self):
        return self._driver


def run_cli(argv, cfg):
    buf = io.StringIO()
    with mock.patch("winuse.cli.Config.load", return_value=cfg):
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv)
    return code, buf.getvalue()


def actions_sent(driver):
    return [args for name, args in driver.calls if name != "list_windows"]


class TestActCommand(unittest.TestCase):
    def test_default_is_dry_run(self):
        driver = StubDriver()
        code, out = run_cli(["act", "--do", "click", "--index", "3", "--snapshot", "sX"], FakeConfig(driver))
        self.assertEqual(code, cli.EXIT_OK)
        data = json.loads(out)
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["would_call"]["tool"], "click")
        self.assertEqual(actions_sent(driver), [])  # 干跑绝不下发

    def test_execute_sends_element_action(self):
        driver = StubDriver()
        code, out = run_cli(
            ["act", "--do", "click", "--index", "3", "--snapshot", "sX", "--execute"],
            FakeConfig(driver),
        )
        self.assertEqual(code, cli.EXIT_OK)
        data = json.loads(out)
        self.assertFalse(data["dry_run"])
        sent = actions_sent(driver)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["element_index"], 3)
        self.assertEqual(sent[0]["snapshot_id"], "sX")
        self.assertEqual(sent[0]["delivery_mode"], "background")

    def test_index_without_snapshot_is_refused(self):
        driver = StubDriver()
        code, out = run_cli(["act", "--do", "click", "--index", "3", "--execute"], FakeConfig(driver))
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertEqual(json.loads(out)["error"]["code"], "SNAPSHOT_REQUIRED")
        self.assertEqual(actions_sent(driver), [])

    def test_token_form_needs_no_snapshot(self):
        driver = StubDriver()
        code, _ = run_cli(["act", "--do", "click", "--token", "s1:5", "--execute"], FakeConfig(driver))
        self.assertEqual(code, cli.EXIT_OK)
        sent = actions_sent(driver)
        self.assertEqual(sent[0]["element_token"], "s1:5")

    def test_unknown_action_is_refused(self):
        driver = StubDriver()
        code, out = run_cli(["act", "--do", "fly", "--token", "s1:5", "--execute"], FakeConfig(driver))
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertEqual(json.loads(out)["error"]["code"], "UNKNOWN_ACTION")

    def test_scroll_without_direction_is_refused(self):
        driver = StubDriver()
        code, out = run_cli(["act", "--do", "scroll", "--execute"], FakeConfig(driver))
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertEqual(json.loads(out)["error"]["code"], "MISSING_PARAM")

    def test_set_value_carries_value(self):
        driver = StubDriver()
        run_cli(
            [
                "act", "--do", "set_value", "--index", "2", "--snapshot", "sY",
                "--value", "新内容", "--execute",
                # R-3 之后假驱动读不到目标内容 → 默认被 fail-closed 拦，这里走双授权
                "--overwrite", "--allow-unverified",
            ],
            FakeConfig(driver),
        )
        sent = actions_sent(driver)
        # 写值前核心会先读一次目标（写值门禁），所以不能再假设第 0 条就是写值调用
        write = next(a for a in sent if "value" in a)
        self.assertEqual(write["value"], "新内容")
        self.assertEqual(write["snapshot_id"], "sY")

    def test_set_value_unverified_is_refused_by_default(self):
        """R-3：读不到目标内容时默认拒绝（fail-closed），不下发写值。"""
        driver = StubDriver()
        code, out = run_cli(
            ["act", "--do", "set_value", "--index", "2", "--snapshot", "sY", "--value", "新内容", "--execute"],
            FakeConfig(driver),
        )
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertEqual(json.loads(out)["error"]["code"], "WRITE_GATE_UNVERIFIED")
        self.assertFalse(any("value" in a for a in actions_sent(driver)), "被拦时不得下发写值")

    def test_activate_needs_no_element(self):
        driver = StubDriver()
        code, _ = run_cli(["act", "--do", "activate", "--execute"], FakeConfig(driver))
        self.assertEqual(code, cli.EXIT_OK)
        sent = actions_sent(driver)
        self.assertEqual(sent[0], {"pid": 100, "window_id": 200})


if __name__ == "__main__":
    unittest.main(verbosity=2)
