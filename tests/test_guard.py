"""安全层测试：危险动作判定（PR-7）。

口径（用户 2026-09-26 批准）：危险动作清单 = kill_app / 系统电源类 / 注册表写 /
点到系统级窗口（任务栏、资源管理器等）/ 批量输入。
纪律：判定必须是**纯函数**（不碰系统），执行侧只负责"没确认就拒绝"。
"""

from __future__ import annotations

import pytest

from winuse.core import guard as G
from winuse.driver.errors import ConfirmRequired

NOTEPAD = {"pid": 100, "window_id": 11, "app_name": "notepad.exe", "title": "无标题 - 记事本"}
TASKBAR = {"pid": 200, "window_id": 22, "app_name": "explorer.exe", "title": "任务栏"}
SETTINGS = {"pid": 300, "window_id": 33, "app_name": "SystemSettings.exe", "title": "设置"}


class TestAssessAction:
    def test_plain_click_on_normal_app_is_safe(self):
        v = G.assess_action(action="click", target=NOTEPAD, element_index=0)
        assert v.dangerous is False
        assert v.code is None

    def test_click_on_taskbar_is_dangerous(self):
        v = G.assess_action(action="click", target=TASKBAR, element_index=3)
        assert v.dangerous is True
        assert v.code == "CONFIRM_REQUIRED"
        assert v.rule, "必须说明命中了哪条规则（可审计）"

    def test_close_window_action_is_dangerous(self):
        assert G.assess_action(action="close", target=None).dangerous is True

    def test_every_dangerous_action_name_is_actually_assessed(self):
        """防漂移守卫：危险清单是唯一出处——名单里的每个动作都必须真被判危险。"""
        for name in sorted(G.DANGEROUS_ACTIONS):
            v = G.assess_action(action=name, target=None)
            assert v.dangerous is True, f"{name} 在 DANGEROUS_ACTIONS 里却没被判危险"
            assert v.code == "CONFIRM_REQUIRED" and v.rule

    def test_typing_into_system_settings_is_dangerous(self):
        v = G.assess_action(action="type_text", target=SETTINGS)
        assert v.dangerous is True

    def test_normal_app_is_safe_regardless_of_action_kind(self):
        for action in ("set_value", "press_key", "scroll", "bring_to_front"):
            assert G.assess_action(action=action, target=NOTEPAD).dangerous is False

    def test_kill_app_action_is_dangerous(self):
        v = G.assess_action(action="kill_app", target=NOTEPAD)
        assert v.dangerous is True and v.code == "CONFIRM_REQUIRED"

    def test_missing_target_is_not_treated_as_dangerous(self):
        """选不到窗口是另一类错误（由选取层报），安全层不该越权替它判断。"""
        assert G.assess_action(action="click", target=None).dangerous is False


class TestAssessBatch:
    def test_any_batch_is_dangerous(self):
        v = G.assess_batch([{"action": "click"}, {"action": "type_text"}])
        assert v.dangerous is True
        assert v.code == "CONFIRM_REQUIRED"

    def test_batch_rule_mentions_count(self):
        v = G.assess_batch([{"action": "click"}] * 3)
        assert "3" in v.reason


class TestAssessPower:
    @pytest.mark.parametrize("action", ["shutdown", "restart", "sleep", "lock", "logoff"])
    def test_power_actions_are_dangerous(self, action):
        v = G.assess_power(action)
        assert v.dangerous is True and v.code == "CONFIRM_REQUIRED"

    def test_unknown_power_action_not_flagged_as_dangerous(self):
        assert G.assess_power("blink").dangerous is False


class TestAssessRegistry:
    def test_write_is_dangerous(self):
        v = G.assess_registry_write(hive="HKCU", path=r"Software\SomeApp", value_name="X")
        assert v.dangerous is True and v.code == "CONFIRM_REQUIRED"

    def test_read_is_safe(self):
        assert G.assess_registry_read(hive="HKCU", path=r"Software\SomeApp").dangerous is False

    def test_sensitive_paths_are_always_dangerous(self):
        for path in (r"SAM\SAM", r"SECURITY\Policy", r"SYSTEM\CurrentControlSet"):
            assert G.assess_registry_read(hive="HKLM", path=path).dangerous is True


class TestEnforcement:
    def test_require_confirm_raises_when_flag_missing(self):
        v = G.assess_batch([{"action": "click"}])
        with pytest.raises(ConfirmRequired) as exc:
            G.require_confirm(v, confirmed=False)
        assert exc.value.code == "CONFIRM_REQUIRED" or "CONFIRM" in str(exc.value)

    def test_require_confirm_passes_with_flag(self):
        v = G.assess_batch([{"action": "click"}])
        assert G.require_confirm(v, confirmed=True) is True

    def test_safe_verdict_never_raises(self):
        assert G.require_confirm(G.assess_action(action="click", target=NOTEPAD), confirmed=False) is False

    def test_verdict_to_dict_is_json_friendly(self):
        import json

        v = G.assess_action(action="click", target=TASKBAR)
        payload = v.to_dict()
        json.dumps(payload, ensure_ascii=False)
        assert payload["dangerous"] is True and payload["code"] == "CONFIRM_REQUIRED"

    def test_dangerous_actions_constant_covers_approved_list(self):
        assert {"kill_app"} <= set(G.DANGEROUS_ACTIONS)
        assert {"shutdown", "restart", "sleep", "lock", "logoff"} <= set(G.POWER_ACTIONS)
