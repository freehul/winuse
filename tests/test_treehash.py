"""treehash 单测：指纹的**稳定性**与 diff 的三类差异。

为什么"稳定性"要拆成这么多条来钉：指纹的全部价值只在于"界面变了没有"这个判断可信。
一份在界面**没变**时也会变的指纹（把坐标、令牌里的快照前缀算进去）会让所有基于它的
决策失灵，而这种失灵是**静默**的——不抛异常、不报错，只让"没变"看起来像"变了"。
反过来，一份把语义字段漏掉的指纹会让真变化逃过检测。两个方向都要钉住。

纯 stdlib、不接触真实桌面（只喂字典）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.treehash import (  # noqa: E402
    HASHED_FIELDS,
    compute_tree_hash,
    diff_trees,
    element_key,
)


def el(
    i,
    role="Button",
    label=None,
    value=None,
    actions=None,
    enabled=True,
    depth=1,
    x=10,
    y=20,
    w=90,
    h=30,
    token=None,
    snapshot="s00000001",
    **extra,
):
    """造一个元素（字段名与驱动实测一致：frame 是原生桌面坐标、可随窗口移动而变）。"""
    out = {
        "actions": list(actions) if actions else [],
        "depth": depth,
        "element_index": i,
        "element_token": token if token is not None else f"{snapshot}:{i}",
        "enabled": enabled,
        "frame": {"x": x, "y": y, "w": w, "h": h},
        "role": role,
    }
    if label is not None:
        out["label"] = label
    if value is not None:
        out["value"] = value
    out.update(extra)
    return out


class TestHashStability(unittest.TestCase):
    def test_same_input_same_hash(self):
        """同一个元素序列算两次必须一模一样（哈希的第一前提）。"""
        elements = [el(0, "Button", "确定"), el(1, "Edit", value="abc")]
        self.assertEqual(compute_tree_hash(elements), compute_tree_hash(elements))
        self.assertEqual(compute_tree_hash(elements), compute_tree_hash(list(elements)))
        self.assertEqual(len(compute_tree_hash(elements)), 64)  # 小写十六进制 sha256

    def test_empty_tree_is_stable(self):
        self.assertEqual(compute_tree_hash([]), compute_tree_hash([]))
        self.assertEqual(compute_tree_hash(None), compute_tree_hash([]))
        # 认不出的字段不参与哈希：两个不同的未知字段归一成同一个"空元素"
        self.assertEqual(compute_tree_hash([{"nonsense": 1}]), compute_tree_hash([{"other": 2}]))
        # 但元素**条数**是结构信息：有一个（哪怕是空的）元素 ≠ 一个都没有
        self.assertNotEqual(compute_tree_hash([{"nonsense": 1}]), compute_tree_hash([]))

    def test_frame_changes_do_not_affect_hash(self):
        """窗口一移动/缩放/滚动，frame 全变但界面没变——指纹必须不变。"""
        a = [el(0, "Button", "确定", x=10, y=20), el(1, "Edit", value="abc", x=10, y=60)]
        b = [el(0, "Button", "确定", x=1010, y=520, w=500, h=40), el(1, "Edit", value="abc", x=1010, y=560, w=500, h=40)]
        self.assertEqual(compute_tree_hash(a), compute_tree_hash(b))

    def test_token_snapshot_prefix_does_not_affect_hash(self):
        """驱动每读一次状态就换一批快照号，令牌前缀随之翻新——必须排除。

        这是最隐蔽的一条：同一次界面读两次会得到不同的 element_token，若把它算进
        指纹，指纹就会在界面毫无变化时每次都变，整个模块失去意义。
        """
        a = [el(0, "Button", "确定", snapshot="s00000001"), el(1, "Edit", value="abc", snapshot="s00000001")]
        b = [el(0, "Button", "确定", snapshot="s00000039"), el(1, "Edit", value="abc", snapshot="s00000039")]
        self.assertEqual(compute_tree_hash(a), compute_tree_hash(b))

    def test_actions_order_does_not_affect_hash(self):
        """actions 的顺序由驱动内部遍历决定，不是界面属性——排序后再入哈希。"""
        a = [el(0, "Button", "确定", actions=["invoke", "select", "expand"])]
        b = [el(0, "Button", "确定", actions=["expand", "invoke", "select"])]
        self.assertEqual(compute_tree_hash(a), compute_tree_hash(b))

    def test_enabled_default_is_normalized(self):
        """缺 enabled 与显式 enabled=True 是同一件事（state.py 的口径也是缺省为真）。"""
        a = [{"element_index": 0, "element_token": "s00000001:0", "role": "Button", "label": "确定"}]
        b = [{"element_index": 0, "element_token": "s00000001:0", "role": "Button", "label": "确定", "enabled": True}]
        self.assertEqual(compute_tree_hash(a), compute_tree_hash(b))

    def test_unknown_new_field_does_not_affect_hash(self):
        """白名单而非黑名单：驱动新增字段默认不参与哈希。

        若用黑名单，驱动某天新增一个易变字段（时间戳、命中测试计数等）就会**静默**
        毁掉所有指纹——而这类字段正是最可能被加进来的那种。
        """
        a = [el(0, "Button", "确定")]
        b = [el(0, "Button", "确定", device_pixel_ratio=2.0, hit_test_count=7, rendering_epoch="e-99")]
        self.assertEqual(compute_tree_hash(a), compute_tree_hash(b))

    def test_semantic_changes_do_change_hash(self):
        """语义字段变了，指纹必须变——否则真变化会逃过检测。"""
        base = [el(0, "Button", "确定", value="abc", actions=["invoke"])]
        variants = {
            "label": [el(0, "Button", "取消", value="abc", actions=["invoke"])],
            "value": [el(0, "Button", "确定", value="abd", actions=["invoke"])],
            "role": [el(0, "CheckBox", "确定", value="abc", actions=["invoke"])],
            "enabled": [el(0, "Button", "确定", value="abc", actions=["invoke"], enabled=False)],
            "actions": [el(0, "Button", "确定", value="abc", actions=["set_value"])],
            "depth": [el(0, "Button", "确定", value="abc", actions=["invoke"], depth=3)],
            "selected": [el(0, "Button", "确定", value="abc", actions=["invoke"], selected=True)],
        }
        base_hash = compute_tree_hash(base)
        for name, variant in variants.items():
            with self.subTest(field=name):
                self.assertNotEqual(base_hash, compute_tree_hash(variant))

    def test_order_and_count_matter(self):
        """元素序列的顺序本身是结构信息：换个顺序 / 多一个元素，指纹都要变。"""
        a = [el(0, "Button", "确定"), el(1, "Button", "取消")]
        b = [el(1, "Button", "取消"), el(0, "Button", "确定")]
        self.assertNotEqual(compute_tree_hash(a), compute_tree_hash(b))
        self.assertNotEqual(compute_tree_hash(a), compute_tree_hash(a[:1]))

    def test_hashed_fields_are_exactly_the_documented_ones(self):
        """白名单就是对外契约：改了它必须是有意识的改动。"""
        self.assertEqual(
            tuple(HASHED_FIELDS),
            ("depth", "role", "label", "value", "enabled", "selected", "actions"),
        )

    def test_no_volatile_field_in_hashed_fields(self):
        """易变字段（坐标 / 令牌 / 快照）一个都不许混进白名单。"""
        for banned in ("frame", "bounds", "element_token", "token", "snapshot_id", "element_index", "index"):
            self.assertNotIn(banned, HASHED_FIELDS)


class TestElementKey(unittest.TestCase):
    def test_key_prefers_element_index(self):
        self.assertEqual(element_key(el(3), 0), "index:3")

    def test_key_falls_back_to_position(self):
        """没有 element_index 时按位置认；键形状必须一眼能分辨来源。"""
        self.assertEqual(element_key({"role": "Button"}, 5), "pos:5")
        self.assertNotEqual(element_key({"role": "Button"}, 0), element_key(el(0), 0))

    def test_key_uses_token_index_when_index_missing(self):
        """只有令牌时用令牌里的序号（跨快照唯一稳定的部分）。"""
        self.assertEqual(element_key({"element_token": "s00000039:7"}, 0), "index:7")


class TestDiff(unittest.TestCase):
    def test_diff_returns_exactly_the_documented_keys(self):
        out = diff_trees([el(0)], [el(0)])
        self.assertEqual(
            set(out),
            {"added", "removed", "changed", "unchanged_count", "hash_before", "hash_after"},
        )

    def test_identical_trees_all_zero(self):
        before = [el(0, "Button", "确定"), el(1, "Edit", value="abc")]
        after = [el(0, "Button", "确定"), el(1, "Edit", value="abc")]
        out = diff_trees(before, after)

        self.assertEqual(out["added"], [])
        self.assertEqual(out["removed"], [])
        self.assertEqual(out["changed"], [])
        self.assertEqual(out["unchanged_count"], 2)
        self.assertEqual(out["hash_before"], out["hash_after"])

    def test_added_and_removed(self):
        before = [el(0, "Button", "确定"), el(1, "Edit", value="abc")]
        after = [el(0, "Button", "确定"), el(1, "Edit", value="abc"), el(9, "ProgressBar", "正在保存")]

        out = diff_trees(before, after)
        self.assertEqual(out["unchanged_count"], 2)
        self.assertEqual(out["changed"], [])
        self.assertEqual(out["removed"], [])
        self.assertEqual(len(out["added"]), 1)
        self.assertEqual(out["added"][0]["index"], 9)
        self.assertEqual(out["added"][0]["key"], "index:9")
        self.assertEqual(out["added"][0]["role"], "ProgressBar")
        self.assertEqual(out["added"][0]["label"], "正在保存")
        self.assertNotEqual(out["hash_before"], out["hash_after"])

        back = diff_trees(after, before)  # 反向：added/removed 对调
        self.assertEqual(back["added"], [])
        self.assertEqual([e["index"] for e in back["removed"]], [9])

    def test_changed_says_which_fields_changed(self):
        before = [el(0, "Edit", "标题", value="草稿"), el(1, "Button", "确定")]
        after = [el(0, "Edit", "标题", value="已保存"), el(1, "Button", "确定")]

        out = diff_trees(before, after)
        self.assertEqual(out["unchanged_count"], 1)
        self.assertEqual(len(out["changed"]), 1)
        entry = out["changed"][0]
        self.assertEqual(entry["key"], "index:0")
        self.assertEqual(entry["index"], 0)
        self.assertEqual(entry["role"], "Edit")
        self.assertEqual(entry["fields"], ["value"])  # 只有 value 变了
        self.assertEqual(entry["before"], {"value": "草稿"})
        self.assertEqual(entry["after"], {"value": "已保存"})
        self.assertNotEqual(out["hash_before"], out["hash_after"])

    def test_changed_lists_all_differing_fields(self):
        before = [el(0, "Edit", "标题", value="草稿", enabled=True)]
        after = [el(0, "Label", "说明", value="草稿", enabled=False)]
        entry = diff_trees(before, after)["changed"][0]
        self.assertEqual(entry["fields"], ["role", "label", "enabled"])
        self.assertEqual(set(entry["before"]), {"role", "label", "enabled"})

    def test_volatile_change_is_not_reported_as_changed(self):
        """坐标变了不算"变了"：diff 的判定口径必须与哈希同源。

        两处不同源会出现最糟的自相矛盾回执——"哈希没变，但说有几个元素变了"，
        调用方无从判断该信哪一个。
        """
        before = [el(0, "Button", "确定", x=10, y=20)]
        after = [el(0, "Button", "确定", x=900, y=700, w=400, h=88, snapshot="s00000039")]
        out = diff_trees(before, after)

        self.assertEqual(out["changed"], [])
        self.assertEqual(out["unchanged_count"], 1)
        self.assertEqual(out["hash_before"], out["hash_after"])

    def test_positional_key_fallback_pairs_elements(self):
        """没有 element_index 的元素按位置配对，仍能报出内容变化。"""
        before = [{"role": "Button", "label": "确定"}]
        after = [{"role": "Button", "label": "取消"}]
        out = diff_trees(before, after)

        self.assertEqual(out["added"], [])
        self.assertEqual(out["removed"], [])
        self.assertEqual(out["changed"][0]["key"], "pos:0")
        self.assertEqual(out["changed"][0]["fields"], ["label"])

    def test_digest_label_is_truncated_for_readability(self):
        """回执里的 label 会截断（省 token），但**截断只影响展示**——哈希用全文。"""
        long_label = "标" * 300
        out = diff_trees([], [el(0, "Button", long_label)])
        shown = out["added"][0]["label"]
        self.assertLess(len(shown), len(long_label))
        self.assertTrue(shown.endswith("…"))

        # 两条尾部不同的长文本必须算出不同指纹（截断不能吃掉尾部差异）
        other = "标" * 299 + "尾"
        self.assertNotEqual(compute_tree_hash([el(0, "Button", long_label)]), compute_tree_hash([el(0, "Button", other)]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
