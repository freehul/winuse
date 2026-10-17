"""winuse_registry 单测：路径门禁 + 只读读取 + 写入意图。

**完全不碰真实注册表**：读取一律走注入的假后端（内存里的树），
写操作只生成意图对象、连 winreg 都不导入（有测试专门证明这点）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.registry import (  # noqa: E402
    RegReadSpec,
    RegWriteSpec,
    RegistryAccessDenied,
    RegistryHiveUnknown,
    RegistryKeyNotFound,
    RegistryPathDenied,
    RegistryValueNotFound,
    UnknownValueType,
    collect_subkeys,
    full_key_path,
    is_denied_path,
    normalize_hive,
    normalize_path,
    plan_registry_write,
    read_registry,
    render_value,
    render_values,
    value_type_name,
)
from winuse.driver.errors import MissingParam, WinUseError  # noqa: E402


class FakeBackend:
    """内存注册表树：{(hive, path): {"values": [...], "subkeys": [...]}}。"""

    def __init__(self, tree=None) -> None:
        self.tree = tree or {}
        self.opened: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []

    def open_key(self, hive: str, path: str):
        handle = (hive, path)
        if handle not in self.tree:
            raise RegistryKeyNotFound(f"键不存在：{hive}\\{path}（假后端）")
        self.opened.append(handle)
        return handle

    def enum_values(self, key):
        return list(self.tree[key].get("values", []))

    def enum_subkeys(self, key):
        return list(self.tree[key].get("subkeys", []))

    def close(self, key):
        self.closed.append(key)


#: 样例键路径（占位值，非真实装机数据）
SAMPLE_PATH = "Software\\Vendor\\App"


def sample_tree() -> FakeBackend:
    return FakeBackend(
        {
            ("HKCU", ""): {"subkeys": ["Software"]},
            ("HKCU", "Software"): {"subkeys": ["Vendor", "Other"]},
            ("HKCU", SAMPLE_PATH): {
                "values": [
                    ("", "未命名的样例值", 1),
                    ("Path", "C:\\Program Files\\Vendor\\App", 2),
                    ("Flags", 3, 4),
                    ("Blob", b"\x00\x01\x02", 3),
                    ("List", ["a", "b"], 7),
                ],
                "subkeys": ["B", "A"],
            },
            ("HKCU", SAMPLE_PATH + "\\A"): {"subkeys": ["A1"]},
            ("HKCU", SAMPLE_PATH + "\\A\\A1"): {"subkeys": ["A2"]},
            ("HKCU", SAMPLE_PATH + "\\A\\A1\\A2"): {"subkeys": ["A3"]},
            ("HKCU", SAMPLE_PATH + "\\A\\A1\\A2\\A3"): {"subkeys": []},
            ("HKCU", SAMPLE_PATH + "\\B"): {"subkeys": []},
        }
    )


class TestNormalizeHive(unittest.TestCase):
    def test_short_names(self):
        for hive in ("HKCU", "HKLM", "HKCR", "HKU"):
            self.assertEqual(normalize_hive(hive), hive)

    def test_case_insensitive_and_trimmed(self):
        self.assertEqual(normalize_hive(" hklm "), "HKLM")

    def test_long_name_aliases(self):
        self.assertEqual(normalize_hive("HKEY_LOCAL_MACHINE"), "HKLM")
        self.assertEqual(normalize_hive("HKEY_CURRENT_USER"), "HKCU")

    def test_unknown_hive_raises_contract_code(self):
        for bad in ("HKCC", "", None, "LOCAL_MACHINE"):
            with self.subTest(bad=bad):
                with self.assertRaises(RegistryHiveUnknown) as ctx:
                    normalize_hive(bad)  # type: ignore[arg-type]
                self.assertEqual(ctx.exception.code, "REGISTRY_HIVE_UNKNOWN")
                self.assertIsInstance(ctx.exception, WinUseError)


class TestNormalizePath(unittest.TestCase):
    def test_strips_and_collapses(self):
        self.assertEqual(normalize_path("\\Software\\\\Vendor\\"), "Software\\Vendor")

    def test_forward_slashes(self):
        self.assertEqual(normalize_path("Software/Vendor/App"), SAMPLE_PATH)

    def test_empty_and_none(self):
        self.assertEqual(normalize_path(""), "")
        self.assertEqual(normalize_path(None), "")
        self.assertEqual(normalize_path("\\\\"), "")

    def test_full_key_path(self):
        self.assertEqual(full_key_path("hklm", SAMPLE_PATH), "HKLM\\" + SAMPLE_PATH)
        self.assertEqual(full_key_path("HKLM", ""), "HKLM")
        self.assertEqual(full_key_path(" hklm ", "\\Software\\\\Vendor\\"), "HKLM\\Software\\Vendor")

    def test_full_key_path_keeps_unknown_hive_verbatim(self):
        # 展示用字符串不该因陌生根键抛错（门禁另有 normalize_hive 把关）
        self.assertEqual(full_key_path("BOGUS", "A"), "BOGUS\\A")


class TestSensitivePathGate(unittest.TestCase):
    DENIED_CASES = [
        ("HKLM", "SAM"),
        ("HKLM", "SAM\\"),
        ("HKLM", "SAM\\SAM"),
        ("HKLM", "SAM\\Domains\\Account"),
        ("HKLM", "\\sam\\sam\\"),
        ("HKLM", "/SAM/SAM"),
        ("HKLM", "SECURITY"),
        ("HKLM", "SECURITY\\Policy\\Secrets"),
        ("HKLM", "security\\policy"),
        ("hklm", "Sam"),
    ]
    ALLOWED_CASES = [
        ("HKCU", "SAM\\SAM"),
        ("HKU", "SAM"),
        ("HKLM", "SOFTWARE\\Vendor\\App"),
        ("HKLM", "SAMClients"),
        ("HKLM", "SECURITYX"),
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Lsa"),
        ("HKLM", ""),
    ]

    def test_denied_paths(self):
        for hive, path in self.DENIED_CASES:
            with self.subTest(hive=hive, path=path):
                self.assertTrue(is_denied_path(hive, path))
                with self.assertRaises(RegistryPathDenied) as ctx:
                    read_registry(RegReadSpec(hive=hive, path=path), backend=sample_tree())
                self.assertEqual(ctx.exception.code, "REGISTRY_PATH_DENIED")

    def test_allowed_paths(self):
        for hive, path in self.ALLOWED_CASES:
            with self.subTest(hive=hive, path=path):
                self.assertFalse(is_denied_path(hive, path))

    def test_denied_path_is_rejected_before_any_key_is_opened(self):
        backend = sample_tree()
        with self.assertRaises(RegistryPathDenied):
            read_registry(RegReadSpec(hive="HKLM", path="SAM\\SAM"), backend=backend)
        self.assertEqual(backend.opened, [])

    def test_reason_is_explained(self):
        with self.assertRaises(RegistryPathDenied) as ctx:
            read_registry(RegReadSpec(hive="HKLM", path="SECURITY\\Policy"), backend=sample_tree())
        self.assertIn("SECURITY", ctx.exception.message)
        self.assertIn("机密", ctx.exception.detail)

    def test_unknown_hive_still_raises_hive_error_not_denied(self):
        with self.assertRaises(RegistryHiveUnknown):
            read_registry(RegReadSpec(hive="HKCC", path="SAM"), backend=sample_tree())


class TestValueRendering(unittest.TestCase):
    def test_type_names(self):
        self.assertEqual(value_type_name(1), "REG_SZ")
        self.assertEqual(value_type_name(4), "REG_DWORD")
        self.assertEqual(value_type_name(11), "REG_QWORD")
        self.assertEqual(value_type_name(7), "REG_MULTI_SZ")

    def test_unknown_type_is_reported_verbatim(self):
        self.assertEqual(value_type_name(99), "REG_UNKNOWN(99)")
        self.assertEqual(value_type_name("abc"), "REG_UNKNOWN(abc)")

    def test_render_string_and_int(self):
        self.assertEqual(render_value("x"), "x")
        self.assertEqual(render_value(3), 3)

    def test_render_long_string_is_truncated(self):
        got = render_value("a" * 1000)
        self.assertTrue(got.endswith("…"))
        self.assertEqual(len(got), 513)

    def test_render_binary_gives_length_and_preview(self):
        got = render_value(b"\x00\xff")
        self.assertEqual(got["kind"], "binary")
        self.assertEqual(got["length"], 2)
        self.assertEqual(got["preview"], "00 ff")

    def test_render_large_binary_marks_truncation(self):
        got = render_value(bytes(range(64)))
        self.assertTrue(got["preview"].endswith("…"))
        self.assertEqual(got["length"], 64)

    def test_render_multi_sz_list(self):
        self.assertEqual(render_value(["a", "b"]), ["a", "b"])

    def test_render_values_maps_default_name_to_empty_string(self):
        got = render_values([("", "v", 1), (None, "w", 1)])
        self.assertEqual(got[0]["name"], "")
        self.assertEqual(got[1]["name"], "")

    def test_render_values_skips_malformed_items(self):
        got = render_values([("ok", 1, 4), "不是三元组", {"name": "d", "value": 2, "type": 4}])
        self.assertEqual([item["name"] for item in got], ["ok", "d"])

    def test_render_values_tolerates_none(self):
        self.assertEqual(render_values(None), [])


class TestReadRegistry(unittest.TestCase):
    def test_lists_values_and_one_level_of_subkeys(self):
        got = read_registry(RegReadSpec(hive="HKCU", path=SAMPLE_PATH), backend=sample_tree())
        self.assertTrue(got["ok"])
        self.assertEqual(got["hive"], "HKCU")
        self.assertEqual(got["path"], SAMPLE_PATH)
        self.assertEqual(got["key"], "HKCU\\" + SAMPLE_PATH)
        self.assertEqual(got["subkeys"], ["A", "B"])  # 一层，且排序稳定
        self.assertFalse(got["subkeys_truncated"])
        names = [item["name"] for item in got["values"]]
        self.assertEqual(names, ["", "Path", "Flags", "Blob", "List"])
        self.assertEqual(got["values"][1]["type"], "REG_EXPAND_SZ")
        self.assertEqual(got["values"][2]["type"], "REG_DWORD")

    def test_value_name_filter(self):
        got = read_registry(
            RegReadSpec(hive="HKCU", path=SAMPLE_PATH, value_name="Flags"), backend=sample_tree()
        )
        self.assertEqual(len(got["values"]), 1)
        self.assertEqual(got["values"][0]["value"], 3)

    def test_default_value_lookup_by_empty_name(self):
        got = read_registry(
            RegReadSpec(hive="HKCU", path=SAMPLE_PATH, value_name=""), backend=sample_tree()
        )
        self.assertEqual(len(got["values"]), 1)
        self.assertEqual(got["values"][0]["value"], "未命名的样例值")

    def test_missing_value_name_raises_contract_code(self):
        with self.assertRaises(RegistryValueNotFound) as ctx:
            read_registry(
                RegReadSpec(hive="HKCU", path=SAMPLE_PATH, value_name="Nope"), backend=sample_tree()
            )
        self.assertEqual(ctx.exception.code, "REGISTRY_VALUE_NOT_FOUND")
        self.assertIn("Flags", ctx.exception.detail)  # 报错时把现有值列出来，便于自纠

    def test_recurse_returns_relative_paths_within_depth(self):
        got = read_registry(
            RegReadSpec(hive="HKCU", path=SAMPLE_PATH, recurse=True, max_depth=3),
            backend=sample_tree(),
        )
        self.assertEqual(got["subkeys"], ["A", "A\\A1", "A\\A1\\A2", "B"])
        self.assertTrue(got["recurse"])

    def test_max_subkeys_truncation_is_marked(self):
        got = read_registry(
            RegReadSpec(hive="HKCU", path=SAMPLE_PATH, recurse=True, max_depth=5, max_subkeys=2),
            backend=sample_tree(),
        )
        self.assertTrue(got["subkeys_truncated"])
        self.assertEqual(got["subkeys"], ["A", "A\\A1"])

    def test_key_not_found(self):
        with self.assertRaises(RegistryKeyNotFound) as ctx:
            read_registry(RegReadSpec(hive="HKCU", path="Software\\Nope"), backend=sample_tree())
        self.assertEqual(ctx.exception.code, "REGISTRY_KEY_NOT_FOUND")

    def test_root_key_is_readable(self):
        got = read_registry(RegReadSpec(hive="HKCU"), backend=sample_tree())
        self.assertEqual(got["path"], "")
        self.assertEqual(got["subkeys"], ["Software"])
        self.assertEqual(got["values"], [])

    def test_injected_backend_is_used_and_closed(self):
        backend = sample_tree()
        read_registry(RegReadSpec(hive="HKCU", path=SAMPLE_PATH), backend=backend)
        self.assertEqual(backend.closed, backend.opened)
        self.assertTrue(backend.opened)

    def test_output_is_json_serializable_and_notes_the_doors(self):
        import json

        got = read_registry(RegReadSpec(hive="HKCU", path=SAMPLE_PATH), backend=sample_tree())
        json.dumps(got)  # 二进制值被渲染成字典，整体可直接序列化
        self.assertIn("REGISTRY_PATH_DENIED", got["note"])

    def test_non_windows_style_backend_failure_is_normalized(self):
        class BrokenBackend(FakeBackend):
            def enum_values(self, key):
                raise RegistryAccessDenied("没有读取权限", "样例")

        with self.assertRaises(RegistryAccessDenied) as ctx:
            read_registry(
                RegReadSpec(hive="HKLM", path="SOFTWARE\\Vendor"), backend=BrokenBackend(
                    {("HKLM", "SOFTWARE\\Vendor"): {}}
                )
            )
        self.assertEqual(ctx.exception.code, "REGISTRY_ACCESS_DENIED")


class TestCollectSubkeys(unittest.TestCase):
    def test_max_depth_one_equals_flat_listing(self):
        backend = sample_tree()
        got = collect_subkeys(backend, "HKCU", SAMPLE_PATH, recurse=True, max_depth=1)
        self.assertEqual(got["names"], ["A", "B"])

    def test_not_recurse_ignores_depth(self):
        got = collect_subkeys(sample_tree(), "HKCU", SAMPLE_PATH, recurse=False)
        self.assertEqual(got["names"], ["A", "B"])

    def test_missing_key_raises(self):
        with self.assertRaises(RegistryKeyNotFound):
            collect_subkeys(sample_tree(), "HKCU", "Software\\Nope")


class TestPlanRegistryWrite(unittest.TestCase):
    def test_set_value_intent(self):
        got = plan_registry_write(
            RegWriteSpec(hive="HKLM", path="SOFTWARE\\Vendor\\App", value_name="Setting", value="1")
        )
        self.assertEqual(got["ok"], True)
        self.assertEqual(got["requires_confirm"], True)
        self.assertEqual(got["intent"], "registry.set_value")
        self.assertEqual(
            got["would_run"],
            [
                "reg", "add", "HKLM\\SOFTWARE\\Vendor\\App",
                "/v", "Setting", "/t", "REG_SZ", "/d", "1", "/f",
            ],
        )
        self.assertEqual(
            sorted(got.keys()), ["intent", "note", "ok", "requires_confirm", "would_run"]
        )

    def test_dword_and_expand_sz_types(self):
        got = plan_registry_write(
            RegWriteSpec(
                hive="HKCU", path=SAMPLE_PATH, value_name="Flags", value="1", value_type="reg_dword"
            )
        )
        self.assertIn("REG_DWORD", got["would_run"])

    def test_default_value_uses_ve_switch(self):
        got = plan_registry_write(
            RegWriteSpec(hive="HKCU", path=SAMPLE_PATH, value_name="", value="x")
        )
        self.assertIn("/ve", got["would_run"])
        self.assertNotIn("/v", got["would_run"])

    def test_delete_value_intent(self):
        got = plan_registry_write(
            RegWriteSpec(hive="HKCU", path=SAMPLE_PATH, value_name="Old", delete=True)
        )
        self.assertEqual(got["intent"], "registry.delete_value")
        self.assertEqual(got["would_run"], ["reg", "delete", "HKCU\\" + SAMPLE_PATH, "/v", "Old", "/f"])

    def test_delete_default_value_uses_ve(self):
        got = plan_registry_write(
            RegWriteSpec(hive="HKCU", path=SAMPLE_PATH, value_name="", delete=True)
        )
        self.assertEqual(got["would_run"], ["reg", "delete", "HKCU\\" + SAMPLE_PATH, "/ve", "/f"])

    def test_sensitive_path_is_denied_for_writes_too(self):
        for path in ("SAM\\SAM", "SECURITY\\Policy"):
            with self.subTest(path=path):
                with self.assertRaises(RegistryPathDenied) as ctx:
                    plan_registry_write(
                        RegWriteSpec(hive="HKLM", path=path, value_name="X", value="1")
                    )
                self.assertEqual(ctx.exception.code, "REGISTRY_PATH_DENIED")

    def test_unknown_value_type_raises(self):
        with self.assertRaises(UnknownValueType) as ctx:
            plan_registry_write(
                RegWriteSpec(hive="HKCU", path=SAMPLE_PATH, value_name="X", value="1", value_type="REG_BOGUS")
            )
        self.assertEqual(ctx.exception.code, "UNKNOWN_VALUE_TYPE")

    def test_missing_pieces_raise_missing_param(self):
        cases = [
            RegWriteSpec(hive="HKCU", path="", value_name="X", value="1"),
            RegWriteSpec(hive="HKCU", path=SAMPLE_PATH, value_name=None, value="1"),
            RegWriteSpec(hive="HKCU", path=SAMPLE_PATH, value_name="X", value=None),
        ]
        for spec in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(MissingParam) as ctx:
                    plan_registry_write(spec)
                self.assertEqual(ctx.exception.code, "MISSING_PARAM")

    def test_delete_does_not_require_a_value(self):
        got = plan_registry_write(
            RegWriteSpec(hive="HKCU", path=SAMPLE_PATH, value_name="X", value=None, delete=True)
        )
        self.assertTrue(got["requires_confirm"])

    def test_unknown_hive_raises(self):
        with self.assertRaises(RegistryHiveUnknown):
            plan_registry_write(RegWriteSpec(hive="HKCC", path="A", value_name="X", value="1"))

    def test_plan_never_touches_winreg(self):
        # 打桩真后端：只要有人实例化它，测试就失败——证明写路径只是"出意图"
        def boom():
            raise AssertionError("plan_registry_write 不应创建 winreg 后端")

        with mock.patch("winuse.core.registry._default_backend", boom):
            with mock.patch("winuse.core.registry._WinregBackend", boom):
                got = plan_registry_write(
                    RegWriteSpec(hive="HKCU", path=SAMPLE_PATH, value_name="X", value="1")
                )
        self.assertEqual(got["intent"], "registry.set_value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
