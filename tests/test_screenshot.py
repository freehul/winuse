"""winuse_screenshot 单测：三种模式的驱动参数、图片提取、落盘与失败分流。

全部用 StubDriver（Python 对象）——**不接触真实驱动与桌面**。
驱动回包形状照抄 cua-driver 0.28.2 的真实采样（2026-09-27 实测，只读调用）：

- `get_window_state`（`include_accessibility_tree:false` 的纯截图路径）：
  `app_name` / `pid` / `window_id` / `window_title` / `window_bounds` /
  `screenshot_width` / `screenshot_height` / `screenshot_mime_type` / `screenshot_png_b64`
  （实测**没有** `screenshot_scale` 字段——所以 scale 必须逐窗口自己算，见 AGENTS.md）
- `get_desktop_state`：`platform` / `display` / `scale_factor`(1.5) / `screen_width` /
  `screen_height` / `screenshot_width` / `screenshot_height` / `screenshot_mime_type` /
  `screenshot_png_b64`（实测 3840×2160 屏：screen 与 screenshot 同尺寸 = 真像素不缩放）
- `zoom`：`format`(jpeg) / `width` / `height` / `mime_type` / `screenshot_mime_type` /
  `screenshot_png_b64`（**内容是 JPEG**；实测请求 100×100 分别回来过 130×130 与 417×417，
  倍数不固定，所以 region 只如实报实测值）
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core import screenshot as shot  # noqa: E402
from winuse.core.screenshot import (  # noqa: E402
    MODES,
    ScreenshotImageInvalid,
    ScreenshotImageTooLarge,
    ScreenshotModeUnknown,
    ScreenshotNoImage,
    ScreenshotOptionInvalid,
    ScreenshotOptionUnknown,
    ScreenshotRegionInvalid,
    ScreenshotRegionTooWide,
    ScreenshotTargetInvalid,
    ScreenshotTargetRequired,
    default_out_dir,
    image_format,
    image_size,
    png_size,
    run_screenshot,
)
from winuse.driver.errors import DriverError, WindowMinimized  # noqa: E402

#: 文件名契约：时间戳 + 模式 + **与内容一致的**扩展名，不含窗口标题/应用名/路径
NAME_RE = re.compile(r"^screenshot-(window|desktop|region)-\d{8}-\d{6}-\d{6}\.(png|jpg)$")


# --------------------------------------------------------------------------
# 造数据：真的 PNG（纯 stdlib）
# --------------------------------------------------------------------------


def make_png(width: int, height: int) -> bytes:
    """造一张**真** PNG（签名 + IHDR + IDAT + IEND，CRC 正确），宽高可断言。"""
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def b64png(width: int, height: int) -> str:
    return base64.b64encode(make_png(width, height)).decode("ascii")


def make_jpeg(width: int, height: int) -> bytes:
    """造 SOI + APP0 + SOF0 + EOI 的 JPEG 头（够验证格式识别与宽高解析）。

    真实驱动实测：`zoom` 回的就是 JPEG（`ffd8ff`），窗口 / 整屏回 PNG。
    完整可解码的 JPEG 需要哈夫曼表，这里只造到 SOF0——解析器要的就是这一段。
    """
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def b64jpeg(width: int, height: int) -> str:
    return base64.b64encode(make_jpeg(width, height)).decode("ascii")


def win(pid=100, wid=200, title="Demo", app="Demo.exe", w=800, h=600, z=3, minimized=False):
    """造一个 list_windows 记录（字段与驱动实测一致）。"""
    return {
        "app_name": app,
        "bounds": {"x": 0, "y": 0, "width": w, "height": h},
        "is_on_screen": True,
        "layer": 0,
        "minimized": minimized,
        "pid": pid,
        "title": title,
        "window_id": wid,
        "z_index": z,
    }


def window_state(png_w=120, png_h=90, pid=100, wid=200, bounds_w=120, bounds_h=90, **extra):
    """造一份 get_window_state 原始返回（纯截图路径：无 UIA 树、无 markdown）。"""
    out = {
        "app_name": "Demo.exe",
        "pid": pid,
        "screenshot_height": png_h,
        "screenshot_mime_type": "image/png",
        "screenshot_png_b64": b64png(png_w, png_h),
        "screenshot_width": png_w,
        "window_bounds": {"x": 0, "y": 0, "width": bounds_w, "height": bounds_h},
        "window_id": wid,
        "window_title": "Demo",
    }
    out.update(extra)
    return out


def desktop_state(png_w=200, png_h=150, screen_w=200, screen_h=150, **extra):
    """造一份 get_desktop_state 原始返回（字段与驱动实测一致）。"""
    out = {
        "display": "primary",
        "platform": "windows",
        "scale_factor": 1.5,
        "screen_height": screen_h,
        "screen_width": screen_w,
        "screenshot_height": png_h,
        "screenshot_mime_type": "image/png",
        "screenshot_png_b64": b64png(png_w, png_h),
        "screenshot_width": png_w,
    }
    out.update(extra)
    return out


def zoom_state(png_w=130, png_h=130, **extra):
    """造一份 zoom 原始返回（字段与驱动实测一致：**JPEG**，只有宽高与图）。"""
    out = {
        "format": "jpeg",
        "height": png_h,
        "mime_type": "image/jpeg",
        "screenshot_mime_type": "image/jpeg",
        "screenshot_png_b64": b64jpeg(png_w, png_h),
        "width": png_w,
    }
    out.update(extra)
    return out


class StubDriver:
    """假驱动：只记录调用参数并按脚本作答（绝不打真桌面）。"""

    def __init__(self, responses: dict | None = None):
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args=None, **_kwargs):
        payload = dict(args or {})
        self.calls.append((tool, payload))
        if tool not in self.responses:
            raise AssertionError(f"未预期的驱动调用：{tool}")
        resp = self.responses[tool]
        if callable(resp):
            return resp(payload)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def args_of(self, tool: str) -> list[dict]:
        return [args for name, args in self.calls if name == tool]

    def single(self, tool: str) -> dict:
        found = self.args_of(tool)
        if len(found) != 1:
            raise AssertionError(f"{tool} 应只被调用一次，实际 {len(found)} 次")
        return found[0]


def window_driver(png_w=120, png_h=90, bounds_w=None, bounds_h=None, **extra):
    """最常见的假驱动：一个窗口 + 一张窗口截图（窗口原生尺寸与出图尺寸默认一致）。"""
    native_w = bounds_w if bounds_w is not None else png_w
    native_h = bounds_h if bounds_h is not None else png_h
    return StubDriver(
        {
            "list_windows": {"windows": [win(w=native_w, h=native_h)]},
            "get_window_state": window_state(
                png_w=png_w, png_h=png_h, bounds_w=native_w, bounds_h=native_h, **extra
            ),
        }
    )


class TempDirCase(unittest.TestCase):
    """每个用例一个临时数据目录（绝不写 ~/.winuse）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "shots"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def files(self, directory: Path | None = None) -> list[str]:
        directory = directory or self.out
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir())


# --------------------------------------------------------------------------
# 纯函数：PNG 宽高解析
# --------------------------------------------------------------------------


class TestPngSize(unittest.TestCase):
    def test_reads_real_png_dimensions(self):
        self.assertEqual(png_size(make_png(37, 11)), (37, 11))

    def test_junk_is_not_a_png(self):
        self.assertIsNone(png_size(b"not a png at all"))
        self.assertIsNone(png_size(b""))
        self.assertIsNone(png_size(b"\x89PNG\r\n\x1a\n"))  # 只有签名、没有 IHDR


class TestImageFormat(unittest.TestCase):
    """格式与宽高都从**文件头**读：实测 zoom 回 JPEG、窗口/整屏回 PNG。"""

    def test_png_is_recognised(self):
        data = make_png(120, 90)
        self.assertEqual(image_format(data), "png")
        self.assertEqual(image_size(data), (120, 90))

    def test_jpeg_is_recognised(self):
        data = make_jpeg(417, 417)
        self.assertEqual(image_format(data), "jpeg")
        self.assertEqual(image_size(data), (417, 417))

    def test_unknown_format_gives_none(self):
        for data in (b"GIF89a" + b"\x00" * 40, b"", b"\xff\xd8", None, 123):
            self.assertIsNone(image_format(data))
            self.assertIsNone(image_size(data))

    def test_jpeg_without_sof_is_not_guessed(self):
        """扫不到 SOF 就说不知道——宁可退回驱动报的尺寸，也不要瞎猜一个数。"""
        self.assertIsNone(image_size(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"))

    def test_truncated_png_does_not_crash(self):
        self.assertIsNone(png_size(make_png(4, 4)[:20]))


# --------------------------------------------------------------------------
# 入口校验：必须先失败、且**不碰驱动**
# --------------------------------------------------------------------------


class TestValidation(TempDirCase):
    def assert_no_driver_call(self, driver: StubDriver):
        self.assertEqual(driver.tools(), [], "参数校验失败时不该下发任何驱动调用")

    def test_unknown_mode_raises(self):
        driver = window_driver()
        with self.assertRaises(ScreenshotModeUnknown) as ctx:
            run_screenshot(driver, mode="fullscreen")
        self.assertEqual(ctx.exception.code, "SCREENSHOT_MODE_UNKNOWN")
        for mode in MODES:
            self.assertIn(mode, ctx.exception.detail)
        self.assert_no_driver_call(driver)

    def test_missing_mode_raises(self):
        with self.assertRaises(ScreenshotModeUnknown):
            run_screenshot(window_driver(), mode=None)

    def test_mode_is_case_and_space_tolerant(self):
        out = run_screenshot(window_driver(), mode=" WINDOW ", out_dir=self.out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "window")

    def test_region_requires_a_window_target(self):
        driver = window_driver()
        with self.assertRaises(ScreenshotTargetRequired) as ctx:
            run_screenshot(driver, mode="region", region={"x1": 0, "y1": 0, "x2": 10, "y2": 10})
        self.assertEqual(ctx.exception.code, "SCREENSHOT_TARGET_REQUIRED")
        self.assert_no_driver_call(driver)

    def test_region_all_none_target_is_still_missing(self):
        """门面常写成 {'window_id': args.get('window_id')}——键在、值是 None。

        只看"有没有键"会把没给目标误判成给了目标，然后静默截到前台窗口上。
        """
        driver = window_driver()
        with self.assertRaises(ScreenshotTargetRequired):
            run_screenshot(
                driver,
                mode="region",
                target={"app": None, "pid": None, "window_id": None},
                region={"x1": 0, "y1": 0, "x2": 10, "y2": 10},
            )
        self.assert_no_driver_call(driver)

    def test_region_missing_rect_raises(self):
        driver = window_driver()
        with self.assertRaises(ScreenshotRegionInvalid) as ctx:
            run_screenshot(driver, mode="region", target={"window_id": 200}, region=None)
        self.assertEqual(ctx.exception.code, "SCREENSHOT_REGION_INVALID")
        self.assert_no_driver_call(driver)

    def test_region_missing_one_edge_raises(self):
        driver = window_driver()
        with self.assertRaises(ScreenshotRegionInvalid):
            run_screenshot(
                driver,
                mode="region",
                target={"window_id": 200},
                region={"x1": 0, "y1": 0, "x2": 10},
            )
        self.assert_no_driver_call(driver)

    def test_region_must_be_named_not_positional(self):
        """四元组按位置传太容易错序：只认具名矩形，且错误里说清怎么写。"""
        with self.assertRaises(ScreenshotRegionInvalid) as ctx:
            run_screenshot(
                window_driver(), mode="region", target={"window_id": 200}, region=[0, 0, 10, 10]
            )
        self.assertIn("x1", ctx.exception.detail)

    def test_region_non_numeric_raises(self):
        for bad in (
            {"x1": "0", "y1": 0, "x2": 10, "y2": 10},
            {"x1": 0, "y1": True, "x2": 10, "y2": 10},
            {"x1": None, "y1": 0, "x2": 10, "y2": 10},
        ):
            with self.assertRaises(ScreenshotRegionInvalid):
                run_screenshot(
                    window_driver(), mode="region", target={"window_id": 200}, region=bad
                )

    def test_region_inverted_raises(self):
        with self.assertRaises(ScreenshotRegionInvalid):
            run_screenshot(
                window_driver(),
                mode="region",
                target={"window_id": 200},
                region={"x1": 10, "y1": 0, "x2": 10, "y2": 10},
            )

    def test_region_wider_than_driver_limit_raises(self):
        """500 px 是驱动硬限制（实测：600 宽被拒，且回的是非 JSON 文本）。

        核心自己先拦，给出可读错误码，而不是让上层收到
        `DRIVER_ERROR: 驱动输出不是合法 JSON` 这种查不出原因的错。
        """
        driver = window_driver()
        with self.assertRaises(ScreenshotRegionTooWide) as ctx:
            run_screenshot(
                driver,
                mode="region",
                target={"window_id": 200},
                region={"x1": 0, "y1": 0, "x2": 501, "y2": 100},
            )
        self.assertEqual(ctx.exception.code, "SCREENSHOT_REGION_TOO_WIDE")
        self.assert_no_driver_call(driver)

    def test_region_exactly_at_limit_is_accepted(self):
        driver = window_driver()
        driver.responses["zoom"] = zoom_state()
        out = run_screenshot(
            driver,
            mode="region",
            target={"window_id": 200},
            region={"x1": 0, "y1": 0, "x2": 500, "y2": 100},
            out_dir=self.out,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(driver.single("zoom")["x2"], 500)

    def test_unknown_option_key_raises(self):
        driver = window_driver()
        with self.assertRaises(ScreenshotOptionUnknown) as ctx:
            run_screenshot(driver, mode="window", out_dir=self.out, options={"thumb": 640})
        self.assertEqual(ctx.exception.code, "SCREENSHOT_OPTION_UNKNOWN")
        self.assertIn("max_dimension", ctx.exception.detail)
        self.assert_no_driver_call(driver)

    def test_options_must_be_a_mapping(self):
        with self.assertRaises(ScreenshotOptionInvalid):
            run_screenshot(window_driver(), mode="window", out_dir=self.out, options=[1, 2])

    def test_bad_option_value_raises(self):
        for bad in ({"max_dimension": 0}, {"max_dimension": -5}, {"max_dimension": "640"}):
            with self.assertRaises(ScreenshotOptionInvalid):
                run_screenshot(window_driver(), mode="window", out_dir=self.out, options=bad)

    def test_bad_allow_minimized_value_raises(self):
        with self.assertRaises(ScreenshotOptionInvalid):
            run_screenshot(
                window_driver(),
                mode="window",
                out_dir=self.out,
                options={"allow_minimized": "yes"},
            )

    def test_target_must_be_a_mapping(self):
        driver = window_driver()
        with self.assertRaises(ScreenshotTargetInvalid) as ctx:
            run_screenshot(driver, mode="window", target=200, out_dir=self.out)
        self.assertEqual(ctx.exception.code, "SCREENSHOT_TARGET_INVALID")
        self.assert_no_driver_call(driver)

    def test_out_dir_must_be_a_path(self):
        with self.assertRaises(ScreenshotOptionInvalid) as ctx:
            run_screenshot(window_driver(), mode="window", out_dir=123)
        self.assertEqual(ctx.exception.code, "SCREENSHOT_OPTION_INVALID")


# --------------------------------------------------------------------------
# window 模式
# --------------------------------------------------------------------------


class TestWindowMode(TempDirCase):
    def test_happy_path_writes_the_driver_bytes(self):
        driver = window_driver(png_w=120, png_h=90)
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)

        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "window")
        self.assertIsNone(out.get("error"))
        path = Path(out["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.out)
        self.assertEqual(path.read_bytes(), make_png(120, 90))
        self.assertEqual((out["width"], out["height"]), (120, 90))
        self.assertEqual(out["bytes"], len(make_png(120, 90)))
        self.assertEqual(out["bytes"], path.stat().st_size)
        self.assertEqual(out["scale"], 1.0)
        self.assertEqual(out["target"]["window_id"], 200)
        self.assertEqual(out["target"]["pid"], 100)
        self.assertEqual(out["source_tool"], "get_window_state")
        self.assertEqual(out["mime"], "image/png")
        self.assertEqual(Path(out["out_dir"]), self.out)

    def test_asks_driver_for_screenshot_without_the_uia_tree(self):
        """截图出口只要像素：带上树会白拉一份 800+ 元素的 UIA 快照。"""
        driver = window_driver()
        run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertEqual(driver.tools()[0], "list_windows")
        args = driver.single("get_window_state")
        self.assertIs(args["include_screenshot"], True)
        self.assertIs(args["include_accessibility_tree"], False)
        self.assertEqual(args["pid"], 100)
        self.assertEqual(args["window_id"], 200)
        self.assertNotIn("max_dimension", args)

    def test_thumbnail_cap_is_forwarded(self):
        driver = window_driver()
        run_screenshot(
            driver,
            mode="window",
            target={"window_id": 200},
            out_dir=self.out,
            options={"max_dimension": 640},
        )
        self.assertEqual(driver.single("get_window_state")["max_dimension"], 640)

    def test_target_by_window_id_beats_z_order(self):
        driver = StubDriver(
            {
                "list_windows": {
                    "windows": [
                        win(pid=1, wid=11, title="Other", z=9),
                        win(pid=100, wid=200, z=1),
                    ]
                },
                "get_window_state": window_state(),
            }
        )
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertEqual(driver.single("get_window_state")["window_id"], 200)
        self.assertEqual(out["target"]["window_id"], 200)

    def test_target_by_app_name_is_case_insensitive(self):
        driver = StubDriver(
            {
                "list_windows": {"windows": [win(pid=7, wid=77, app="Notepad.exe", title="Untitled")]},
                "get_window_state": window_state(pid=7, wid=77),
            }
        )
        out = run_screenshot(driver, mode="window", target={"app": "notepad"}, out_dir=self.out)
        self.assertEqual(out["target"]["pid"], 7)
        self.assertEqual(driver.single("get_window_state")["pid"], 7)

    def test_scale_is_derived_per_window(self):
        """AGENTS.md：scale 逐窗口取（出图宽 ÷ 窗口原生宽），不拿全局估值。"""
        driver = window_driver(png_w=1200, png_h=900, bounds_w=800, bounds_h=600)
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertEqual(out["scale"], 1.5)
        self.assertIn("窗口", out["scale_note"])

    def test_minimized_window_is_rejected_with_the_shared_code(self):
        driver = StubDriver(
            {
                "list_windows": {"windows": [win(minimized=True)]},
                "get_window_state": window_state(),
            }
        )
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], WindowMinimized.code)
        self.assertIsNone(out["path"])
        self.assertEqual(self.files(), [])
        self.assertEqual(driver.tools(), ["list_windows"], "窗口不可用时不该继续去截图")

    def test_allow_minimized_option_reaches_pick_window(self):
        driver = StubDriver(
            {
                "list_windows": {"windows": [win(minimized=True)]},
                "get_window_state": window_state(),
            }
        )
        out = run_screenshot(
            driver,
            mode="window",
            target={"window_id": 200},
            out_dir=self.out,
            options={"allow_minimized": True},
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(driver.single("get_window_state")["window_id"], 200)

    def test_no_window_found_returns_code(self):
        driver = StubDriver({"list_windows": {"windows": [win(pid=1, wid=11)]}})
        out = run_screenshot(driver, mode="window", target={"window_id": 999}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "WINDOW_NOT_FOUND")
        self.assertIn("error", out)

    def test_driver_failure_is_returned_not_raised(self):
        driver = window_driver()
        driver.responses["get_window_state"] = DriverError("驱动调用失败：get_window_state")
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "DRIVER_ERROR")
        self.assertEqual(out["error"]["code"], "DRIVER_ERROR")
        self.assertEqual(self.files(), [])

    def test_missing_image_returns_a_code_and_writes_nothing(self):
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(
            screenshot_png_b64=None,
            has_screenshot=False,
            screenshot_error="窗口已最小化，无法抓取像素",
        )
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], ScreenshotNoImage.code)
        self.assertIsNone(out["path"])
        self.assertEqual(self.files(), [])
        self.assertFalse(self.out.exists(), "没图就不该碰磁盘：连目录都不建")
        self.assertIn("has_screenshot", out["error"]["detail"])

    def test_driver_side_file_path_is_copied(self):
        """驱动可以直写文件（`screenshot_out_file`）：那时回包里没有 base64。"""
        source = self.tmp / "driver-shot.png"
        source.write_bytes(make_png(64, 48))
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(
            screenshot_png_b64=None,
            screenshot_file_path=str(source),
            screenshot_width=64,
            screenshot_height=48,
        )
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["width"], out["height"]), (64, 48))
        self.assertEqual(Path(out["path"]).read_bytes(), make_png(64, 48))
        self.assertEqual(out["image_source"], "screenshot_file_path")

    def test_driver_side_file_path_missing_returns_code(self):
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(
            screenshot_png_b64=None, screenshot_file_path=str(self.tmp / "gone.png")
        )
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], ScreenshotNoImage.code)

    def test_broken_base64_returns_code_and_leaves_no_partial_file(self):
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(screenshot_png_b64="!!!!not base64!!!!")
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], ScreenshotImageInvalid.code)
        self.assertEqual(self.files(), [])

    def test_empty_payload_returns_code(self):
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(screenshot_png_b64="   ")
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], ScreenshotNoImage.code)

    def test_oversized_image_is_refused(self):
        driver = window_driver()
        with mock.patch.object(shot, "MAX_IMAGE_BYTES", 16):
            out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], ScreenshotImageTooLarge.code)
        self.assertEqual(self.files(), [])

    def test_dimensions_come_from_the_file_not_the_report(self):
        """图片真实宽高以**落盘文件**为准；驱动报的对不上就如实标出来。"""
        driver = window_driver(png_w=120, png_h=90)
        driver.responses["get_window_state"]["screenshot_width"] = 999
        driver.responses["get_window_state"]["screenshot_height"] = 999
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertEqual((out["width"], out["height"]), (120, 90))
        self.assertTrue(out["size_mismatch"])
        self.assertEqual(out["driver_reported_size"], {"width": 999, "height": 999})
        self.assertIn("999", out["size_note"])

    def test_no_size_note_when_dimensions_agree(self):
        out = run_screenshot(window_driver(), mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertNotIn("size_mismatch", out)
        self.assertNotIn("size_note", out)

    def test_file_header_wins_over_a_lying_driver_mime(self):
        """驱动说 jpeg、文件头是 png → 以文件为准（扩展名不撒谎）。"""
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(
            screenshot_mime_type="image/jpeg", screenshot_png_b64=b64png(120, 90)
        )
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertTrue(Path(out["path"]).name.endswith(".png"))
        self.assertEqual(out["mime"], "image/png")

    def test_unknown_format_falls_back_to_the_driver_mime_and_size(self):
        """认不出的格式：退回驱动报的 mime 与宽高，别把这张图丢掉。"""
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(
            screenshot_png_b64=base64.b64encode(b"GIF89a" + b"\x00" * 40).decode("ascii"),
            screenshot_width=64,
            screenshot_height=48,
        )
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["width"], out["height"]), (64, 48))
        self.assertTrue(Path(out["path"]).name.endswith(".png"))

    def test_unknown_format_without_any_size_is_refused(self):
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(
            screenshot_png_b64=base64.b64encode(b"GIF89a" + b"\x00" * 40).decode("ascii"),
            screenshot_width=None,
            screenshot_height=None,
        )
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], ScreenshotImageInvalid.code)
        self.assertEqual(self.files(), [])

    def test_image_bytes_never_enter_the_result(self):
        driver = window_driver()
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        dumped = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("base64", dumped, "回包里不该出现任何 base64 内容")
        self.assertNotIn(b64png(120, 90)[:40], dumped)
        for key, value in out.items():
            if isinstance(value, str):
                self.assertLess(len(value), 400, f"{key} 太长，像是在往回包里塞图或整段数据")
        self.assertLess(len(dumped), 1500, "回包必须是文本优先的小结构，不是图")

    def test_file_name_is_timestamped_and_free_of_window_identity(self):
        driver = window_driver()
        driver.responses["get_window_state"]["window_title"] = "报销单-张三-2026"
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        name = Path(out["path"]).name
        self.assertRegex(name, NAME_RE)
        self.assertNotIn("Demo", name)
        self.assertNotIn("报销单", name)
        self.assertNotIn("张三", name)

    def test_two_shots_never_overwrite_each_other(self):
        driver = window_driver()
        with mock.patch.object(shot, "_now_stamp", lambda: "20260927-120000-000001"):
            first = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
            second = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(self.files()), 2)
        for path in (first["path"], second["path"]):
            self.assertTrue(Path(path).is_file())

    def test_no_part_files_left_behind(self):
        run_screenshot(window_driver(), mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertEqual([n for n in self.files() if n.endswith(".part")], [])


# --------------------------------------------------------------------------
# desktop 模式
# --------------------------------------------------------------------------


class TestDesktopMode(TempDirCase):
    def test_happy_path_uses_true_screen_pixels(self):
        driver = StubDriver({"get_desktop_state": desktop_state(png_w=200, png_h=150)})
        out = run_screenshot(driver, mode="desktop", out_dir=self.out)

        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "desktop")
        self.assertEqual(driver.tools(), ["get_desktop_state"], "全屏截图不该去枚举窗口")
        self.assertEqual(driver.single("get_desktop_state"), {})
        self.assertEqual((out["width"], out["height"]), (200, 150))
        self.assertEqual(out["scale"], 1.0)
        self.assertEqual(out["source_tool"], "get_desktop_state")
        self.assertIsNone(out["target"])
        self.assertEqual(out["screen_size"], {"width": 200, "height": 150})
        self.assertEqual(out["screen_scale_factor"], 1.5)
        self.assertTrue(Path(out["path"]).is_file())

    def test_scale_reflects_downscaled_capture(self):
        """驱动若把整屏缩过，scale 必须如实给出（否则坐标换算全错）。"""
        driver = StubDriver({"get_desktop_state": desktop_state(png_w=1920, screen_w=3840)})
        out = run_screenshot(driver, mode="desktop", out_dir=self.out)
        self.assertEqual(out["scale"], 0.5)

    def test_target_is_ignored_but_declared(self):
        driver = StubDriver({"get_desktop_state": desktop_state()})
        out = run_screenshot(driver, mode="desktop", target={"window_id": 200}, out_dir=self.out)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["target"])
        self.assertIn("忽略", out["target_note"])
        self.assertNotIn("list_windows", driver.tools())

    def test_missing_image_returns_code(self):
        driver = StubDriver({"get_desktop_state": desktop_state(screenshot_png_b64=None)})
        out = run_screenshot(driver, mode="desktop", out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], ScreenshotNoImage.code)
        self.assertEqual(self.files(), [])


# --------------------------------------------------------------------------
# region 模式
# --------------------------------------------------------------------------


class TestRegionMode(TempDirCase):
    def test_happy_path_forwards_the_rect(self):
        driver = window_driver()
        driver.responses["zoom"] = zoom_state(png_w=130, png_h=130)
        out = run_screenshot(
            driver,
            mode="region",
            target={"window_id": 200},
            region={"x1": 10, "y1": 20, "x2": 110, "y2": 120},
            out_dir=self.out,
        )

        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "region")
        args = driver.single("zoom")
        self.assertEqual(args["pid"], 100)
        self.assertEqual(args["window_id"], 200)
        self.assertEqual((args["x1"], args["y1"], args["x2"], args["y2"]), (10, 20, 110, 120))
        self.assertEqual((out["width"], out["height"]), (130, 130))
        self.assertIsNone(out["scale"])
        self.assertEqual(out["source_tool"], "zoom")
        self.assertEqual(out["region"]["requested"], {"x1": 10, "y1": 20, "x2": 110, "y2": 120})
        self.assertEqual(out["region"]["padded_ratio"], 1.3)
        self.assertEqual(out["region_note"], shot.REGION_NOTE)
        self.assertTrue(Path(out["path"]).is_file())

    def test_region_image_is_jpeg_and_the_extension_says_so(self):
        """实测：zoom 回的是 JPEG。写成 .png 就是名不副实（预览/OCR 会走错分支）。"""
        driver = window_driver()
        driver.responses["zoom"] = zoom_state(png_w=417, png_h=417)
        out = run_screenshot(
            driver,
            mode="region",
            target={"window_id": 200},
            region={"x1": 0, "y1": 0, "x2": 100, "y2": 100},
            out_dir=self.out,
        )
        path = Path(out["path"])
        self.assertTrue(out["ok"], out)
        self.assertTrue(path.name.endswith(".jpg"), path.name)
        self.assertEqual(out["mime"], "image/jpeg")
        self.assertEqual(path.read_bytes(), make_jpeg(417, 417))
        self.assertEqual(image_format(path.read_bytes()), "jpeg")
        self.assertEqual((out["width"], out["height"]), (417, 417))
        self.assertIn("JPEG", shot.REGION_NOTE)

    def test_float_rect_is_accepted_and_rounded(self):
        driver = window_driver()
        driver.responses["zoom"] = zoom_state()
        out = run_screenshot(
            driver,
            mode="region",
            target={"window_id": 200},
            region={"x1": 1.4, "y1": 2.6, "x2": 100.2, "y2": 100.9},
            out_dir=self.out,
        )
        self.assertTrue(out["ok"], out)
        args = driver.single("zoom")
        self.assertEqual((args["x1"], args["y1"], args["x2"], args["y2"]), (1, 3, 100, 101))

    def test_missing_image_returns_code(self):
        driver = window_driver()
        driver.responses["zoom"] = zoom_state(screenshot_png_b64=None)
        out = run_screenshot(
            driver,
            mode="region",
            target={"window_id": 200},
            region={"x1": 0, "y1": 0, "x2": 50, "y2": 50},
            out_dir=self.out,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], ScreenshotNoImage.code)


# --------------------------------------------------------------------------
# 落盘目录解析（跟 config.data_dir 同源）
# --------------------------------------------------------------------------


class TestOutDir(TempDirCase):
    def test_default_out_dir_is_the_configured_data_dir(self):
        data_dir = self.tmp / "winuse-data"
        env = self.tmp / ".env"
        env.write_text(f"WINUSE_DATA_DIR={data_dir.as_posix()}\n", encoding="utf-8")
        self.assertEqual(default_out_dir(env), data_dir / "screenshots")

    def test_default_out_dir_falls_back_to_the_home_dir(self):
        path = default_out_dir(None)
        self.assertEqual(path, Path.home() / ".winuse" / "screenshots")
        self.assertNotIn("~", str(path))

    def test_run_writes_into_the_configured_data_dir(self):
        data_dir = self.tmp / "winuse-data"
        env = self.tmp / ".env"
        env.write_text(f"WINUSE_DATA_DIR={data_dir.as_posix()}\n", encoding="utf-8")
        out = run_screenshot(
            window_driver(), mode="window", target={"window_id": 200}, options={"env_path": env}
        )
        self.assertTrue(out["ok"], out)
        path = Path(out["path"])
        self.assertEqual(path.parent, data_dir / "screenshots")
        self.assertTrue(path.is_file())

    def test_explicit_out_dir_wins_and_is_not_appended(self):
        explicit = self.tmp / "custom"
        out = run_screenshot(
            window_driver(), mode="window", target={"window_id": 200}, out_dir=explicit
        )
        self.assertEqual(Path(out["path"]).parent, explicit)
        self.assertNotIn("screenshots", str(explicit))

    def test_write_failure_returns_a_code_instead_of_swallowing(self):
        """目录建不出来（同名文件占位）→ 明确错误码，不抛异常、不留半截文件。"""
        blocked = self.tmp / "blocked"
        blocked.write_text("i am a file, not a dir", encoding="utf-8")
        out = run_screenshot(
            window_driver(),
            mode="window",
            target={"window_id": 200},
            out_dir=blocked / "shots" / "deep",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "SCREENSHOT_WRITE_FAILED")
        self.assertIsNone(out["path"])
        self.assertIsNone(out["bytes"])
        self.assertIn("error", out)
        self.assertEqual(out["error"]["code"], "SCREENSHOT_WRITE_FAILED")
        self.assertTrue(blocked.is_file(), "不该把占位文件删掉或改名")

    def test_write_failure_on_replace_is_reported(self):
        with mock.patch.object(shot.os, "replace", side_effect=OSError("磁盘已满")):
            out = run_screenshot(
                window_driver(), mode="window", target={"window_id": 200}, out_dir=self.out
            )
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "SCREENSHOT_WRITE_FAILED")
        self.assertEqual(self.files(), [], "失败后不该留下临时文件")


# --------------------------------------------------------------------------
# 对外结构契约
# --------------------------------------------------------------------------


class TestResultContract(TempDirCase):
    def test_success_shape(self):
        out = run_screenshot(window_driver(), mode="window", target={"window_id": 200}, out_dir=self.out)
        for key in ("ok", "mode", "path", "width", "height", "bytes", "scale", "target", "out_dir"):
            self.assertIn(key, out)
        self.assertNotIn("code", out)
        self.assertNotIn("error", out)

    def test_failure_shape(self):
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(screenshot_png_b64=None)
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        for key in ("ok", "mode", "path", "width", "height", "bytes", "code", "error", "out_dir"):
            self.assertIn(key, out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["mode"], "window")
        self.assertEqual(out["error"]["code"], out["code"])

    def test_failure_shape_keeps_the_target_when_known(self):
        driver = window_driver()
        driver.responses["get_window_state"] = window_state(screenshot_png_b64=None)
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertEqual(out["target"]["window_id"], 200)

    def test_failure_shape_keeps_the_target_when_the_driver_fails(self):
        """驱动在截图那一步失败时，目标窗口已经解析出来了——别把线索丢掉。"""
        driver = window_driver()
        driver.responses["get_window_state"] = DriverError("驱动调用失败：get_window_state（退出码 1）")
        out = run_screenshot(driver, mode="window", target={"window_id": 200}, out_dir=self.out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "DRIVER_ERROR")
        self.assertEqual(out["target"]["window_id"], 200)
        self.assertEqual(self.files(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
