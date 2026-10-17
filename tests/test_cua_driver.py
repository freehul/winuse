"""驱动封装层单测 —— 全程不接触真实驱动与桌面。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.driver import (  # noqa: E402
    MIN_VERSION,
    CuaDriver,
    DriverError,
    DriverNotFound,
    DriverTimeout,
    DriverVersionTooOld,
    candidate_paths,
    check_min_version,
    parse_version,
)

FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_driver.py")]


def driver(**kwargs) -> CuaDriver:
    params = {"command": FAKE, "timeout_s": 20.0}
    params.update(kwargs)
    return CuaDriver(**params)


class TestPureHelpers(unittest.TestCase):
    def test_parse_version_variants(self):
        self.assertEqual(parse_version("cua-driver 0.28.2"), (0, 28, 2))
        self.assertEqual(parse_version("v1.2.3-beta"), (1, 2, 3))
        self.assertIsNone(parse_version("no version here"))

    def test_check_min_version(self):
        self.assertTrue(check_min_version((0, 28, 0)))
        self.assertTrue(check_min_version((0, 29, 1)))
        self.assertTrue(check_min_version((1, 0, 0)))
        self.assertFalse(check_min_version((0, 27, 9)))
        self.assertFalse(check_min_version((0, 6, 8)))
        self.assertEqual(MIN_VERSION, (0, 28, 0))

    def test_candidate_paths_puts_versioned_install_first(self):
        paths = candidate_paths(home=Path("/home/u"), local_appdata=Path("/home/u/AppData/Local"))
        self.assertTrue(paths[0].as_posix().endswith("packages/current/cua-driver.exe"))
        self.assertTrue(paths[1].as_posix().endswith("Programs/Cua/cua-driver/bin/cua-driver.exe"))

    def test_resolve_prefers_candidate_over_path(self):
        """PATH 里就算有驱动，也必须优先用版本化安装目录（旧副本劫持的教训）。"""
        d = driver(command=None)
        with mock.patch(
            "winuse.driver.cua.candidate_paths",
            return_value=[Path("/found/current/cua-driver.exe")],
        ), mock.patch("winuse.driver.cua.Path.is_file", return_value=True), mock.patch(
            "winuse.driver.cua.shutil.which", return_value="/path/cua-driver"
        ) as which:
            self.assertEqual([Path(p).as_posix() for p in d.resolve()],
                             ["/found/current/cua-driver.exe"])
            which.assert_not_called()

    def test_resolve_falls_back_to_path_then_raises(self):
        d = driver(command=None)
        with mock.patch("winuse.driver.cua.candidate_paths", return_value=[]), mock.patch(
            "winuse.driver.cua.shutil.which", return_value="/path/cua-driver"
        ):
            self.assertEqual(d.resolve(), ["/path/cua-driver"])

        d2 = driver(command=None)
        with mock.patch("winuse.driver.cua.candidate_paths", return_value=[]), mock.patch(
            "winuse.driver.cua.shutil.which", return_value=None
        ):
            with self.assertRaises(DriverNotFound):
                d2.resolve()

    def test_parse_output_strips_warn_lines(self):
        out = 'WARN 缓存未刷新\n{"a": 1}\n'
        self.assertEqual(CuaDriver.parse_output(out), {"a": 1})

    def test_parse_output_rejects_empty_and_garbage(self):
        with self.assertRaises(DriverError) as ctx:
            CuaDriver.parse_output("WARN 只有警告\n")
        self.assertEqual(ctx.exception.code, "DRIVER_ERROR")
        with self.assertRaises(DriverError):
            CuaDriver.parse_output("这不是 JSON")


class TestDriverCalls(unittest.TestCase):
    def test_version(self):
        self.assertEqual(driver().version_str(), "0.28.2")

    def test_ensure_ready_passes_on_new_enough_version(self):
        self.assertEqual(driver().ensure_ready(), (0, 28, 2))

    def test_ensure_ready_rejects_old_version(self):
        d = driver()
        with mock.patch.object(CuaDriver, "version", return_value=(0, 6, 8)):
            with self.assertRaises(DriverVersionTooOld) as ctx:
                d.ensure_ready()
        self.assertEqual(ctx.exception.code, "DRIVER_VERSION_TOO_OLD")

    def test_call_returns_payload_and_skips_version_check_when_asked(self):
        data = driver().call("list_windows", {"x": 1})
        self.assertTrue(data["ok"])
        self.assertEqual(data["tool"], "list_windows")
        self.assertEqual(data["args"], {"x": 1})

    def test_call_nonzero_exit_raises_driver_error(self):
        with self.assertRaises(DriverError) as ctx:
            driver().call("boom")
        self.assertEqual(ctx.exception.code, "DRIVER_ERROR")
        self.assertIn("boom", ctx.exception.message)

    def test_call_unparsable_output_raises(self):
        with self.assertRaises(DriverError):
            driver().call("garbage")

    def test_call_timeout_raises_driver_timeout(self):
        with self.assertRaises(DriverTimeout) as ctx:
            driver().call("slow", timeout_s=0.5)
        self.assertEqual(ctx.exception.code, "DRIVER_TIMEOUT")

    def test_health_reports_ok(self):
        health = driver().health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["version"], "0.28.2")
        self.assertEqual(health["min_version"], "0.28.0")

    def test_health_reports_error_for_missing_driver(self):
        d = CuaDriver(command=["definitely-not-a-real-binary-xyz"])
        health = d.health()
        self.assertFalse(health["ok"])
        self.assertIn("error", health)


if __name__ == "__main__":
    unittest.main(verbosity=2)
