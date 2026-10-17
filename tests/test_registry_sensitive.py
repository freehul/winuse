"""R-11 + R-18：注册表敏感读的接线、路径匹配口径、递归旁路与枚举错误。

三个问题（审查报告 2026-09-27）：

- **R-11**：`guard.assess_registry_read()` 定义了敏感读，但 CLI/MCP 读路径从没调用过
  → 声明与行为脱节。而且它用 `prefix in key` 裸子串匹配：`SAM` 会命中
  `...\\SAMPLE`、`system\\currentcontrolset` 会命中 `HKCU\\System\\CurrentControlSet`
  （HKCU 下那棵树根本不是敏感区）。
- **R-18a**：`read_registry` 只对起始路径做敏感检查，`collect_subkeys` 递归打开每个
  子键时不再检查 → 从 HKLM 根递归可以绕过，继续打开 `HKLM\\SAM` 及其子路径。
- **R-18b**：`enum_values` / `enum_subkeys` 把所有 `OSError` 都当"枚举结束"
  → 权限不足、无效句柄都会静默变成空列表（看起来"这个键没有值"）。

铁律：**不碰真实注册表**，全部走内存假后端。
"""

from __future__ import annotations

import unittest
from unittest import mock

from winuse.core import guard as guard_mod
from winuse.core import registry as reg_mod
from winuse.core.registry import RegReadSpec, read_registry
from winuse.driver.errors import WinUseError


class FakeBackend:
    """内存注册表树；记录每一次 open_key（用来证明"敏感键根本没被打开"）。"""

    def __init__(self, tree=None) -> None:
        self.tree = tree or {}
        self.opened: list[tuple[str, str]] = []

    def open_key(self, hive: str, path: str):
        handle = (hive, path)
        self.opened.append(handle)
        if handle not in self.tree:
            raise reg_mod.RegistryKeyNotFound(f"键不存在：{hive}\\{path}（假后端）")
        return handle

    def enum_values(self, key):
        return list(self.tree[key].get("values", []))

    def enum_subkeys(self, key):
        return list(self.tree[key].get("subkeys", []))

    def close(self, key):
        pass


class TestSensitiveReadMatching(unittest.TestCase):
    """判据口径：组件级边界匹配 + 只在 HKLM 生效。"""

    def test_real_sensitive_paths_are_dangerous(self):
        for path in ("SAM\\SAM", "SECURITY\\Policy\\Secrets", "SYSTEM\\CurrentControlSet\\Services", "BCD00000000"):
            with self.subTest(path=path):
                self.assertTrue(guard_mod.assess_registry_read("HKLM", path).dangerous)

    def test_substring_false_positives_are_gone(self):
        """裸子串匹配的误报：`SAM` 不该命中 `SAMPLE`，系统树不该命中 HKCU。"""
        for hive, path in (
            ("HKLM", "SOFTWARE\\Vendor\\SAMPLE"),
            ("HKLM", "SOFTWARE\\SamClients"),
            ("HKLM", "SOFTWARE\\Vendor\\MySamThing"),
            ("HKCU", "SYSTEM\\CurrentControlSet"),  # HKCU 下这棵树不是敏感区
            ("HKCU", "Software\\Vendor\\Sample"),
        ):
            with self.subTest(hive=hive, path=path):
                self.assertFalse(
                    guard_mod.assess_registry_read(hive, path).dangerous,
                    f"{hive}\\{path} 不该被判敏感（裸子串匹配会误报）",
                )

    def test_suffix_inside_component_is_not_a_match(self):
        """`...\\bcd00000000_something` 不该命中（原来 endswith 会中）。"""
        self.assertFalse(guard_mod.assess_registry_read("HKLM", "SOFTWARE\\bcd00000000_extra").dangerous)

    def test_dangerous_verdict_carries_rule(self):
        verdict = guard_mod.assess_registry_read("HKLM", "SYSTEM\\CurrentControlSet")
        self.assertEqual(verdict.rule, guard_mod.REGISTRY_SENSITIVE_RULE)


class TestReadWiring(unittest.TestCase):
    """敏感读必须真的接上门禁（报告 R-11 的核心：声明了却没人调用）。"""

    def _tree(self):
        return FakeBackend(
            {
                ("HKLM", "SYSTEM\\CurrentControlSet"): {"values": [("Start", 2, 4)], "subkeys": []},
                ("HKLM", SAMPLE := "Software\\Vendor\\App"): {"values": [("Path", "x", 2)], "subkeys": []},
            }
        )

    def test_sensitive_read_requires_confirmation(self):
        backend = self._tree()
        out = read_registry(RegReadSpec(hive="HKLM", path="SYSTEM\\CurrentControlSet"), backend)
        self.assertFalse(out["ok"])
        self.assertTrue(out["requires_confirm"])
        self.assertEqual(out["error"]["code"], "CONFIRM_REQUIRED")
        self.assertEqual(backend.opened, [], "未确认时连键都不该打开")

    def test_sensitive_read_after_confirmation_works(self):
        backend = self._tree()
        out = read_registry(
            RegReadSpec(hive="HKLM", path="SYSTEM\\CurrentControlSet"), backend, confirmed=True
        )
        self.assertTrue(out["ok"])
        # 值枚举与子键枚举各开一次（既有行为）；关键是确认后**确实**读到了
        self.assertEqual(backend.opened, [("HKLM", "SYSTEM\\CurrentControlSet")] * 2)

    def test_plain_read_needs_no_confirmation(self):
        backend = self._tree()
        out = read_registry(RegReadSpec(hive="HKLM", path="Software\\Vendor\\App"), backend)
        self.assertTrue(out["ok"])
        self.assertNotIn("requires_confirm", out)

    def test_sam_is_still_hard_denied_even_with_confirmation(self):
        """SAM/SECURITY 是凭据数据源：保持**硬拒**，不提供"确认后即可读"的通道。"""
        backend = FakeBackend({("HKLM", "SAM"): {"values": [], "subkeys": []}})
        with self.assertRaises(reg_mod.RegistryPathDenied) as ctx:
            read_registry(RegReadSpec(hive="HKLM", path="SAM"), backend, confirmed=True)
        self.assertEqual(ctx.exception.code, "REGISTRY_PATH_DENIED")
        self.assertEqual(backend.opened, [])


class TestRecursiveBypass(unittest.TestCase):
    """R-18a：从 HKLM 根递归时，敏感子树不得被打开。"""

    def _tree(self):
        return FakeBackend(
            {
                ("HKLM", ""): {"subkeys": ["SAM", "SOFTWARE"]},
                ("HKLM", "SAM"): {"subkeys": ["SAM"]},
                ("HKLM", "SAM\\SAM"): {"subkeys": ["Domains"]},
                ("HKLM", "SAM\\SAM\\Domains"): {"subkeys": []},
                ("HKLM", "SOFTWARE"): {"subkeys": ["Vendor"]},
                ("HKLM", "SOFTWARE\\Vendor"): {"subkeys": []},
            }
        )

    def test_recursion_skips_sensitive_subtrees(self):
        backend = self._tree()
        out = read_registry(
            RegReadSpec(hive="HKLM", path="", recurse=True, max_depth=4), backend
        )
        opened_paths = [path for _hive, path in backend.opened]
        self.assertNotIn("SAM", opened_paths, "递归不得打开 SAM")
        self.assertNotIn("SAM\\SAM", opened_paths)
        self.assertNotIn("SAM\\SAM\\Domains", opened_paths)
        self.assertIn("SOFTWARE", opened_paths, "非敏感分支照常展开")

    def test_skipped_paths_are_reported_not_silently_dropped(self):
        backend = self._tree()
        out = read_registry(RegReadSpec(hive="HKLM", path="", recurse=True, max_depth=4), backend)
        self.assertEqual(out.get("skipped_sensitive"), ["SAM"])


class TestEnumerationErrors(unittest.TestCase):
    """R-18b：只有"没有更多项"才算正常结束。"""

    def test_no_more_items_ends_enumeration_normally(self):
        winreg = mock.MagicMock()
        winreg.EnumValue.side_effect = OSError(259, "No more data is available")
        backend = reg_mod._WinregBackend()
        backend._winreg = winreg
        self.assertEqual(backend.enum_values("handle"), [])

    def test_other_oserror_becomes_registry_error(self):
        winreg = mock.MagicMock()
        winreg.EnumValue.side_effect = OSError(5, "Access is denied")
        backend = reg_mod._WinregBackend()
        backend._winreg = winreg
        with self.assertRaises(WinUseError) as ctx:
            backend.enum_values("handle")
        self.assertEqual(ctx.exception.code, "REGISTRY_ENUM_FAILED")

    def test_subkeys_enumeration_error_also_raises(self):
        winreg = mock.MagicMock()
        winreg.EnumKey.side_effect = OSError(6, "Invalid handle")
        backend = reg_mod._WinregBackend()
        backend._winreg = winreg
        with self.assertRaises(WinUseError) as ctx:
            backend.enum_subkeys("handle")
        self.assertEqual(ctx.exception.code, "REGISTRY_ENUM_FAILED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
