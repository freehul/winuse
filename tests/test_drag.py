"""winuse_drag 单测：目标校验、坐标换算、快照纪律、干跑、安全闸。

不接触真实驱动与桌面：坐标换算是**纯函数**（可穷举断言），run_drag 走 StubDriver。

坐标口径（实测依据，2026-09-27 真机采样）：
- 元素 `frame` 与 `window_bounds` **同处原生桌面坐标空间**（实测某窗口
  `bounds=(2027,21,1813,2058)`、其 Document 元素 `frame=(2027,21,…)`，原点一致）。
- `screenshot_width/height` 才是"window-local screenshot 像素"的口径，且**逐窗口**
  不同（实测 0.9989 / 0.5785 两种——后者是驱动对大窗口截图做了降采样）。
- 所以换算必须用**逐窗口** scale = `screenshot 尺寸 ÷ window_bounds 尺寸`。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.drag import (  # noqa: E402
    build_drag_call,
    frame_center_in_screenshot,
    run_drag,
)
from winuse.driver.errors import WinUseError  # noqa: E402

TARGET = {"pid": 100, "window_id": 200, "app_name": "Demo.exe", "title": "Demo"}
SYSTEM_TARGET = {"pid": 1, "window_id": 2, "app_name": "explorer.exe", "title": "任务栏"}

BOUNDS = {"x": 1000, "y": 500, "width": 800, "height": 600}
SHOT_W, SHOT_H = 800, 600  # scale == 1.0
ELS = [
    {
        "element_index": 0,
        "element_token": "sFresh:0",
        "role": "Button",
        "label": "A",
        "enabled": True,
        "actions": ["invoke"],
        "frame": {"x": 1050, "y": 550, "w": 40, "h": 20},  # 中心 (1070,560) → 局部 (70,60)
    },
    {
        "element_index": 1,
        "element_token": "sFresh:1",
        "role": "ListItem",
        "label": "B",
        "enabled": True,
        "actions": ["select"],
        "frame": {"x": 1250, "y": 750, "w": 100, "h": 60},  # 中心 (1300,780) → 局部 (300,280)
    },
]


def window_state(**overrides) -> dict:
    state = {
        "elements": [dict(e) for e in ELS],
        "window_bounds": dict(BOUNDS),
        "screenshot_width": SHOT_W,
        "screenshot_height": SHOT_H,
        "snapshot_id": "sFresh",
        "element_count": len(ELS),
    }
    state.update(overrides)
    return state


class StubDriver:
    """按工具名回固定结果；记录每次调用（与 test_act.py 同形）。"""

    def __init__(self, responses=None, boom=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.boom = boom

    def call(self, tool, args=None, **kwargs):
        self.calls.append((tool, dict(args or {})))
        if self.boom:
            raise self.boom
        if tool in self.responses:
            value = self.responses[tool]
            return value() if callable(value) else value
        return {"ok": True}

    def tools_called(self) -> list[str]:
        return [tool for tool, _ in self.calls]


class TestFrameCenterConversion(unittest.TestCase):
    """纯函数：原生桌面坐标的元素 frame → window-local screenshot 像素。"""

    def test_scale_one_subtracts_window_origin(self):
        x, y = frame_center_in_screenshot(
            {"x": 110, "y": 20, "w": 20, "h": 10},
            {"x": 100, "y": 10, "width": 800, "height": 600},
            800,
            600,
        )
        # 中心 (120,25)；减去窗口原点 (100,10) → (20,15)；scale=1.0
        self.assertAlmostEqual(x, 20.0)
        self.assertAlmostEqual(y, 15.0)

    def test_scale_is_per_window_screenshot_over_bounds(self):
        """降采样/高 DPI 时 screenshot 尺寸 ≠ 窗口尺寸，必须按比例换回截图口径。"""
        x, y = frame_center_in_screenshot(
            {"x": 100, "y": 200, "w": 40, "h": 40},
            {"x": 0, "y": 0, "width": 1000, "height": 1000},
            500,
            500,  # scale 0.5
        )
        self.assertAlmostEqual(x, 60.0)  # (120) * 0.5
        self.assertAlmostEqual(y, 110.0)  # (220) * 0.5

    def test_measured_real_world_window(self):
        """真机采样那一组（Hermes 窗口 + 最小化按钮）——回归护栏。"""
        x, y = frame_center_in_screenshot(
            {"x": 3633, "y": 23, "w": 68, "h": 50},
            {"x": 2027, "y": 21, "width": 1813, "height": 2058},
            1811,
            2056,
        )
        self.assertAlmostEqual(x, 1640 * 1811 / 1813, places=3)
        self.assertAlmostEqual(y, 27 * 2056 / 2058, places=3)
        self.assertAlmostEqual(x, 1638.19, places=2)  # scale ≈ 0.9989 ≠ 1.0

    def test_missing_frame_is_refused_not_guessed(self):
        with self.assertRaises(WinUseError) as ctx:
            frame_center_in_screenshot(None, BOUNDS, SHOT_W, SHOT_H)
        self.assertEqual(ctx.exception.code, "DRAG_COORD_SPACE_UNKNOWN")

    def test_zero_or_missing_geometry_is_refused(self):
        for frame, bounds, sw, sh in (
            ({"x": 1, "y": 1, "w": 1, "h": 1}, {"x": 0, "y": 0, "width": 0, "height": 600}, 800, 600),
            ({"x": 1, "y": 1, "w": 1, "h": 1}, BOUNDS, 0, 0),
            ({"x": 1, "y": 1, "w": 1, "h": 1}, None, SHOT_W, SHOT_H),
        ):
            with self.assertRaises(WinUseError) as ctx:
                frame_center_in_screenshot(frame, bounds, sw, sh)
            self.assertEqual(ctx.exception.code, "DRAG_COORD_SPACE_UNKNOWN")


class TestBuildDragCall(unittest.TestCase):
    def test_point_args_shape(self):
        args = build_drag_call(TARGET, (10.4, 20.6), (300.2, 400.9))
        self.assertEqual(args["pid"], 100)
        self.assertEqual(args["window_id"], 200)
        self.assertEqual(args["from_x"], 10)  # 截图像素：取整
        self.assertEqual(args["from_y"], 21)
        self.assertEqual(args["to_x"], 300)
        self.assertEqual(args["to_y"], 401)
        # 不抢焦点：与 core.act 同口径，显式后台投递（驱动默认即 background）
        self.assertEqual(args["delivery_mode"], "background")
        # 驱动 schema 是 additionalProperties:false，绝不带元素/快照字段
        for bad in ("element_index", "element_token", "snapshot_id", "from_element", "to_element"):
            self.assertNotIn(bad, args)

    def test_spec_options_are_passed_through(self):
        args = build_drag_call(
            TARGET,
            (1, 2),
            (3, 4),
            {"duration_ms": 800, "steps": 30, "button": "right", "modifier": ["shift"]},
        )
        self.assertEqual(args["duration_ms"], 800)
        self.assertEqual(args["steps"], 30)
        self.assertEqual(args["button"], "right")
        self.assertEqual(args["modifier"], ["shift"])

    def test_spec_options_can_override_delivery_mode(self):
        args = build_drag_call(TARGET, (1, 2), (3, 4), {"delivery_mode": "foreground"})
        self.assertEqual(args["delivery_mode"], "foreground")

    def test_unknown_spec_option_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            build_drag_call(TARGET, (1, 2), (3, 4), {"frobnicate": 1})
        self.assertEqual(ctx.exception.code, "DRAG_OPTION_UNKNOWN")


class TestTargetValidation(unittest.TestCase):
    """要么两个元素、要么两个坐标——混合/缺失一律明确报错，绝不猜。"""

    def test_mixed_element_and_point_is_refused(self):
        driver = StubDriver()
        with self.assertRaises(WinUseError) as ctx:
            run_drag(driver, target=TARGET, from_element=0, to_point=(1, 2))
        self.assertEqual(ctx.exception.code, "DRAG_TARGET_MIXED")
        self.assertEqual(driver.calls, [])

    def test_half_pair_is_refused(self):
        for kwargs in (
            {"from_element": 0},
            {"to_element": 1},
            {"from_point": (1, 2)},
            {"to_point": (3, 4)},
        ):
            with self.assertRaises(WinUseError) as ctx:
                run_drag(StubDriver(), target=TARGET, **kwargs)
            self.assertEqual(ctx.exception.code, "DRAG_TARGET_INCOMPLETE", kwargs)

    def test_nothing_given_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            run_drag(StubDriver(), target=TARGET)
        self.assertEqual(ctx.exception.code, "DRAG_TARGET_INCOMPLETE")

    def test_target_without_window_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            run_drag(StubDriver(), target={"pid": 100}, from_point=(1, 2), to_point=(3, 4))
        self.assertEqual(ctx.exception.code, "MISSING_TARGET")


class TestPointMode(unittest.TestCase):
    def test_dry_run_sends_nothing(self):
        driver = StubDriver()
        out = run_drag(driver, target=TARGET, from_point=(10, 20), to_point=(300, 400))
        self.assertEqual(driver.calls, [])  # 坐标模式连读都不需要
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["coordinate_mode"], "point")
        self.assertEqual(out["would_call"]["tool"], "drag")
        self.assertEqual(out["would_call"]["args"]["from_x"], 10)
        self.assertNotIn("args", out)

    def test_execute_sends_drag_once(self):
        driver = StubDriver({"drag": {"ok": True, "moved": True}})
        out = run_drag(
            driver, target=TARGET, from_point=(10, 20), to_point=(300, 400), execute=True
        )
        self.assertEqual(driver.tools_called(), ["drag"])
        self.assertFalse(out["dry_run"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], {"ok": True, "moved": True})
        self.assertNotIn("would_call", out)

    def test_point_accepts_dict_form(self):
        out = run_drag(
            StubDriver(), target=TARGET, from_point={"x": 5, "y": 6}, to_point={"x": 7, "y": 8}
        )
        args = out["would_call"]["args"]
        self.assertEqual((args["from_x"], args["from_y"], args["to_x"], args["to_y"]), (5, 6, 7, 8))

    def test_bad_point_shape_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            run_drag(StubDriver(), target=TARGET, from_point="nope", to_point=(3, 4))
        self.assertEqual(ctx.exception.code, "DRAG_TARGET_INVALID")


class TestElementMode(unittest.TestCase):
    def test_dry_run_reads_state_but_sends_no_drag(self):
        driver = StubDriver({"get_window_state": window_state()})
        out = run_drag(
            driver, target=TARGET, from_element=0, to_element=1, snapshot_id="sFresh"
        )
        # 只读一次状态拿到坐标空间；**不动桌面**
        self.assertEqual(driver.tools_called(), ["get_window_state"])
        self.assertTrue(out["dry_run"])
        args = out["would_call"]["args"]
        self.assertEqual((args["from_x"], args["from_y"]), (70, 60))
        self.assertEqual((args["to_x"], args["to_y"]), (300, 280))
        self.assertEqual(out["coordinate_mode"], "element")
        self.assertEqual(out["coordinate_space"]["scale_x"], 1.0)

    def test_execute_reads_then_drags(self):
        driver = StubDriver({"get_window_state": window_state(), "drag": {"ok": True}})
        out = run_drag(
            driver,
            target=TARGET,
            from_element=0,
            to_element=1,
            snapshot_id="sFresh",
            execute=True,
        )
        self.assertEqual(driver.tools_called(), ["get_window_state", "drag"])
        self.assertTrue(out["ok"])
        _, drag_args = driver.calls[1]
        self.assertEqual((drag_args["from_x"], drag_args["from_y"]), (70, 60))
        self.assertEqual((drag_args["to_x"], drag_args["to_y"]), (300, 280))

    def test_token_reference_needs_no_snapshot(self):
        driver = StubDriver({"get_window_state": window_state()})
        out = run_drag(driver, target=TARGET, from_element="sFresh:0", to_element="sFresh:1")
        self.assertTrue(out["ok"])
        args = out["would_call"]["args"]
        self.assertEqual((args["from_x"], args["from_y"]), (70, 60))

    def test_stale_token_prefix_still_resolves_by_embedded_index(self):
        """实测踩到的坑：令牌前缀带快照标识，而 run_drag 为拿坐标空间必然新读一次。

        新读会刷新快照 → 调用方手上的令牌前缀立刻失效（act.py 记录过的
        `stale_element_token`）。若只按"完全一致"匹配，**所有**用令牌的调用都会
        被判成"元素找不到"。序号在换快照后不变，按它兜底（与 act.py 同款）。
        """
        driver = StubDriver({"get_window_state": window_state()})  # 新树的令牌是 sFresh:*
        out = run_drag(
            driver, target=TARGET, from_element="s00000072:0", to_element="s00000072:1"
        )
        self.assertTrue(out["ok"])
        args = out["would_call"]["args"]
        self.assertEqual((args["from_x"], args["from_y"]), (70, 60))
        self.assertEqual((args["to_x"], args["to_y"]), (300, 280))

    def test_unresolvable_token_is_still_reported(self):
        driver = StubDriver({"get_window_state": window_state()})
        with self.assertRaises(WinUseError) as ctx:
            run_drag(driver, target=TARGET, from_element="not-a-token", to_element="sFresh:1")
        self.assertEqual(ctx.exception.code, "DRAG_ELEMENT_NOT_FOUND")

    def test_index_reference_requires_snapshot(self):
        driver = StubDriver({"get_window_state": window_state()})
        with self.assertRaises(WinUseError) as ctx:
            run_drag(driver, target=TARGET, from_element=0, to_element=1)
        self.assertEqual(ctx.exception.code, "SNAPSHOT_REQUIRED")
        self.assertEqual(driver.calls, [])

    def test_element_not_found_is_reported(self):
        driver = StubDriver({"get_window_state": window_state()})
        with self.assertRaises(WinUseError) as ctx:
            run_drag(driver, target=TARGET, from_element=99, to_element=1, snapshot_id="sFresh")
        self.assertEqual(ctx.exception.code, "DRAG_ELEMENT_NOT_FOUND")

    def test_element_without_frame_is_refused(self):
        broken = window_state(elements=[{"element_index": 0, "element_token": "sFresh:0"}, ELS[1]])
        driver = StubDriver({"get_window_state": broken})
        with self.assertRaises(WinUseError) as ctx:
            run_drag(driver, target=TARGET, from_element=0, to_element=1, snapshot_id="sFresh")
        self.assertEqual(ctx.exception.code, "DRAG_COORD_SPACE_UNKNOWN")

    def test_missing_screenshot_dims_is_refused(self):
        """没有 screenshot 尺寸就无从换算到截图口径——fail-closed，不拿 bounds 顶替。"""
        driver = StubDriver({"get_window_state": window_state(screenshot_width=None, screenshot_height=None)})
        with self.assertRaises(WinUseError) as ctx:
            run_drag(driver, target=TARGET, from_element=0, to_element=1, snapshot_id="sFresh")
        self.assertEqual(ctx.exception.code, "DRAG_COORD_SPACE_UNKNOWN")

    def test_downsized_screenshot_scale_applies(self):
        state = window_state(
            window_bounds={"x": 0, "y": 0, "width": 1000, "height": 1000},
            screenshot_width=500,
            screenshot_height=500,
            elements=[
                {"element_index": 0, "element_token": "s:0", "frame": {"x": 100, "y": 200, "w": 40, "h": 40}},
                {"element_index": 1, "element_token": "s:1", "frame": {"x": 400, "y": 600, "w": 40, "h": 40}},
            ],
        )
        driver = StubDriver({"get_window_state": state})
        out = run_drag(driver, target=TARGET, from_element=0, to_element=1, snapshot_id="s")
        args = out["would_call"]["args"]
        self.assertEqual((args["from_x"], args["from_y"]), (60, 110))
        self.assertEqual((args["to_x"], args["to_y"]), (210, 310))


class TestSafetyGate(unittest.TestCase):
    def test_system_window_requires_confirm(self):
        driver = StubDriver()
        out = run_drag(
            driver, target=SYSTEM_TARGET, from_point=(1, 2), to_point=(3, 4), execute=True
        )
        self.assertFalse(out["ok"])
        self.assertTrue(out["requires_confirm"])
        self.assertEqual(out["error"]["code"], "CONFIRM_REQUIRED")
        self.assertEqual(driver.calls, [])  # 未确认：一个动作都不下发

    def test_confirm_lets_system_window_through(self):
        driver = StubDriver({"drag": {"ok": True}})
        out = run_drag(
            driver,
            target=SYSTEM_TARGET,
            from_point=(1, 2),
            to_point=(3, 4),
            execute=True,
            confirm=True,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(driver.tools_called(), ["drag"])
        self.assertTrue(out["danger"]["dangerous"])


class TestDriverResultSemantics(unittest.TestCase):
    def test_driver_failure_on_state_read_is_reported_not_raised(self):
        """读状态时的**驱动侧故障**回收成结构（区别于本模块的守门错误——那些直接抛）。"""
        from winuse.driver.errors import DriverError

        driver = StubDriver(boom=DriverError("驱动调用失败：get_window_state（退出码 2）", "boom"))
        out = run_drag(
            driver, target=TARGET, from_element=0, to_element=1, snapshot_id="sFresh"
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "DRIVER_ERROR")

    def test_driver_refusal_is_not_ok(self):
        """驱动不抛异常但回 ok=false / background_unavailable —— 仍必须体现在 ok 上。"""
        driver = StubDriver(
            {
                "drag": {
                    "ok": False,
                    "code": "background_unavailable",
                    "message": "drag started on a caption bar; re-issue with delivery_mode:foreground",
                }
            }
        )
        out = run_drag(
            driver, target=TARGET, from_point=(1, 2), to_point=(3, 4), execute=True
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "BACKGROUND_UNAVAILABLE")
        # 拒绝要带可执行的下一步：标题栏起手的拖拽只能升到 foreground
        self.assertIn("foreground", out["error"]["detail"])

    def test_empty_result_is_not_ok(self):
        driver = StubDriver({"drag": {}})
        out = run_drag(driver, target=TARGET, from_point=(1, 2), to_point=(3, 4), execute=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "DRIVER_EMPTY_RESPONSE")


class TestAudit(unittest.TestCase):
    def test_dry_run_and_execute_are_audited(self):
        events: list[dict] = []
        run_drag(
            StubDriver(),
            target=TARGET,
            from_point=(1, 2),
            to_point=(3, 4),
            audit=events.append,
        )
        self.assertEqual(events[-1]["event"], "drag.dry_run")
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(events[-1]["detail"]["tool"], "drag")

        events.clear()
        run_drag(
            StubDriver({"drag": {"ok": True}}),
            target=TARGET,
            from_point=(1, 2),
            to_point=(3, 4),
            execute=True,
            audit=events.append,
        )
        self.assertEqual(events[-1]["event"], "drag.executed")

    def test_blocked_drag_is_audited(self):
        events: list[dict] = []
        run_drag(
            StubDriver(),
            target=SYSTEM_TARGET,
            from_point=(1, 2),
            to_point=(3, 4),
            execute=True,
            audit=events.append,
        )
        self.assertEqual(events[-1]["event"], "drag.blocked")
        self.assertFalse(events[-1]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
