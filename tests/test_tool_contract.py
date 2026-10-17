"""R-4：工具表（schema）与核心契约的**自动对账**。

审查报告发现三处漂移（客户端照 `tools/list` 生成调用会稳定失败）：

| 工具 | 对外枚举 | 核心实际接受 |
|---|---|---|
| winuse_act | type_text / press_key / bring_to_front / focus | type / key / activate |
| winuse_window | move / resize / topmost | move_resize / always_on_top / activate |
| winuse_registry | hive 描述里有 HKCC | HKCR / HKCU / HKLM / HKU |

本文件的立场：**动作名与枚举只有一个来源**（`core.act.ACTION_TOOLS`、
`core.desktop.WINDOW_ACTIONS`、`core.registry.HIVES`），工具表由它们生成——
所以这里不做"人工核对清单"，而是逐项断言"schema 可枚举值 == 核心可执行值"。
核心哪天加/删一个名字，这些用例会立刻跟着变，不需要有人记得回来改文档。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from winuse import cli
from winuse.core.act import ACTION_TOOLS
from winuse.core.desktop import WINDOW_ACTIONS
from winuse.core.registry import HIVES
from winuse.mcp import server as S


def _tool(name: str) -> dict:
    found = [t for t in S.tool_catalog() if t["name"] == name]
    assert found, f"工具表里没有 {name}"
    return found[0]


def _enum_of(tool_name: str, field: str) -> list[str]:
    schema = _tool(tool_name)["inputSchema"]
    return schema["properties"][field]["enum"]


# ------------------------------------------------------------------ act


def test_act_enum_equals_core_action_keys():
    assert _enum_of("winuse_act", "action") == sorted(ACTION_TOOLS), (
        "winuse_act 的 action 枚举必须与 core.act.ACTION_TOOLS 完全一致"
    )


def test_act_enum_contains_no_ghost_names():
    """历史漂移名不许回来（它们会让客户端调用稳定失败）。"""
    ghosts = {"type_text", "press_key", "bring_to_front", "focus", "topmost", "move", "resize"}
    assert not (set(_enum_of("winuse_act", "action")) & ghosts)


def test_act_description_names_are_all_executable():
    """描述里提到的动作名必须是核心真认识的（描述也是契约的一部分）。"""
    description = _tool("winuse_act")["description"]
    for ghost in ("type_text", "press_key", "bring_to_front"):
        assert ghost not in description, f"描述里还在教用户用已不存在的动作名：{ghost}"


def test_act_text_and_key_descriptions_match_action_names():
    props = _tool("winuse_act")["inputSchema"]["properties"]
    assert "type" in props["text"]["description"], "text 参数应注明给 type 用"
    assert "key" in props["key"]["description"], "key 参数应注明给 key 动作用"


# --------------------------------------------------------------- window


def test_window_enum_equals_core_window_actions():
    assert set(_enum_of("winuse_window", "action")) == set(WINDOW_ACTIONS)


# ------------------------------------------------------------- registry


def test_registry_hive_enum_equals_core_hives():
    schema = _tool("winuse_registry")["inputSchema"]["properties"]["hive"]
    hives = schema.get("enum")
    assert hives is not None, "hive 应当给出 enum（否则客户端只能靠猜）"
    assert set(hives) == set(HIVES), "hive 枚举必须与 core.registry.HIVES 一致（HKCC 不在其中）"
    assert "HKCC" not in schema["description"], "描述里不该再出现核心不接受的根键"


def test_registry_read_description_mentions_sensitive_gate():
    description = _tool("winuse_registry")["description"]
    assert "敏感" in description or "SAM" in description


# ------------------------------------------------------------------ CLI


def test_cli_act_choices_come_from_core():
    """CLI 侧同样不另立一份动作名清单。"""
    parser = cli.build_parser()
    action = next(a for a in parser._subparsers._group_actions[0].choices["act"]._actions
                  if a.dest == "do")
    if action.choices is not None:
        assert set(action.choices) == set(ACTION_TOOLS)
    else:
        assert "、".join(sorted(ACTION_TOOLS)) in action.help


# ------------------------------------------------------------- Hermes 桥接


@pytest.fixture()
def bridge():
    path = Path(__file__).resolve().parents[1] / "adapters" / "hermes" / "core_bridge.py"
    spec = importlib.util.spec_from_file_location("winuse_core_bridge_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("action", sorted(ACTION_TOOLS))
def test_bridge_translates_every_core_action(bridge, action):
    """桥接必须能把工具表里的每个动作翻成 CLI 参数（否则那条枚举是假的）。"""
    argv = bridge.build_args("winuse_act", {"action": action, "element_token": "s1:1"})
    assert argv[:1] == ["act"]
    assert argv[argv.index("--do") + 1] == action


def test_bridge_multi_steps_description_example_is_executable():
    """multi 步骤示例里的动作名也要是核心认识的。"""
    description = _tool("winuse_multi")["description"]
    assert "批量输入一律算危险动作" in description
