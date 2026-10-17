"""配置层单测：解析、默认值、校验，以及"绝不读进程环境变量"的守护测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.config import (  # noqa: E402
    DEFAULT_DATA_DIR,
    DEFAULT_STATE_MAX_ELEMENTS,
    DEFAULT_TIMEOUT_S,
    ENV_TIMEOUT_S,
    Config,
    parse_env_file,
    split_command,
    unquote,
)
from winuse.driver.errors import ConfigError  # noqa: E402


class TestParseEnvFile(unittest.TestCase):
    def test_handles_comments_blanks_export(self):
        text = (
            "# 注释\n"
            "\n"
            "WINUSE_TIMEOUT_S=30\n"
            "export WINUSE_DATA_DIR=/tmp/winuse\n"
            "WINUSE_STATE_MAX_ELEMENTS=50\n"
            "NOT_A_PAIR\n"
        )
        got = parse_env_file(text)
        self.assertEqual(got["WINUSE_TIMEOUT_S"], "30")
        self.assertEqual(got["WINUSE_DATA_DIR"], "/tmp/winuse")
        self.assertEqual(got["WINUSE_STATE_MAX_ELEMENTS"], "50")
        self.assertNotIn("NOT_A_PAIR", got)

    def test_values_keep_quotes_raw(self):
        """引号不在这一层剥——标量走 unquote、命令行走 split_command。"""
        got = parse_env_file('A="x y"\nB=\'z\'\n')
        self.assertEqual(got["A"], '"x y"')
        self.assertEqual(got["B"], "'z'")

    def test_value_with_equals_sign_kept_whole(self):
        self.assertEqual(parse_env_file("K=a=b")["K"], "a=b")


class TestUnquote(unittest.TestCase):
    def test_strips_only_fully_wrapped(self):
        self.assertEqual(unquote('"x y"'), "x y")
        self.assertEqual(unquote("'z'"), "z")
        self.assertEqual(unquote('"a" b'), '"a" b')
        self.assertEqual(unquote("plain"), "plain")
        self.assertEqual(unquote(""), "")


class TestSplitCommand(unittest.TestCase):
    def test_windows_path_backslashes_are_literal(self):
        """守护测试：反斜杠不能被当转义符吃掉（shlex POSIX 模式的坑）。"""
        self.assertEqual(
            split_command(r"C:\tools\cua-driver.exe"), (r"C:\tools\cua-driver.exe",)
        )
        self.assertEqual(
            split_command(r'python.exe D:\work\proj\tests\fake_driver.py'),
            ("python.exe", r"D:\work\proj\tests\fake_driver.py"),
        )

    def test_quoted_path_with_spaces(self):
        self.assertEqual(
            split_command(r'"C:\Program Files\cua\cua-driver.exe" --quiet'),
            (r"C:\Program Files\cua\cua-driver.exe", "--quiet"),
        )
        self.assertEqual(
            split_command("'/opt/cua/cua-driver' -v"), ("/opt/cua/cua-driver", "-v")
        )

    def test_blank_and_unclosed_quote(self):
        self.assertIsNone(split_command(""))
        self.assertIsNone(split_command("   "))
        with self.assertRaises(ConfigError):
            split_command('"unclosed')


class TestConfigLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text: str) -> Path:
        p = self.tmp / ".env"
        p.write_text(text, encoding="utf-8")
        return p

    def test_defaults_when_env_missing(self):
        cfg = Config.load(env_path=self.tmp / "不存在.env")
        self.assertIsNone(cfg.cua_driver)
        self.assertEqual(cfg.timeout_s, DEFAULT_TIMEOUT_S)
        self.assertEqual(cfg.state_max_elements, DEFAULT_STATE_MAX_ELEMENTS)
        self.assertEqual(str(cfg.data_dir), str(Path(DEFAULT_DATA_DIR).expanduser()))
        self.assertIsNone(cfg.env_path)

    def test_values_are_applied(self):
        path = self._write(
            "WINUSE_CUA_DRIVER=C:/x/cua-driver.exe\n"
            "WINUSE_TIMEOUT_S=45\n"
            "WINUSE_DATA_DIR=~/winuse-data\n"
            "WINUSE_STATE_MAX_ELEMENTS=77\n"
        )
        cfg = Config.load(env_path=path)
        self.assertEqual(cfg.cua_driver, ("C:/x/cua-driver.exe",))
        self.assertEqual(cfg.timeout_s, 45.0)
        self.assertEqual(cfg.state_max_elements, 77)
        self.assertTrue(str(cfg.data_dir).endswith("winuse-data"))
        self.assertNotIn("~", str(cfg.data_dir))
        self.assertEqual(cfg.env_path, path)

    def test_quoted_command_with_args_is_split(self):
        path = self._write('WINUSE_CUA_DRIVER="C:/Program Files/cua/cua-driver.exe" --quiet\n')
        self.assertEqual(
            Config.load(env_path=path).cua_driver,
            ("C:/Program Files/cua/cua-driver.exe", "--quiet"),
        )

    def test_invalid_numbers_raise_config_error(self):
        for line in ("WINUSE_TIMEOUT_S=abc", "WINUSE_TIMEOUT_S=0", "WINUSE_STATE_MAX_ELEMENTS=-3"):
            with self.subTest(line=line):
                path = self._write(line + "\n")
                with self.assertRaises(ConfigError):
                    Config.load(env_path=path)

    def test_does_not_read_process_environment(self):
        """守护测试：进程环境变量不能影响配置（项目纪律：变量走项目配置文件）。"""
        os.environ[ENV_TIMEOUT_S] = "999"
        os.environ["WINUSE_DATA_DIR"] = "/should-be-ignored"
        try:
            cfg = Config.load(env_path=self.tmp / "不存在.env")
            self.assertEqual(cfg.timeout_s, DEFAULT_TIMEOUT_S)
            self.assertNotEqual(str(cfg.data_dir), "/should-be-ignored")
        finally:
            os.environ.pop(ENV_TIMEOUT_S, None)
            os.environ.pop("WINUSE_DATA_DIR", None)

    def test_describe_is_json_friendly(self):
        path = self._write("WINUSE_TIMEOUT_S=10\n")
        described = Config.load(env_path=path).describe()
        self.assertEqual(described["timeout_s"], 10.0)
        self.assertIn("data_dir", described)
        self.assertEqual(described["cua_driver"], None)

    def test_driver_factory_carries_command_and_timeout(self):
        path = self._write("WINUSE_CUA_DRIVER=C:/x/cua-driver.exe\nWINUSE_TIMEOUT_S=15\n")
        d = Config.load(env_path=path).driver()
        self.assertEqual(d.resolve(), ["C:/x/cua-driver.exe"])
        self.assertEqual(d.timeout_s, 15.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
