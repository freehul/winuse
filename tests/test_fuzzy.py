"""fuzzy 模糊寻址单测：相似度定义、阈值与 limit 上限、字段解析、长串保护。

全部是**纯函数/纯数据**断言，不接触驱动与桌面。

数据形状照抄 cua-driver 0.28.2 的真实元素字段（2026-09-27 采样）：

- 驱动原始回执：`actions / depth / element_index / element_token / enabled / frame /
  in_web_content / label / parent_index / role / selected / value`
- `core.state` 投影后：`index / token / label / value / role / actions / ...`（短名）

⚠️ 原始回执里**没有** `name` / `text` / `title` 这三个字段——模糊匹配的默认字段名
是调用方习惯叫法，靠 `FIELD_ALIASES` 落到真实字段上（本文件有一条测试专门守住
"别名只能指向真实字段"，防臆造）。
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.fuzzy import (  # noqa: E402
    FIELD_ALIASES,
    MAX_COMPARE_CHARS,
    MAX_FUZZY_LIMIT,
    TEXT_FIELDS,
    find_elements,
    levenshtein,
)
from winuse.driver.errors import WinUseError  # noqa: E402


def raw_element(index, role="Button", label=None, value=None, enabled=True):
    """造一个**驱动原始形状**的元素（字段名与实测一致）。"""
    out = {
        "actions": ["invoke"],
        "depth": 2,
        "element_index": index,
        "element_token": f"s0000006e:{index}",
        "enabled": enabled,
        "frame": {"x": 10, "y": 20, "w": 30, "h": 40},
        "role": role,
    }
    if label is not None:
        out["label"] = label
    if value is not None:
        out["value"] = value
    return out


def projected_element(index, role="Button", label=None, value=None):
    """造一个 **core.state 投影后**的元素（短名）。"""
    out = {"actions": ["invoke"], "index": index, "token": f"s0000006e:{index}", "role": role}
    if label is not None:
        out["label"] = label
    if value is not None:
        out["value"] = value
    return out


class TestLevenshtein(unittest.TestCase):
    def test_equal_strings_are_zero(self):
        self.assertEqual(levenshtein("保存", "保存"), 0)
        self.assertEqual(levenshtein("", ""), 0)

    def test_empty_vs_string_is_its_length(self):
        self.assertEqual(levenshtein("", "abcd"), 4)
        self.assertEqual(levenshtein("abcd", ""), 4)

    def test_classic_distance(self):
        self.assertEqual(levenshtein("kitten", "sitting"), 3)
        self.assertEqual(levenshtein("保存文档", "保存文件"), 1)
        self.assertEqual(levenshtein("abcXXXghij", "abcdefghij"), 3)

    def test_symmetric(self):
        self.assertEqual(
            levenshtein("设置", "设置项"), levenshtein("设置项", "设置")
        )

    def test_non_string_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            levenshtein("a", 1)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_length_gap_stops_early_but_still_reports_over_bound(self):
        """长度差 > 上界 → 直接给下界（长度差），不再算 DP。"""
        got = levenshtein("a" * 100, "a" * 110, max_distance=2)
        self.assertGreater(got, 2)

    def test_oversized_exact_compare_is_refused_not_left_to_hang(self):
        """无上界的精确距离有格子数预算：超了**明确拒绝**，不静默算到天亮。"""
        with self.assertRaises(WinUseError) as ctx:
            levenshtein("x" * 2000, "y" * 2000)
        self.assertEqual(ctx.exception.code, "PARAM_OUT_OF_RANGE")

    def test_bounded_compare_on_huge_strings_returns_promptly(self):
        """带 `max_distance` 时是带内 DP（O(n·k)）——两万个字符也必须秒回。

        若退化成平方复杂度，这一行会跑到超时（4 亿格），所以这条断言本身就是保护。
        """
        got = levenshtein("x" * 20000, "y" * 20000, max_distance=1)
        self.assertGreater(got, 1)

    def test_bounded_matches_exact_within_bound(self):
        """带上界的返回值契约：≤ 上界时**必须是真实距离**，> 上界时只需表示超界。"""
        rng = random.Random(42)
        alphabet = "abcd"
        cases = [("", ""), ("a", ""), ("ab", "ba"), ("保存文件", "保存文档")]
        for _ in range(120):
            la, lb = rng.randint(0, 7), rng.randint(0, 7)
            cases.append(
                ("".join(rng.choice(alphabet) for _ in range(la)),
                 "".join(rng.choice(alphabet) for _ in range(lb)))
            )
        for left, right in cases:
            exact = levenshtein(left, right)
            for bound in (0, 1, 2, 3, 5):
                got = levenshtein(left, right, max_distance=bound)
                if exact <= bound:
                    self.assertEqual(got, exact, f"{left!r} vs {right!r} bound={bound}")
                else:
                    self.assertGreater(got, bound, f"{left!r} vs {right!r} bound={bound}")

    def test_negative_bound_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            levenshtein("a", "b", max_distance=-1)
        self.assertEqual(ctx.exception.code, "PARAM_OUT_OF_RANGE")


class TestAliasesPointAtRealFields(unittest.TestCase):
    def test_alias_targets_are_real_driver_field_names(self):
        """守门：别名只能指向真实存在的元素字段，防止'臆造字段名'。"""
        self.assertTrue(set(FIELD_ALIASES.values()) <= set(TEXT_FIELDS), TEXT_FIELDS)
        for alias in ("name", "text", "title"):
            self.assertIn(alias, FIELD_ALIASES)


class TestFindElements(unittest.TestCase):
    def test_exact_match_scores_one(self):
        elements = [raw_element(0, label="保存"), raw_element(1, label="完全不相干的东西")]
        got = find_elements(elements, text="保存")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["score"], 1.0)
        self.assertEqual(got[0]["index"], 0)
        self.assertEqual(got[0]["token"], "s0000006e:0")
        self.assertEqual(got[0]["matched_field"], "label")
        self.assertEqual(got[0]["matched_value"], "保存")

    def test_one_wrong_letter_still_hits(self):
        """痛点场景：记错/打错一个字，仍然找得到（不必精确子串）。"""
        elements = [
            raw_element(0, label="保存文档"),
            raw_element(1, label="取消"),
        ]
        got = find_elements(elements, text="保存文件")  # 少了一个字/错一个字
        self.assertEqual([item["index"] for item in got], [0])
        self.assertGreater(got[0]["score"], 0.7)
        self.assertLess(got[0]["score"], 1.0)

    def test_substring_hit_scores_one(self):
        """精确子串命中直接满分——既有行为不被'模糊化'降级。"""
        got = find_elements([raw_element(0, label="保存并退出菜单")], text="退出")
        self.assertEqual(got[0]["score"], 1.0)

    def test_threshold_filters_weak_candidates(self):
        elements = [raw_element(0, label="保存文档"), raw_element(1, label="保存文件")]
        loose = find_elements(elements, text="保存文档", threshold=0.7)
        self.assertEqual(len(loose), 2)
        strict = find_elements(elements, text="保存文档", threshold=0.85)
        self.assertEqual([item["index"] for item in strict], [0])

    def test_threshold_boundary_is_inclusive(self):
        """阈值等于得分时**要留下**（3/10 编辑 → 正好 0.7，浮点边界不能误杀）。"""
        got = find_elements([raw_element(0, label="abcdefghij")], text="abcXXXghij", threshold=0.7)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0]["score"], 0.7, places=6)

    def test_no_candidates_returns_empty(self):
        elements = [raw_element(0, label="保存"), raw_element(1, label="取消")]
        self.assertEqual(find_elements(elements, text="打印预览设置"), [])

    def test_decoration_suffix_still_hits(self):
        """真机数据的常见形态：标签尾部带记号（`▲`/`★`/`…`）。

        用文档里留存的**真机采样**（`修改日期▲`）验：调用方不会打出那个记号，
        纯长度归一的分母会把"多一个记号"也算成一步编辑，让本来该命中的掉到阈值下。
        """
        elements = [raw_element(48, label="修改日期▲"), raw_element(52, label="操作")]
        got = find_elements(elements, text="修改日斯")  # 错一个字 + 候选带记号
        self.assertEqual([item["index"] for item in got], [48])
        self.assertGreater(got[0]["score"], 0.7)
        # 出口回的是**完整原标签**（窗口只用于比较，不是裁剪后的东西）
        self.assertEqual(got[0]["matched_value"], "修改日期▲")

    def test_trailing_parenthetical_still_hits(self):
        elements = [raw_element(0, label="打印预览（PDF）"), raw_element(1, label="打印设置项")]
        got = find_elements(elements, text="打印预蓝")  # 错一个字，候选尾部是括号提示
        self.assertEqual([item["index"] for item in got], [0])

    def test_window_trick_does_not_invent_matches(self):
        """窗口只解决"候选多带记号"，不能把不相关的窗口也算成命中。"""
        elements = [raw_element(0, label="打印设置项"), raw_element(1, label="打印预览")]
        got = find_elements(elements, text="导出为表格")
        self.assertEqual(got, [])

    def test_empty_or_bad_elements_returns_empty(self):
        self.assertEqual(find_elements([], text="保存"), [])
        self.assertEqual(find_elements(None, text="保存"), [])
        self.assertEqual(find_elements([None, "x", 3], text="保存"), [])

    def test_limit_caps_results_and_keeps_best(self):
        elements = [raw_element(i, label="保存文档") for i in range(5)]
        elements.append(raw_element(9, label="保存文件"))  # 相似但更差
        got = find_elements(elements, text="保存文档", limit=2)
        self.assertEqual(len(got), 2)
        self.assertEqual([item["index"] for item in got], [0, 1])

    def test_results_sorted_by_score_desc(self):
        elements = [
            raw_element(0, label="保存文件"),
            raw_element(1, label="保存文档"),
            raw_element(2, label="保存文档说明"),
        ]
        got = find_elements(elements, text="保存文档")
        scores = [item["score"] for item in got]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(scores[0], 1.0)

    def test_long_strings_are_handled_without_blowing_up(self):
        """长串保护：三万字的候选值不得让一次匹配变成天文数字级运算。"""
        huge = "残" * 30000
        # ① 完全不相关 → 空结果（截断后仍算不出 0.7 以上）
        self.assertEqual(find_elements([raw_element(0, label=huge)], text="打印"), [])
        # ② 子串命中走的是 O(n) 的 in，不需要 DP，直接满分
        got = find_elements([raw_element(0, label=huge)], text="残残残")
        self.assertEqual(got[0]["score"], 1.0)
        # ③ 截断口径是公开常量
        self.assertGreater(MAX_COMPARE_CHARS, 0)

    def test_matched_value_is_truncated_on_output(self):
        """出口只回前若干字符（与 winuse_state 的 label/value 裁剪同口径）。"""
        got = find_elements([raw_element(0, label="打印预览 " + "详" * 5000)], text="打印预览")
        self.assertEqual(len(got), 1)
        self.assertLess(len(got[0]["matched_value"]), 200)

    def test_fields_filter_narrows_the_compared_fields(self):
        elements = [raw_element(0, label="保存", value="草稿内容"), raw_element(1, value="保存")]
        by_value = find_elements(elements, text="保存", fields=("value",))
        self.assertEqual([item["index"] for item in by_value], [1])
        self.assertEqual(by_value[0]["matched_field"], "value")
        by_label = find_elements(elements, text="保存", fields=("label",))
        self.assertEqual([item["index"] for item in by_label], [0])

    def test_default_fields_resolve_to_real_names_on_raw_elements(self):
        """默认 fields 是调用方叫法（name/text/title），出口必须给**真实字段名**。"""
        got = find_elements([raw_element(0, label="保存", value="未命名草稿")], text="保存")
        self.assertEqual(got[0]["matched_field"], "label")
        got = find_elements([raw_element(0, label="保存", value="未命名草稿")], text="未命名")
        self.assertEqual(got[0]["matched_field"], "value")

    def test_projected_state_elements_also_work(self):
        got = find_elements([projected_element(3, label="设置项")], text="设置")
        self.assertEqual(got[0]["index"], 3)
        self.assertEqual(got[0]["token"], "s0000006e:3")

    def test_item_shape(self):
        got = find_elements([raw_element(0, label="保存")], text="保存")
        self.assertEqual(
            sorted(got[0]), ["index", "matched_field", "matched_value", "score", "token"]
        )

    def test_empty_text_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            find_elements([raw_element(0, label="保存")], text="   ")
        self.assertEqual(ctx.exception.code, "MISSING_PARAM")

    def test_bad_text_type_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            find_elements([raw_element(0, label="保存")], text=7)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_threshold_out_of_range_is_refused(self):
        for bad in (-0.1, 1.5):
            with self.assertRaises(WinUseError, msg=repr(bad)) as ctx:
                find_elements([raw_element(0, label="保存")], text="保存",
                              threshold=bad)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "PARAM_OUT_OF_RANGE", repr(bad))

    def test_threshold_of_wrong_type_is_refused(self):
        """类型错与量级错分开报（与 core.params.strict_bool 同纪律：字符串不许蒙混过关）。"""
        for bad in ("0.7", None, True):
            with self.assertRaises(WinUseError, msg=repr(bad)) as ctx:
                find_elements([raw_element(0, label="保存")], text="保存",
                              threshold=bad)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "INVALID_PARAMS", repr(bad))

    def test_limit_above_cap_is_refused(self):
        for bad in (0, -1, MAX_FUZZY_LIMIT + 1):
            with self.assertRaises(WinUseError, msg=repr(bad)) as ctx:
                find_elements([raw_element(0, label="保存")], text="保存", limit=bad)
            self.assertEqual(ctx.exception.code, "PARAM_OUT_OF_RANGE", repr(bad))

    def test_limit_of_wrong_type_is_refused(self):
        for bad in ("3", None, 3.5, True):
            with self.assertRaises(WinUseError, msg=repr(bad)) as ctx:
                find_elements([raw_element(0, label="保存")], text="保存", limit=bad)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "INVALID_PARAMS", repr(bad))

    def test_limit_accepts_the_cap_itself(self):
        elements = [raw_element(i, label="保存") for i in range(MAX_FUZZY_LIMIT + 5)]
        self.assertEqual(len(find_elements(elements, text="保存", limit=MAX_FUZZY_LIMIT)),
                         MAX_FUZZY_LIMIT)

    def test_unknown_field_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            find_elements([raw_element(0, label="保存")], text="保存", fields=("display_name",))
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_empty_field_tuple_is_refused(self):
        with self.assertRaises(WinUseError) as ctx:
            find_elements([raw_element(0, label="保存")], text="保存", fields=())
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
