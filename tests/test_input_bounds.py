"""报告 Optional 1 / 2：输入边界（`max_elements`、`top_processes`、OCR 图片）。

报告原文（Optional）：
- 「`wait_for` 对负数/零间隔静默归零，`interval=0` 会进最多 10000 次无休眠轮询；
  为 timeout、interval、top_n、max_elements 设类型、最小值和最大值」——
  timeout/interval 已在 R-21 落地，这里补 `top_n` 与 `max_elements` 的边界。
- 「`ocr.py` 的 `read_bytes()` 无文件大小上限，MCP 可请求超大图片导致内存和 Base64 膨胀；
  增加文件大小上限、扩展名/格式检查和更明确的错误码」。

**与代码不符之处（如实记录）**：`ocr.py` 里**没有** `read_bytes()`——OCR 只把图片
**路径**（base64 编码）交给 PowerShell，脚本内容与图片大小无关，所以"Base64 膨胀"
不会发生。真实风险在 PowerShell 侧读图：超大图会让引擎吃内存/超时。因此这里按
"图片大小与格式上限"来加固，而不是给不存在的函数加参数。
"""

from __future__ import annotations

import tempfile
import unittest

import pytest

from winuse import limit as limit_mod
from winuse.core import ocr as ocr_mod
from winuse.core.state import StateOptions, trim_elements
from winuse.mcp import server as S


class TestStateMaxElements(unittest.TestCase):
    def _elements(self, count: int) -> list[dict]:
        return [
            {"index": i, "token": f"t{i}", "actions": ["invoke"], "label": "占位", "value": ""}
            for i in range(count)
        ]

    def test_negative_max_elements_is_floored_to_zero(self):
        kept, stats = trim_elements(self._elements(5), StateOptions(max_elements=-3))
        self.assertEqual(kept, [])
        self.assertEqual(stats["cap"], 0, "负数不能原样进 stats（会被当成有效上限）")

    def test_huge_max_elements_is_capped(self):
        kept, stats = trim_elements(
            self._elements(limit_mod.MAX_STATE_ELEMENTS + 50),
            StateOptions(max_elements=10**9),
        )
        self.assertEqual(len(kept), limit_mod.MAX_STATE_ELEMENTS)
        self.assertEqual(stats["cap"], limit_mod.MAX_STATE_ELEMENTS)

    def test_normal_value_is_untouched(self):
        kept, stats = trim_elements(self._elements(5), StateOptions(max_elements=3))
        self.assertEqual(len(kept), 3)
        self.assertEqual(stats["cap"], 3)

    def test_schema_declares_bounds(self):
        tool = next(t for t in S.tool_catalog() if t["name"] == "winuse_state")
        prop = tool["inputSchema"]["properties"]["max_elements"]
        self.assertEqual(prop.get("minimum"), 0)
        self.assertEqual(prop.get("maximum"), limit_mod.MAX_STATE_ELEMENTS)


class TestTopProcessesBounds(unittest.TestCase):
    def test_schema_declares_bounds(self):
        tool = next(t for t in S.tool_catalog() if t["name"] == "winuse_system")
        prop = tool["inputSchema"]["properties"]["top_processes"]
        self.assertEqual(prop.get("minimum"), 0)
        self.assertEqual(prop.get("maximum"), limit_mod.MAX_TOP_PROCESSES)


class TestOcrImageBounds(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp = tmp_path

    def _write(self, name: str, size: int) -> str:
        path = self.tmp / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * max(0, size - 8))
        return str(path)

    def test_oversized_image_is_refused_with_a_stable_code(self):
        big = self._write("big.png", limit_mod.MAX_OCR_IMAGE_BYTES + 1)
        with pytest.raises(ocr_mod.OcrImageTooLarge) as ctx:
            ocr_mod.validate_ocr_spec(ocr_mod.OcrSpec(image_path=big))
        self.assertEqual(ctx.value.code, "OCR_IMAGE_TOO_LARGE")

    def test_format_whitelist_rejects_unknown_extension(self):
        weird = self._write("shot.exe", 1024)
        with pytest.raises(ocr_mod.OcrImageUnsupported) as ctx:
            ocr_mod.validate_ocr_spec(ocr_mod.OcrSpec(image_path=weird))
        self.assertEqual(ctx.value.code, "OCR_IMAGE_UNSUPPORTED")

    def test_normal_image_passes(self):
        ok = self._write("shot.png", 2048)
        ocr_mod.validate_ocr_spec(ocr_mod.OcrSpec(image_path=ok))  # 不抛即通过

    def test_size_limit_is_shared_with_the_limits_module(self):
        self.assertGreater(limit_mod.MAX_OCR_IMAGE_BYTES, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
