"""Optional-5：审计"轮转 + 追加写"必须能并发（多 agent 同时写一个日志）。

报告原文：「审计轮转 `rotate_if_needed()` 没有跨进程锁，多 agent 同时写入时可能
发生轮转竞态；至少增加并发写入测试，必要时使用 Windows 文件锁/原子替换策略。」

竞态长什么样：两个写者同时判断"超限了" → 都去 `target.replace(.1)` →
其中一个把**另一个刚创建的新文件**也搬进归档，或两份归档互相覆盖 → 丢记录。
表现为"审计日志偶尔少几条"——最难查的一类问题（正常路径完全看不出来）。

处置：把「判断轮转 + 追加写」放进一个**跨进程文件锁**的临界区（stdlib：
Windows 用 `msvcrt.locking`，POSIX 用 `fcntl.flock`，零第三方依赖），
并写并发测试钉住"一行都不该丢"。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from winuse import audit as audit_mod

WRITERS = 8
LINES_PER_WRITER = 25
#: 每行约 60+ 字节 → 上限设小一点，让整轮测试里发生**多次**轮转；
#: KEEP 要够大，否则"测试通过"可能只是因为旧归档被正常删掉了（轮转语义允许丢最旧的）
MAX_BYTES = 400
KEEP = 200


class TestConcurrentRotation(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)
        self.path = self.tmp / "audit.jsonl"
        # 轮转策略只从项目内 .env 读：不写这份文件的话根本不会轮转，测不到竞态
        self.env = self.tmp / ".env"
        self.env.write_text(
            f"WINUSE_AUDIT_MAX_BYTES={MAX_BYTES}\nWINUSE_AUDIT_KEEP={KEEP}\n",
            encoding="utf-8",
        )

    def _write_one(self, writer: int, index: int) -> None:
        audit_mod.record(
            {"event": "act.dry_run", "action": "click", "writer": writer, "seq": index},
            path=self.path,
            env_path=str(self.env),
        )

    def test_nothing_is_lost_under_concurrency(self):
        def burst(writer: int) -> None:
            for index in range(LINES_PER_WRITER):
                self._write_one(writer, index)

        with ThreadPoolExecutor(max_workers=WRITERS) as pool:
            list(pool.map(burst, range(WRITERS)))

        seen = self._all_entries()
        expected = WRITERS * LINES_PER_WRITER
        self.assertEqual(
            len(seen), expected, f"并发写入丢了记录：期望 {expected} 条，实际 {len(seen)} 条"
        )
        keys = {(e.get("writer"), e.get("seq")) for e in seen}
        self.assertEqual(len(keys), expected, "有重复或有丢失（键集合数量对不上）")

    def test_rotation_actually_happened(self):
        """前提校验：如果压根没轮转过，上面的用例就没在测竞态。"""
        with ThreadPoolExecutor(max_workers=WRITERS) as pool:
            list(pool.map(lambda w: [self._write_one(w, i) for i in range(LINES_PER_WRITER)],
                          range(WRITERS)))
        files = audit_mod.rotated_paths(self.path)
        self.assertTrue(any(p.is_file() for p in files), "应当产生过至少一份归档")

    def test_lock_helper_is_reentrant_within_a_process(self):
        """同进程内串行复用不能死锁（Windows 的文件锁对此很敏感）。"""
        for _ in range(3):
            with audit_mod._audit_lock(self.path):
                pass

    def test_entries_are_valid_json_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=WRITERS) as pool:
            list(pool.map(
                lambda w: [self._write_one(w, i) for i in range(LINES_PER_WRITER)],
                range(WRITERS),
            ))
        for raw in self._raw_lines():
            json.loads(raw)  # 抛异常即失败：半行/交错行说明写不是原子的

    def _all_entries(self) -> list[dict]:
        return [json.loads(x) for x in self._raw_lines()]

    def _raw_lines(self) -> list[str]:
        """跨轮转读回所有行（当前文件 + 归档），并按写入顺序无关地汇总。"""
        out: list[str] = []
        for candidate in [self.path, *audit_mod.rotated_paths(self.path)]:
            if candidate.is_file():
                out.extend(
                    line for line in candidate.read_text(encoding="utf-8").splitlines() if line.strip()
                )
        return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
