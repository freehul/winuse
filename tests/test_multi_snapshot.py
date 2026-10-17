"""R-14：批内快照刷新与步骤回执口径。

两个问题（审查报告 R-14）：

1. 批处理文档规定"整批共用传入的 snapshot"，但 `set_value` 的写值门禁会**自己读一次
   状态**——那次读会刷新驱动快照（令牌随之改变）。第一步写值之后，后续步骤仍拿着
   旧快照：少则 `stale_element_token`，多则指向同序号的**另一个控件**（误点）。
2. 步骤回执里的 `args` 是 `run_multi` 预组装的产物，不一定等于 `run_act` 最终下发的
   参数（门禁会把写值调用切到新令牌上）→ 审计/回执描述的不是"真正发出去的东西"。

处置：
- 步骤回执一律引用 `run_act` 的**最终**下发参数
- 批内一旦发生快照轮换，后续**按索引**的步骤直接拒绝（`INDEX_INVALIDATED_BY_WRITE`）
  ——宁可让 agent 分批，也不要拿旧索引去猜控件；用 `element_token` 的步骤不受影响
  （令牌自带快照信息）
"""

from __future__ import annotations

import unittest

from winuse.core.multi_input import run_multi
from winuse.core.state import StateOptions

TARGET = {"pid": 100, "window_id": 200, "app_name": "demo.exe", "title": "占位窗口"}
SNAP = "s00000001"
DRAFT = "用户草稿"


class FakeDriver:
    """记录每一笔下发；`get_window_state` 返回给定元素（模拟写值门禁那次读）。"""

    def __init__(self, elements=None):
        self.elements = elements if elements is not None else []
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, args: dict | None = None):
        self.calls.append((tool, dict(args or {})))
        if tool == "get_window_state":
            return {"elements": self.elements, "snapshot_id": "s99999999"}
        return {"ok": True, "tool": tool}

    @property
    def actions(self) -> list[tuple[str, dict]]:
        """排除读状态类调用，只看真正的动作下发。"""
        return [(t, a) for t, a in self.calls if t != "get_window_state"]


def _empty_element(index=0, token="t9"):
    return [{"index": index, "token": token, "value": "", "label": "空框"}]


class TestSnapshotRotation(unittest.TestCase):
    def test_index_step_after_write_is_refused(self):
        """批内写值刷新过快照 → 后续按索引的步骤必须拒绝，不能拿旧索引硬发。"""
        driver = FakeDriver(_empty_element(index=0, token="t9"))
        out = run_multi(
            driver,
            [
                {"action": "set_value", "element_index": 0, "value": "新值"},
                {"action": "click", "element_index": 1},
            ],
            TARGET,
            SNAP,
            execute=True,
            confirmed=True,
        )
        self.assertEqual(out["steps"][0]["ok"], True)
        self.assertEqual(out["steps"][1]["code"], "INDEX_INVALIDATED_BY_WRITE")
        self.assertEqual(out["stopped_at"], 1)
        self.assertEqual(
            [t for t, _ in driver.actions], ["set_value"], "第二步不许下发"
        )

    def test_token_step_after_write_still_runs(self):
        """用令牌的步骤不受影响（令牌自带快照信息）。"""
        driver = FakeDriver(_empty_element(index=0, token="t9"))
        out = run_multi(
            driver,
            [
                {"action": "set_value", "element_index": 0, "value": "新值"},
                {"action": "click", "element_token": "s99999999:5"},
            ],
            TARGET,
            SNAP,
            execute=True,
            confirmed=True,
        )
        self.assertTrue(out["ok"])
        self.assertEqual([t for t, _ in driver.actions], ["set_value", "click"])

    def test_rotation_is_reported_on_the_batch(self):
        driver = FakeDriver(_empty_element(index=0, token="t9"))
        out = run_multi(
            driver,
            [{"action": "set_value", "element_index": 0, "value": "新值"}],
            TARGET,
            SNAP,
            execute=True,
            confirmed=True,
        )
        self.assertEqual(out.get("snapshot_rotated_at"), 0, "批层要说明在哪一步轮换过快照")

    def test_no_rotation_when_nothing_refreshes(self):
        driver = FakeDriver()
        out = run_multi(
            driver,
            [{"action": "click", "element_index": 1}, {"action": "click", "element_index": 2}],
            TARGET,
            SNAP,
            execute=True,
            confirmed=True,
        )
        self.assertTrue(out["ok"])
        self.assertNotIn("snapshot_rotated_at", out)


class TestStepArgsAreFinal(unittest.TestCase):
    def test_step_record_shows_the_args_actually_sent(self):
        """门禁把写值切到新令牌后，回执里必须是切过之后的参数。"""
        driver = FakeDriver(_empty_element(index=0, token="t9"))
        out = run_multi(
            driver,
            [{"action": "set_value", "element_index": 0, "value": "新值"}],
            TARGET,
            SNAP,
            execute=True,
            confirmed=True,
        )
        step_args = out["steps"][0]["result"]["args"]
        sent = dict(driver.actions[0][1])
        self.assertEqual(step_args, sent, "回执描述的必须就是真正发出去的那份参数")
        self.assertEqual(step_args.get("element_token"), "t9", "应按门禁刷新后的令牌下发")
        self.assertNotIn("element_index", step_args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
