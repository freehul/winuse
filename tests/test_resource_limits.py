"""R-21：外来请求必须有资源上限——门面 schema 与核心入口**两处都设**。

报告 R-21 的缺口：
- 批量的 steps 没有 `maxItems`
- wait 接受零间隔与超长 timeout：零间隔会进 10000 次**无休眠**轮询（忙等吃 CPU）
- 注册表递归参数没有硬上限
- stdio 输入行没有长度限制
- 结果裁剪发生在**昂贵驱动调用完成之后**（先花完资源再裁，等于没防）

立场：schema 是给守规矩的客户端的提示，核心入口才是真正的边界——所以两处都要设。
"""

from __future__ import annotations

import unittest

from winuse import limit as limit_mod
from winuse.core import registry as reg_mod
from winuse.core import wait_for as wait_mod
from winuse.core.multi_input import run_multi
from winuse.driver.errors import WinUseError
from winuse.mcp import server as S


class TestLimitsAreDeclared(unittest.TestCase):
    def test_constants_exist_and_are_sane(self):
        self.assertGreater(limit_mod.MAX_BATCH_STEPS, 0)
        self.assertGreater(limit_mod.MAX_WAIT_SECONDS, limit_mod.MIN_WAIT_INTERVAL_SECONDS)
        self.assertGreater(limit_mod.MIN_WAIT_INTERVAL_SECONDS, 0)
        self.assertGreater(limit_mod.MAX_REGISTRY_DEPTH, 0)
        self.assertGreater(limit_mod.MAX_STDIO_LINE_BYTES, 0)


class TestBatchSteps(unittest.TestCase):
    def test_too_many_steps_is_refused_before_any_driver_call(self):
        class Driver:
            def __init__(self):
                self.calls = []

            def call(self, tool, args=None):
                self.calls.append(tool)
                return {"ok": True}

        driver = Driver()
        steps = [{"action": "click", "element_index": 0}] * (limit_mod.MAX_BATCH_STEPS + 1)
        with self.assertRaises(WinUseError) as ctx:
            run_multi(driver, steps, {"pid": 1, "window_id": 2}, "s1", execute=True, confirmed=True)
        self.assertEqual(ctx.exception.code, "PARAM_OUT_OF_RANGE")
        self.assertEqual(driver.calls, [], "超限必须在任何驱动调用之前拒掉")

    def test_schema_declares_max_items(self):
        tool = next(t for t in S.tool_catalog() if t["name"] == "winuse_multi")
        steps = tool["inputSchema"]["properties"]["steps"]
        self.assertEqual(steps.get("maxItems"), limit_mod.MAX_BATCH_STEPS)


class TestWaitLimits(unittest.TestCase):
    def test_zero_interval_is_lifted_to_minimum(self):
        """零间隔会变成忙轮询：抬到最小值，并在回执里说明被抬过。"""
        spec = wait_mod.WaitSpec(kind="window_appear", app="x", timeout_seconds=1, interval_seconds=0)
        self.assertGreaterEqual(wait_mod.effective_interval(spec), limit_mod.MIN_WAIT_INTERVAL_SECONDS)

    def test_over_max_timeout_is_refused(self):
        spec = wait_mod.WaitSpec(
            kind="window_appear", app="x", timeout_seconds=limit_mod.MAX_WAIT_SECONDS + 1
        )
        with self.assertRaises(WinUseError) as ctx:
            wait_mod.wait_for(FakeDriver(), spec, sleep=lambda _s: None)
        self.assertEqual(ctx.exception.code, "PARAM_OUT_OF_RANGE")

    def test_schema_declares_bounds(self):
        tool = next(t for t in S.tool_catalog() if t["name"] == "winuse_wait")
        props = tool["inputSchema"]["properties"]
        self.assertEqual(props["timeout_seconds"].get("maximum"), limit_mod.MAX_WAIT_SECONDS)
        self.assertEqual(props["interval_seconds"].get("minimum"), limit_mod.MIN_WAIT_INTERVAL_SECONDS)


class FakeDriver:
    def call(self, tool, args=None):
        return {"windows": [], "elements": []}


class TestRegistryLimits(unittest.TestCase):
    def test_depth_and_subkeys_are_clamped_with_note(self):
        backend = reg_mod._default_backend.__self__ if False else None  # 只为可读性占位
        tree = {
            ("HKCU", ""): {"subkeys": ["A"], "values": []},
            ("HKCU", "A"): {"subkeys": [], "values": []},
        }

        class FakeBackend:
            def __init__(self, tree):
                self.tree = tree

            def open_key(self, hive, path):
                if (hive, path) not in self.tree:
                    raise reg_mod.RegistryKeyNotFound(f"键不存在：{hive}\\{path}")
                return (hive, path)

            def enum_values(self, key):
                return list(self.tree[key].get("values", []))

            def enum_subkeys(self, key):
                return list(self.tree[key].get("subkeys", []))

            def close(self, key):
                pass

        out = reg_mod.read_registry(
            reg_mod.RegReadSpec(hive="HKCU", path="", recurse=True,
                                max_depth=999, max_subkeys=10**9),
            FakeBackend(tree),
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("clamped"), {
            "max_depth": limit_mod.MAX_REGISTRY_DEPTH,
            "max_subkeys": limit_mod.MAX_REGISTRY_SUBKEYS,
        })
        self.assertIsNone(backend)

    def test_schema_declares_bounds(self):
        tool = next(t for t in S.tool_catalog() if t["name"] == "winuse_registry")
        props = tool["inputSchema"]["properties"]
        self.assertEqual(props["max_depth"].get("maximum"), limit_mod.MAX_REGISTRY_DEPTH)
        self.assertEqual(props["max_subkeys"].get("maximum"), limit_mod.MAX_REGISTRY_SUBKEYS)


class TestStdioLineLimit(unittest.TestCase):
    def test_oversized_line_is_refused_without_crashing(self):
        import io

        raw = io.StringIO("x" * (limit_mod.MAX_STDIO_LINE_BYTES + 10) + "\n")
        out = io.StringIO()
        err = io.StringIO()
        code = S.serve(stdin=raw, stdout=out, stderr=err)
        self.assertEqual(code, 0)
        line = out.getvalue().strip().splitlines()
        self.assertTrue(line, "超长行必须有回包，不能静默吞掉")
        self.assertIn("-32600", line[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestErrorRedactionAtSource(unittest.TestCase):
    r"""R-20 补：错误对象出口统一脱敏。

    真机踩到的具体例子：`HKCU\Software\Classes\AppUserModelId` 下**键名本身**
    就含本机用户目录，错误消息原样带出去就泄露了用户名与目录结构。
    放在 `WinUseError.to_dict()` 上，所有门面自动受益。
    """

    def test_to_dict_masks_user_paths(self):
        import os

        from winuse.driver.errors import WindowNotFound

        user_root = "C:" + os.sep + "Users" + os.sep + "someone"
        key = os.path.join(user_root, "AppData", "Local", "Demo", "app.exe")
        exc = WindowNotFound(f"键不存在：{key}", f"原始：{key}")
        payload = exc.to_dict()
        self.assertNotIn(user_root, payload["message"])
        self.assertNotIn(user_root, payload.get("detail", ""))
        self.assertIn("<USER_HOME>", payload["message"])

    def test_raw_message_is_preserved_for_debugging(self):
        import os

        from winuse.driver.errors import WindowNotFound

        user_root = "C:" + os.sep + "Users" + os.sep + "someone"
        exc = WindowNotFound(f"路径：{user_root}")
        self.assertIn(user_root, exc.message, "原始属性保持完整，排查时仍要看得到")
