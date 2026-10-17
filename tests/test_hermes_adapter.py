"""Hermes 插件门面（adapters/hermes）单测。

要点：按官方姿势把插件当"命名空间子包"加载（相对导入可用），再用假 ctx 断言注册结果；
不碰真 HERMES_HOME（部署测试全在临时目录）。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parents[1] / "adapters" / "hermes"


def load_namespace_package(dir_path: Path, pkg_name: str, root_name: str = "hermes_plugins"):
    """把目录当 `<root_name>.<pkg_name>` 子包加载，返回模块对象。"""
    root = sys.modules.get(root_name)
    if root is None:
        root = types.ModuleType(root_name)
        root.__path__ = []  # type: ignore[attr-defined]
        sys.modules[root_name] = root
    full = f"{root_name}.{pkg_name}"
    spec = importlib.util.spec_from_file_location(
        full, dir_path / "__init__.py", submodule_search_locations=[str(dir_path)]
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    module.__package__ = full
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


def load_plain_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bridge_module():
    """取插件的 core_bridge 子模块——必须显式 import，属性访问不会自动加载子模块。"""
    load_namespace_package(ADAPTER_DIR, "winuse")
    return importlib.import_module("hermes_plugins.winuse.core_bridge")


class FakeCtx:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def register_tool(self, name, toolset, schema, handler, **kw) -> None:  # noqa: ANN001
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "description": kw.get("description", ""),
            "emoji": kw.get("emoji", ""),
        }


class PluginRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin = load_namespace_package(ADAPTER_DIR, "winuse")
        cls.bridge = __import__("hermes_plugins.winuse.core_bridge", fromlist=["x"])
        cls.ctx = FakeCtx()
        cls.plugin.register(cls.ctx)

    def test_registers_tool_table_from_core(self) -> None:
        """工具表以核心为唯一出处：插件注册的集合必须等于核心导出的工具表。"""
        core_tools = json.loads(self.bridge.catalog_json()).get("tools") or []
        expected = {t["name"] for t in core_tools}
        self.assertTrue(expected, "核心工具表不应为空")
        self.assertEqual(set(self.ctx.tools), expected)
        for basic in ("winuse_state", "winuse_act", "winuse_display"):
            self.assertIn(basic, self.ctx.tools)

    def test_every_tool_uses_one_toolset_and_has_object_parameters(self) -> None:
        for name, entry in self.ctx.tools.items():
            with self.subTest(tool=name):
                self.assertEqual(entry["toolset"], "winuse")
                self.assertIsInstance(entry["schema"], dict)
                self.assertEqual(entry["schema"]["name"], name)
                # schema['parameters'] 非 dict 会被 Hermes 注册期直接拒绝
                self.assertIsInstance(entry["schema"]["parameters"], dict)
                self.assertTrue(entry["description"])
                self.assertTrue(entry["emoji"])

    def test_act_requires_action_and_defaults_to_dry_run_in_schema(self) -> None:
        schema = self.ctx.tools["winuse_act"]["schema"]
        self.assertEqual(schema["parameters"]["required"], ["action"])
        self.assertNotIn("execute", schema["parameters"].get("required", []))
        self.assertIn("干跑", schema["description"])

    def test_handler_is_callable(self) -> None:
        self.assertTrue(callable(self.ctx.tools["winuse_state"]["handler"]))


class BuildArgsTests(unittest.TestCase):
    """参数翻译是纯函数——穷举断言，不碰真桌面、不起子进程。"""

    def setUp(self) -> None:
        self.build = bridge_module().build_args

    def test_state_translates_filters(self) -> None:
        argv = self.build(
            "winuse_state",
            {"app": "Hermes", "query": "会话", "max_elements": 400, "frames": True, "no_windows": True},
        )
        self.assertEqual(argv[0], "state")
        self.assertIn("--app", argv)
        self.assertIn("Hermes", argv)
        self.assertIn("--query", argv)
        self.assertIn("--max", argv)
        self.assertIn("400", argv)
        self.assertIn("--frames", argv)
        self.assertIn("--no-windows", argv)

    def test_state_omits_unset_options(self) -> None:
        self.assertEqual(self.build("winuse_state", {}), ["state"])
        self.assertEqual(self.build("winuse_state", {"app": None, "frames": False}), ["state"])

    def test_act_is_dry_run_without_execute(self) -> None:
        argv = self.build("winuse_act", {"action": "click", "element_index": 7, "snapshot": "abc"})
        self.assertEqual(argv[0], "act")
        self.assertIn("--do", argv)
        self.assertIn("click", argv)
        self.assertIn("--index", argv)
        self.assertIn("--snapshot", argv)
        self.assertNotIn("--execute", argv)

    def test_act_executes_only_when_asked(self) -> None:
        argv = self.build("winuse_act", {"action": "set_value", "value": "hi", "execute": True})
        self.assertIn("--execute", argv)
        self.assertIn("--value", argv)

    def test_display_flags(self) -> None:
        self.assertEqual(self.build("winuse_display", {}), ["display"])
        self.assertEqual(self.build("winuse_display", {"no_windows": True}), ["display", "--no-windows"])

    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.build("winuse_nope", {})


class BridgeErrorShapeTests(unittest.TestCase):
    """桥接层失败必须返回**结构化 JSON 字符串**（不抛异常进对话流程）。"""

    def setUp(self) -> None:
        self.bridge = bridge_module()

    def test_unknown_tool_returns_json_string(self) -> None:
        out = self.bridge.call_core("winuse_nope", {})
        self.assertIsInstance(out, str)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "UNKNOWN_TOOL")

    def test_missing_core_returns_json_string(self) -> None:
        original = self.bridge.project_root
        self.bridge.project_root = lambda: None
        try:
            payload = json.loads(self.bridge.call_core("winuse_display", {}))
        finally:
            self.bridge.project_root = original
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "CORE_NOT_FOUND")


class InstallTests(unittest.TestCase):
    """部署脚本：幂等 + 记录核心位置 + 不搬运行时垃圾。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.install = load_plain_module(ADAPTER_DIR / "install.py", "winuse_install_for_test")

    def test_install_is_idempotent_and_records_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(self.install.install(home), 0)
            self.assertEqual(self.install.install(home), 0)  # 二次执行不报错
            dest = home / "plugins" / "winuse"
            for name in self.install.COPY_FILES:
                self.assertTrue((dest / name).is_file(), name)
            location = (dest / "_location.py").read_text(encoding="utf-8")
            self.assertIn(str(self.install.PROJECT_ROOT), location)
            self.assertFalse(list(dest.rglob("__pycache__")))

    def test_uninstall_removes_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.install.install(home)
            self.assertEqual(self.install.uninstall(home), 0)
            self.assertFalse((home / "plugins" / "winuse").exists())

    def test_resolve_home_prefers_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.install.resolve_home(tmp), Path(tmp).resolve())


class InstallGuardTests(unittest.TestCase):
    """部署目标合理性 + 部署后自检：这两件事都是「错一次很难发现」的坑。

    - 目标写错（例如在 git-bash 里 `--home D:\\HermesAgent` 被吃掉反斜杠，
      参数退化成裸目录名）会让副本装进**项目目录内**，混进 git 视野且无人察觉；
    - 副本悄悄过期（改了源码没重跑 install.py）时，运行中的 agent 用的是旧翻译层，
      但工具表却由核心现读、看起来是新的——这种「半新」错位只能靠比对内容发现。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.install = load_plain_module(ADAPTER_DIR / "install.py", "winuse_install_guard_test")

    @staticmethod
    def _hermes_home(tmp: str) -> Path:
        """造一个像 Hermes 主目录的临时目标（有 config.yaml 这个标志物）。"""
        home = Path(tmp)
        (home / "config.yaml").write_text("model: x\n", encoding="utf-8")
        return home

    def test_refuses_target_inside_project(self) -> None:
        """目标落在项目目录内一定错：拒绝部署且一个文件都不创建。"""
        target = self.install.PROJECT_ROOT / ".deploy-guard-probe"
        try:
            self.assertNotEqual(self.install.install(target), 0)
            self.assertFalse(target.exists())
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_refuses_existing_unrelated_dir(self) -> None:
        """已存在但不像 Hermes 主目录的目录：拒绝，别往里扔插件。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "notes.txt").write_text("x", encoding="utf-8")
            self.assertNotEqual(self.install.install(home), 0)
            self.assertFalse((home / "plugins").exists())

    def test_accepts_existing_hermes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.install.install(self._hermes_home(tmp)), 0)

    def test_accepts_fresh_empty_dir(self) -> None:
        """全新安装（目录还是空的）不该被拦。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.install.install(Path(tmp)), 0)

    def test_force_overrides_target_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "notes.txt").write_text("x", encoding="utf-8")
            self.assertEqual(self.install.install(home, force=True), 0)

    def test_verify_is_clean_right_after_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self._hermes_home(tmp)
            self.assertEqual(self.install.install(home), 0)
            self.assertEqual(self.install.verify(home), [])

    def test_verify_flags_stale_copy(self) -> None:
        """副本内容变了（源码更新后没重跑 install.py）→ verify 必须报出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = self._hermes_home(tmp)
            self.assertEqual(self.install.install(home), 0)
            stale = home / "plugins" / "winuse" / "core_bridge.py"
            stale.write_text("# 旧代码\n", encoding="utf-8")
            problems = self.install.verify(home)
            self.assertTrue(problems, "过期副本必须被报出来")
            self.assertTrue(any("core_bridge.py" in p for p in problems))

    def test_verify_flags_missing_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = self.install.verify(self._hermes_home(tmp))
            self.assertTrue(problems, "未部署时 verify 应报出问题")

    def test_verify_flags_wrong_core_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self._hermes_home(tmp)
            self.assertEqual(self.install.install(home), 0)
            (home / "plugins" / "winuse" / "_location.py").write_text(
                'PROJECT_ROOT = r"C:\\\\别的项目"\n', encoding="utf-8"
            )
            self.assertTrue(any("_location" in p for p in self.install.verify(home)))


if __name__ == "__main__":
    unittest.main()
