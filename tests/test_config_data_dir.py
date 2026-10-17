"""Optional-4：`data_dir` 是**休眠契约**——声明了却没人消费。

报告原文：「`winuse/config.py` 声明了 `data_dir`，但审计路径实际由
`audit.default_path()` 单独决定，当前没有生产消费者；应接通或删除该配置，
避免形成休眠契约。」

选择**接通**（而不是删除）：`.env` 里写 `WINUSE_DATA_DIR` 却对审计落盘毫无影响，
是"配置骗人"——比没有这项配置更糟。接通后规则变成一句话：

    审计文件 = WINUSE_AUDIT_PATH（显式路径）> WINUSE_DATA_DIR/audit.jsonl > ~/.winuse/audit.jsonl
"""

from __future__ import annotations

import unittest
from pathlib import Path

from winuse import audit as audit_mod
from winuse.config import Config


class TestDataDirWiring(unittest.TestCase):
    def _env(self, tmp_path: Path, body: str) -> Path:
        env = tmp_path / ".env"
        env.write_text(body, encoding="utf-8")
        return env

    def test_data_dir_drives_the_audit_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            data_dir = tmp / "winuse-data"
            env = self._env(tmp, f"WINUSE_DATA_DIR={data_dir.as_posix()}\n")
            self.assertEqual(audit_mod.default_path(env), data_dir / audit_mod.AUDIT_FILENAME)

    def test_explicit_audit_path_wins_over_data_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            explicit = tmp / "custom.jsonl"
            env = self._env(
                tmp,
                f"WINUSE_DATA_DIR={(tmp / 'data').as_posix()}\n"
                f"WINUSE_AUDIT_PATH={explicit.as_posix()}\n",
            )
            self.assertEqual(audit_mod.default_path(env), explicit)

    def test_fallback_is_the_home_dir(self):
        self.assertEqual(
            audit_mod.default_path(None),
            Path.home() / audit_mod.DEFAULT_DIRNAME / audit_mod.AUDIT_FILENAME,
        )

    def test_config_and_audit_share_the_same_env_key(self):
        """键名只有一处出处：否则改了一边、另一边静默失效。"""
        self.assertEqual(Config.__module__ and audit_mod.ENV_DATA_DIR, "WINUSE_DATA_DIR")
        import winuse.config as config_mod

        self.assertEqual(config_mod.ENV_DATA_DIR, audit_mod.ENV_DATA_DIR)

    def test_config_still_reports_data_dir(self):
        cfg = Config.load(env_path=None)
        self.assertEqual(cfg.data_dir, Path.home() / audit_mod.DEFAULT_DIRNAME)


if __name__ == "__main__":
    unittest.main(verbosity=2)
