"""winuse_ocr 单测：命令构造、引擎输出解析、失败分流。

全部走纯函数 + 注入的假 runner，**不启动 PowerShell、不碰桌面**。
WinRT 的真实输出形态用自造的样例字符串覆盖三类：正常 / 空结果 / 异常。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.ocr import (  # noqa: E402
    OUTPUT_MARKER,
    OcrEngineUnavailable,
    OcrImageNotFound,
    OcrInputMissing,
    OcrOutputInvalid,
    OcrRunFailed,
    OcrSpec,
    build_ocr_command,
    build_ocr_script,
    compact_ocr_text,
    parse_ocr_output,
    run_ocr,
)

#: 样例图片路径（占位值，不是本机真实路径）
SAMPLE_IMAGE = "D:/samples/invoice.png"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_arg(argv: list[str]) -> str:
    """把 `-EncodedCommand` 还原成脚本（其编码为 UTF-16LE base64）。"""
    return base64.b64decode(argv[-1]).decode("utf-16-le")


def marked(payload) -> str:
    """模拟引擎输出：标记 + 紧凑 JSON（与脚本里的 ConvertTo-Json -Compress 同形）。"""
    return OUTPUT_MARKER + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# 正常：两行文字，每行带词与像素框
NORMAL_OUTPUT = (
    "WARN 2026-01-01 00:00:00 引擎缓存未刷新，以下内容仅供参考\n"
    + marked(
        {
            "ok": True,
            "text": "发票号码\n12345",
            "lines": [
                {
                    "text": "发票号码",
                    "words": [
                        {"text": "发票", "bounds": {"x": 10, "y": 20, "width": 60, "height": 18}},
                        {"text": "号码", "bounds": {"x": 72, "y": 20, "width": 60, "height": 18}},
                    ],
                },
                {
                    "text": "12345",
                    "words": [{"text": "12345", "bounds": {"x": 10, "y": 44, "width": 80, "height": 18}}],
                },
            ],
        }
    )
    + "\n"
)

# PS 5.1 的坑：单元素数组被序列化成对象（不是数组）——行与词两层都被压平
SINGLE_ELEMENT_OUTPUT = marked(
    {
        "ok": True,
        "text": "只有一行",
        "lines": {
            "text": "只有一行",
            "words": {"text": "只有一行", "bounds": {"x": 1, "y": 2, "width": 3, "height": 4}},
        },
    }
)

# 空结果：引擎真的跑了，图上确实没字（这是唯一合法的空文本来源）
EMPTY_OUTPUT = marked({"ok": True, "text": "", "lines": []})
EMPTY_OUTPUT_NO_LINES = marked({"ok": True, "text": ""})

# 真机样例（引擎口径，数据为拼造的假样张）：源图写的是 "WinUse OCR 验收 12345"，
# WinRT 回来是 "WinUse OCR 验 收 1 2345"——汉字之间插了空格，数字还被拆了段。
REAL_ENGINE_TEXT = "WinUse OCR 验 收 1 2345"
REAL_SOURCE_TEXT = "WinUse OCR 验收 12345"
REAL_MACHINE_OUTPUT = marked(
    {
        "ok": True,
        "text": REAL_ENGINE_TEXT,
        "lines": [
            {
                "text": REAL_ENGINE_TEXT,
                "words": [
                    {"text": "WinUse", "bounds": {"x": 10, "y": 8, "width": 74, "height": 20}},
                    {"text": "OCR", "bounds": {"x": 92, "y": 8, "width": 46, "height": 20}},
                    {"text": "验", "bounds": {"x": 146, "y": 8, "width": 20, "height": 20}},
                    {"text": "收", "bounds": {"x": 166, "y": 8, "width": 20, "height": 20}},
                    {"text": "1", "bounds": {"x": 190, "y": 8, "width": 10, "height": 20}},
                    {"text": "2345", "bounds": {"x": 204, "y": 8, "width": 44, "height": 20}},
                ],
            }
        ],
    }
)

# 真机样例二（同一次实测的真实形态，数据为拼造的假样张）：
# 源图两行，引擎的 `Text` 把它们用**一个空格**粘成了一行，真正的行边界只在 `lines` 里。
REAL_MULTILINE_SOURCE = "WinUse OCR 验收 12345\n订单号 12345 金额 6789"
REAL_MULTILINE_OUTPUT = marked(
    {
        "ok": True,
        "text": "WinUse OCR 验 收 12345 订 单 号 12345 金 额 6789",
        "lines": [
            {
                "text": "WinUse OCR 验 收 12345",
                "words": [
                    {"text": "WinUse", "bounds": {"x": 24, "y": 20, "width": 74, "height": 20}},
                    {"text": "OCR", "bounds": {"x": 106, "y": 20, "width": 46, "height": 20}},
                    {"text": "验", "bounds": {"x": 160, "y": 20, "width": 20, "height": 20}},
                    {"text": "收", "bounds": {"x": 180, "y": 20, "width": 20, "height": 20}},
                    {"text": "12345", "bounds": {"x": 204, "y": 20, "width": 44, "height": 20}},
                ],
            },
            {
                "text": "订 单 号 12345 金 额 6789",
                "words": [
                    {"text": "订", "bounds": {"x": 24, "y": 80, "width": 20, "height": 20}},
                    {"text": "单", "bounds": {"x": 44, "y": 80, "width": 20, "height": 20}},
                    {"text": "号", "bounds": {"x": 64, "y": 80, "width": 20, "height": 20}},
                    {"text": "12345", "bounds": {"x": 88, "y": 80, "width": 44, "height": 20}},
                    {"text": "金", "bounds": {"x": 136, "y": 80, "width": 20, "height": 20}},
                    {"text": "额", "bounds": {"x": 156, "y": 80, "width": 20, "height": 20}},
                    {"text": "6789", "bounds": {"x": 180, "y": 80, "width": 40, "height": 20}},
                ],
            },
        ],
    }
)

# 异常一：引擎按该语言建不起来（语言包缺失 / WinRT 不可用）
ENGINE_UNAVAILABLE_OUTPUT = marked(
    {
        "ok": False,
        "code": "OCR_ENGINE_UNAVAILABLE",
        "message": "OCR 引擎无法按语言 zh-Hans 创建（语言包未安装或 WinRT 不可用）",
        "languages": ["en-US"],
    }
)

# 异常二：脚本执行中抛错（图片损坏等）
RUN_FAILED_OUTPUT = marked({"ok": False, "code": "OCR_RUN_FAILED", "message": "文件损坏"})

# 异常三：连标记都没有（powershell.exe 不在 / 进程异常退出）
NO_MARKER_OUTPUT = "powershell.exe : 无法将“powershell.exe”项识别为 cmdlet\n"


class FakeRunner:
    """假 runner：记录 argv，按序返回预设结果（绝不真起进程）。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if not self.results:
            return 0, "", ""
        return self.results.pop(0)


def fake_which(found: bool):
    """假 which：不查真实环境（本机装没装 powershell 不该影响单测结果）。"""
    return (lambda name: name) if found else (lambda name: None)


class TestBuildOcrCommand(unittest.TestCase):
    def test_uses_encoded_command_and_winrt_api(self):
        argv = build_ocr_command(OcrSpec(image_path=SAMPLE_IMAGE))
        self.assertEqual(argv[0], "powershell.exe")
        self.assertIn("-EncodedCommand", argv)
        script = decode_arg(argv)
        self.assertIn("Windows.Media.Ocr.OcrEngine", script)
        self.assertIn("Windows.Graphics.Imaging.BitmapDecoder", script)
        self.assertIn("RecognizeAsync", script)
        self.assertIn(OUTPUT_MARKER, script)

    def test_image_path_is_base64ed_so_quotes_cannot_break_out(self):
        # 带引号/分号的路径：不能以字面量形式进脚本（否则就是注入面）
        tricky = "D:/samples/a'b; c\".png"
        script = build_ocr_script(OcrSpec(image_path=tricky))
        self.assertNotIn(tricky, script)
        self.assertIn(b64(tricky), script)

    def test_language_defaults_to_zh_hans(self):
        script = build_ocr_script(OcrSpec(image_path=SAMPLE_IMAGE))
        self.assertIn(b64("zh-Hans"), script)

    def test_language_can_be_overridden(self):
        script = build_ocr_script(OcrSpec(image_path=SAMPLE_IMAGE, language="en-US"))
        self.assertIn(b64("en-US"), script)
        self.assertNotIn(b64("zh-Hans"), script)

    def test_script_declares_utf8_console(self):
        # 不设 UTF-8 控制台编码，中文回读会变乱码
        self.assertIn("OutputEncoding", build_ocr_script(OcrSpec(image_path=SAMPLE_IMAGE)))


class TestParseOcrOutput(unittest.TestCase):
    def test_normal_output_is_structured(self):
        got = parse_ocr_output(NORMAL_OUTPUT, "zh-Hans")
        self.assertTrue(got["ok"])
        self.assertEqual(got["engine"], "winrt-ocr")
        self.assertEqual(got["language"], "zh-Hans")
        self.assertEqual(got["text"], "发票号码\n12345")
        self.assertEqual(len(got["lines"]), 2)
        self.assertEqual(got["lines"][0]["text"], "发票号码")
        self.assertEqual([w["text"] for w in got["lines"][0]["words"]], ["发票", "号码"])
        self.assertEqual(
            got["lines"][1]["words"][0]["bounds"],
            {"x": 10, "y": 44, "width": 80, "height": 18},
        )
        self.assertIsInstance(got["lines"][0]["words"][0]["bounds"]["x"], int)

    def test_noise_before_marker_is_ignored(self):
        got = parse_ocr_output("WARN 提示\n" + NORMAL_OUTPUT, "zh-Hans")
        self.assertEqual(len(got["lines"]), 2)

    def test_ps_single_element_array_collapse_is_handled(self):
        got = parse_ocr_output(SINGLE_ELEMENT_OUTPUT, "zh-Hans")
        self.assertEqual(len(got["lines"]), 1)
        self.assertEqual(got["lines"][0]["text"], "只有一行")
        self.assertEqual(len(got["lines"][0]["words"]), 1)
        self.assertEqual(got["lines"][0]["words"][0]["bounds"]["width"], 3)

    def test_last_marker_wins(self):
        first = marked({"ok": True, "text": "旧的", "lines": []})
        second = marked({"ok": True, "text": "新的", "lines": []})
        got = parse_ocr_output(first + "\n" + second, "zh-Hans")
        self.assertEqual(got["text"], "新的")

    def test_empty_result_is_success_with_no_lines(self):
        got = parse_ocr_output(EMPTY_OUTPUT, "zh-Hans")
        self.assertTrue(got["ok"])
        self.assertEqual(got["text"], "")
        self.assertEqual(got["lines"], [])

    def test_missing_lines_field_is_tolerated(self):
        got = parse_ocr_output(EMPTY_OUTPUT_NO_LINES, "zh-Hans")
        self.assertEqual(got["lines"], [])

    def test_text_falls_back_to_line_join(self):
        payload = marked(
            {
                "ok": True,
                "lines": [
                    {"text": "第一行", "words": []},
                    {"text": "第二行", "words": []},
                ],
            }
        )
        self.assertEqual(parse_ocr_output(payload, "zh-Hans")["text"], "第一行\n第二行")

    def test_compact_text_matches_source_image_not_engine_spacing(self):
        # T-24：真机样例——引擎插的空格不该泄漏给上层做匹配
        got = parse_ocr_output(REAL_MACHINE_OUTPUT, "zh-Hans")
        self.assertEqual(got["compact_text"], REAL_SOURCE_TEXT)

    def test_compact_text_keeps_line_breaks_even_when_engine_glues_text(self):
        # 真机实测：`Text` 把两行粘成一行，紧凑文本必须按 `lines` 还原行边界，
        # 否则两行的内容会连成一条，做匹配时前后串味。
        got = parse_ocr_output(REAL_MULTILINE_OUTPUT, "zh-Hans")
        self.assertEqual(got["compact_text"], REAL_MULTILINE_SOURCE)
        self.assertEqual(got["compact_text"].count("\n"), 1)
        # 原始 `text` 照旧保持引擎的粘连形态（既有字段语义不变）
        self.assertEqual(got["text"], "WinUse OCR 验 收 12345 订 单 号 12345 金 额 6789")
        self.assertEqual(len(got["lines"]), 2)

    def test_join_words_is_the_backlog_name_for_the_same_value(self):
        got = parse_ocr_output(REAL_MACHINE_OUTPUT, "zh-Hans")
        self.assertEqual(got["join_words"], got["compact_text"])
        self.assertIsInstance(got["join_words"], str)

    def test_compact_text_does_not_change_existing_fields(self):
        # 铁律：既有字段（text / lines / words / bounds）语义原样，一个字节都不动
        got = parse_ocr_output(REAL_MACHINE_OUTPUT, "zh-Hans")
        self.assertEqual(got["text"], REAL_ENGINE_TEXT)
        self.assertEqual(got["lines"][0]["text"], REAL_ENGINE_TEXT)
        self.assertEqual(
            [w["text"] for w in got["lines"][0]["words"]],
            ["WinUse", "OCR", "验", "收", "1", "2345"],
        )
        self.assertEqual(
            got["lines"][0]["words"][-1]["bounds"],
            {"x": 204, "y": 8, "width": 44, "height": 20},
        )

    def test_compact_text_on_empty_result_is_empty_string(self):
        got = parse_ocr_output(EMPTY_OUTPUT, "zh-Hans")
        self.assertEqual(got["compact_text"], "")
        self.assertEqual(got["join_words"], "")

    def test_compact_text_present_when_text_was_missing(self):
        # 整段文本缺失时走按行拼的兜底，紧凑文本同样要有（不能只在这个分支上是 None）
        payload = marked({"ok": True, "lines": [{"text": "发 票 号 码", "words": []}]})
        self.assertEqual(parse_ocr_output(payload, "zh-Hans")["compact_text"], "发票号码")

    def test_missing_bounds_default_to_zero(self):
        payload = marked({"ok": True, "text": "x", "lines": [{"text": "x", "words": [{"text": "x"}]}]})
        bounds = parse_ocr_output(payload, "zh-Hans")["lines"][0]["words"][0]["bounds"]
        self.assertEqual(bounds, {"x": 0, "y": 0, "width": 0, "height": 0})

    def test_engine_unavailable_payload_raises(self):
        with self.assertRaises(OcrEngineUnavailable) as ctx:
            parse_ocr_output(ENGINE_UNAVAILABLE_OUTPUT, "zh-Hans")
        self.assertEqual(ctx.exception.code, "OCR_ENGINE_UNAVAILABLE")
        self.assertIn("en-US", ctx.exception.detail)  # 把已装语言带出来便于排障

    def test_run_failed_payload_is_not_reported_as_success(self):
        # 引擎在跑但这一步失败：如实报 OCR_RUN_FAILED，不冒充"引擎不可用"
        with self.assertRaises(OcrRunFailed) as ctx:
            parse_ocr_output(RUN_FAILED_OUTPUT, "zh-Hans")
        self.assertEqual(ctx.exception.code, "OCR_RUN_FAILED")
        self.assertIn("文件损坏", str(ctx.exception))

    def test_no_marker_raises_engine_unavailable(self):
        with self.assertRaises(OcrEngineUnavailable) as ctx:
            parse_ocr_output(NO_MARKER_OUTPUT, "zh-Hans")
        self.assertEqual(ctx.exception.code, "OCR_ENGINE_UNAVAILABLE")

    def test_marker_but_bad_json_raises_output_invalid(self):
        with self.assertRaises(OcrOutputInvalid) as ctx:
            parse_ocr_output(OUTPUT_MARKER + "{不是 JSON", "zh-Hans")
        self.assertEqual(ctx.exception.code, "OCR_OUTPUT_INVALID")

    def test_error_never_returns_ok_true(self):
        # 铁律：不可用必须报错，不许假装成功
        for raw in (ENGINE_UNAVAILABLE_OUTPUT, RUN_FAILED_OUTPUT, NO_MARKER_OUTPUT):
            with self.assertRaises(Exception):
                parse_ocr_output(raw, "zh-Hans")


class TestCompactOcrText(unittest.TestCase):
    """T-24：紧凑文本的规则边界（纯函数，逐条钉住"删什么、留什么"）。"""

    def test_chinese_chars_are_joined(self):
        self.assertEqual(compact_ocr_text("发 票 号 码"), "发票号码")

    def test_real_machine_sample_matches_source_image(self):
        self.assertEqual(compact_ocr_text(REAL_ENGINE_TEXT), REAL_SOURCE_TEXT)

    def test_number_split_into_segments_is_joined(self):
        for engine, want in (
            ("1 2345", "12345"),
            ("12 345", "12345"),
            ("123 456", "123456"),
            ("12 345 678", "12345678"),
            ("１２ ３４５", "１２３４５"),  # 全角数字同样处理
        ):
            with self.subTest(engine=engine):
                self.assertEqual(compact_ocr_text(engine), want)

    def test_two_four_digit_numbers_are_not_glued(self):
        # '2020 2021' 是两个独立的数（年份/编号），粘起来就毁了数据——宁可不粘
        self.assertEqual(compact_ocr_text("2020 2021"), "2020 2021")
        self.assertEqual(compact_ocr_text("订 单 号 2020 2021"), "订单号 2020 2021")

    def test_latin_word_spacing_is_kept(self):
        self.assertEqual(compact_ocr_text("WinUse OCR"), "WinUse OCR")
        self.assertEqual(compact_ocr_text("WinUse   OCR"), "WinUse OCR")  # 多空格压一个

    def test_chinese_latin_and_digit_boundary_spaces_are_kept(self):
        # 中英/数与单位之间的空格在源文里是真实存在的，删了就是粘错
        self.assertEqual(compact_ocr_text("发 票 WinUse"), "发票 WinUse")
        self.assertEqual(compact_ocr_text("验 收 12345"), "验收 12345")

    def test_fullwidth_punctuation_has_no_adjacent_space(self):
        self.assertEqual(compact_ocr_text("号 码 ： 1 2345"), "号码：12345")
        self.assertEqual(compact_ocr_text("备注 ， 请 核 对 。"), "备注，请核对。")

    def test_line_structure_is_preserved_and_lines_are_stripped(self):
        self.assertEqual(compact_ocr_text(" 发 票 号 码 \n 12345 "), "发票号码\n12345")
        # 不在行之间乱粘：跨行合并会把两行的内容混成一行
        self.assertEqual(compact_ocr_text("发 票\n号 码"), "发票\n号码")

    def test_empty_input_is_empty_string(self):
        for value in ("", None):
            with self.subTest(value=value):
                self.assertEqual(compact_ocr_text(value), "")

    def test_is_idempotent(self):
        for value in (REAL_ENGINE_TEXT, "号 码 ： 1 2345", "WinUse  OCR\n 12345 "):
            with self.subTest(value=value):
                once = compact_ocr_text(value)
                self.assertEqual(compact_ocr_text(once), once)

    def test_no_information_is_dropped_except_spaces(self):
        # 只动空白：非空白字符序列必须与原文完全一致（不替换、不丢弃字符）
        compact = compact_ocr_text(REAL_ENGINE_TEXT)
        self.assertEqual(compact.replace(" ", ""), REAL_ENGINE_TEXT.replace(" ", ""))


class TestRunOcr(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 只为过"文件存在"校验：假 runner 不会真读它
        handle, cls.image = tempfile.mkstemp(suffix=".png")
        os.close(handle)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.image):
            os.remove(cls.image)

    def test_success_path(self):
        runner = FakeRunner([(0, NORMAL_OUTPUT, "")])
        got = run_ocr(OcrSpec(image_path=self.image), runner=runner, which=fake_which(True))
        self.assertTrue(got["ok"])
        self.assertEqual(got["engine"], "winrt-ocr")
        self.assertEqual(got["language"], "zh-Hans")
        self.assertEqual(got["text"], "发票号码\n12345")
        self.assertEqual(len(runner.calls), 1)
        # 下发给 runner 的必须是 argv 列表（不是 shell 字符串）
        self.assertIsInstance(runner.calls[0], list)
        self.assertEqual(runner.calls[0][0], "powershell.exe")

    def test_compact_text_reaches_the_caller_through_run_ocr(self):
        runner = FakeRunner([(0, REAL_MACHINE_OUTPUT, "")])
        got = run_ocr(OcrSpec(image_path=self.image), runner=runner, which=fake_which(True))
        self.assertTrue(got["ok"])
        self.assertEqual(got["compact_text"], REAL_SOURCE_TEXT)
        self.assertEqual(got["join_words"], REAL_SOURCE_TEXT)
        self.assertEqual(got["text"], REAL_ENGINE_TEXT)  # 原始文本照旧

    def test_compact_text_is_not_in_failure_payload(self):
        # 失败结构不掺识别结果字段，免得上层误以为"识别过了"
        runner = FakeRunner([(0, ENGINE_UNAVAILABLE_OUTPUT, "")])
        got = run_ocr(OcrSpec(image_path=self.image), runner=runner, which=fake_which(True))
        self.assertFalse(got["ok"])
        self.assertNotIn("compact_text", got)
        self.assertNotIn("join_words", got)

    def test_language_passed_through_to_script_and_result(self):
        runner = FakeRunner([(0, EMPTY_OUTPUT, "")])
        got = run_ocr(OcrSpec(image_path=self.image, language="en-US"), runner=runner, which=fake_which(True))
        self.assertEqual(got["language"], "en-US")
        self.assertIn(b64("en-US"), decode_arg(runner.calls[0]))

    def test_missing_image_path_raises(self):
        with self.assertRaises(OcrInputMissing) as ctx:
            run_ocr(OcrSpec(), which=fake_which(True))
        self.assertEqual(ctx.exception.code, "OCR_INPUT_MISSING")

    def test_nonexistent_image_raises(self):
        missing = os.path.join(tempfile.gettempdir(), "winuse-no-such-image-2f8a.png")
        with self.assertRaises(OcrImageNotFound) as ctx:
            run_ocr(OcrSpec(image_path=missing), which=fake_which(True))
        self.assertEqual(ctx.exception.code, "OCR_IMAGE_NOT_FOUND")

    def test_no_powershell_returns_engine_unavailable(self):
        runner = FakeRunner([(0, NORMAL_OUTPUT, "")])
        got = run_ocr(OcrSpec(image_path=self.image), runner=runner, which=fake_which(False))
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "OCR_ENGINE_UNAVAILABLE")
        self.assertEqual(runner.calls, [])  # 连试都不试

    def test_nonzero_exit_is_run_failed_regardless_of_stdout(self):
        """R-17：退出码非零就是"没跑成"，不再按 stdout 内容区分。

        原先是"非零且无标记 → ENGINE_UNAVAILABLE"，但那条口径会漏掉更危险的情形：
        非零退出但 stdout 里带成功标记（部分输出）时会被判成识别成功。统一成
        RUN_FAILED 后，判断只看退出码，stdout 只作诊断材料。
        """
        runner = FakeRunner([(1, NO_MARKER_OUTPUT, "WinRT 不可用")])
        got = run_ocr(OcrSpec(image_path=self.image), runner=runner, which=fake_which(True))
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "OCR_RUN_FAILED")
        self.assertIn("退出码", got["message"])
        self.assertNotIn("lines", got)

    def test_powershell_launch_failure_is_normalized(self):
        def boom(_argv):
            raise FileNotFoundError("powershell.exe 不存在")

        got = run_ocr(OcrSpec(image_path=self.image), runner=boom, which=fake_which(True))
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "OCR_ENGINE_UNAVAILABLE")

    def test_engine_unavailable_payload_surfaces_code_not_exception(self):
        # 引擎侧失败走返回值（ok:false + code），不把异常穿透给上层
        runner = FakeRunner([(0, ENGINE_UNAVAILABLE_OUTPUT, "")])
        got = run_ocr(OcrSpec(image_path=self.image), runner=runner, which=fake_which(True))
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "OCR_ENGINE_UNAVAILABLE")
        self.assertEqual(got["engine"], "winrt-ocr")

    def test_timeout_is_normalized(self):
        def slow(_argv):
            raise subprocess.TimeoutExpired(cmd="powershell.exe", timeout=1)

        got = run_ocr(OcrSpec(image_path=self.image), runner=slow, which=fake_which(True))
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "OCR_ENGINE_UNAVAILABLE")
        self.assertIn("超时", got["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
