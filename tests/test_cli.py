"""CLI 单测：用假驱动跑通 doctor（PR-1 的验收标准之一，全程不碰桌面）。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse import cli  # noqa: E402

FAKE_DRIVER = Path(__file__).resolve().parent / "fake_driver.py"


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.env = self.tmp / ".env"
        self.env.write_text(
            f'WINUSE_CUA_DRIVER="{sys.executable}" {FAKE_DRIVER}\nWINUSE_TIMEOUT_S=20\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_doctor_ok_with_probe(self):
        code, out, err = run_cli(["--env", str(self.env), "doctor"])
        self.assertEqual(code, cli.EXIT_OK, msg=f"stderr={err}")
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertTrue(report["checks"]["driver"]["ok"])
        self.assertEqual(report["checks"]["driver"]["version"], "0.28.2")
        self.assertTrue(report["checks"]["probe"]["ok"])
        self.assertEqual(report["checks"]["probe"]["tool"], cli.DOCTOR_PROBE_TOOL)

    def test_doctor_no_probe_skips_real_call(self):
        code, out, _ = run_cli(["--env", str(self.env), "doctor", "--no-probe"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("probe", json.loads(out)["checks"])

    def test_doctor_fails_cleanly_when_driver_missing(self):
        bad = self.tmp / "bad.env"
        bad.write_text("WINUSE_CUA_DRIVER=definitely-not-a-real-binary-xyz\n", encoding="utf-8")
        code, out, err = run_cli(["--env", str(bad), "doctor"])
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn("驱动检查失败", err)

    def test_doctor_fails_cleanly_on_probe_error(self):
        bad = self.tmp / "bad.env"
        bad.write_text(f'WINUSE_CUA_DRIVER="{sys.executable}" {FAKE_DRIVER}\n', encoding="utf-8")
        original = cli.DOCTOR_PROBE_TOOL
        cli.DOCTOR_PROBE_TOOL = "boom"
        try:
            code, out, _ = run_cli(["--env", str(bad), "doctor"])
        finally:
            cli.DOCTOR_PROBE_TOOL = original
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertFalse(json.loads(out)["checks"]["probe"]["ok"])

    def test_doctor_rejects_bad_config(self):
        bad = self.tmp / "bad.env"
        bad.write_text("WINUSE_TIMEOUT_S=abc\n", encoding="utf-8")
        code, out, err = run_cli(["--env", str(bad), "doctor"])
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("配置检查失败", err)


class TestCallCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.env = self.tmp / ".env"
        self.env.write_text(
            f'WINUSE_CUA_DRIVER="{sys.executable}" {FAKE_DRIVER}\n', encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_call_passes_payload_through(self):
        code, out, _ = run_cli(["--env", str(self.env), "call", "list_windows", '{"pid": 7}', "--unsafe-driver-call"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["args"], {"pid": 7})

    def test_call_defaults_to_empty_object(self):
        code, out, _ = run_cli(["--env", str(self.env), "call", "list_windows", "--unsafe-driver-call"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["args"], {})

    def test_call_rejects_non_json_payload(self):
        code, _, err = run_cli(["--env", str(self.env), "call", "list_windows", "not-json", "--unsafe-driver-call"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("不是合法 JSON", err)

    def test_call_rejects_json_array_payload(self):
        code, _, err = run_cli(["--env", str(self.env), "call", "list_windows", "[1,2]", "--unsafe-driver-call"])
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("JSON 对象", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
