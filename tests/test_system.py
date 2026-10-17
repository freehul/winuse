"""winuse_system 单测：解析纯函数 + 注入桩的盘点 + 电源意图。

不碰真实机器：不发任何命令、不读真实系统信息。解析函数喂样例字符串，
`describe_system` 的 runner / power_reader 全部换成桩。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.core.system import (  # noqa: E402
    POWER_ACTIONS,
    POWER_NOTE,
    SystemSpec,
    SystemProbeError,
    as_records,
    decode_output,
    describe_system,
    normalize_power_status,
    parse_cpu_info,
    parse_disks,
    parse_json_payload,
    parse_os_info,
    parse_processes,
    plan_power_action,
    used_percent,
)
from winuse.driver.errors import UnknownAction  # noqa: E402

#: 样例输出（一律占位值，不含本机真实信息）
OS_JSON = (
    '{"Caption":"Microsoft Windows 11 Pro","Version":"10.0.22631","BuildNumber":"22631",'
    '"OSArchitecture":"64-bit","TotalVisibleMemorySize":"16777216","FreePhysicalMemory":"4194304"}'
)
CPU_JSON = (
    '[{"Name":"Sample CPU @ 2.50GHz","NumberOfCores":8,"NumberOfLogicalProcessors":16},'
    '{"Name":"Sample CPU @ 2.50GHz","NumberOfCores":8,"NumberOfLogicalProcessors":16}]'
)
DISKS_JSON = (
    '[{"DeviceID":"C:","VolumeName":"System","FileSystem":"NTFS","Size":"511000000000",'
    '"FreeSpace":"211000000000"},'
    '{"DeviceID":"D:","VolumeName":null,"FileSystem":"NTFS","Size":"1024000000000",'
    '"FreeSpace":null}]'
)
PROCS_JSON = (
    '[{"ProcessId":100,"Name":"small.exe","WorkingSetSize":"1048576"},'
    '{"ProcessId":200,"Name":"big.exe","WorkingSetSize":"2147483648"},'
    '{"ProcessId":300,"Name":"mid.exe","WorkingSetSize":"52428800"}]'
)


def _runner_factory(overrides=None, calls=None):
    """按脚本特征分发的桩 runner；overrides 可按分区注入异常/自定义输出。"""
    overrides = overrides or {}

    def runner(script: str) -> str:
        if calls is not None:
            calls.append(script)
        if script in overrides:
            value = overrides[script]
            if isinstance(value, Exception):
                raise value
            return value
        if "Win32_OperatingSystem" in script:
            return OS_JSON
        if "Win32_Processor" in script:
            return CPU_JSON
        if "LogicalDisk" in script:
            return DISKS_JSON
        if "Win32_Process" in script:
            return PROCS_JSON
        raise AssertionError(f"桩收到未预期的脚本：{script}")

    return runner


def _power_stub(**fields):
    def reader() -> dict:
        return normalize_power_status(
            fields.get("ac", 1), fields.get("flag", 128), fields.get("percent", 88)
        )

    return reader


class TestDecodeOutput(unittest.TestCase):
    def test_utf8_chinese(self):
        self.assertEqual(decode_output("内存总量".encode("utf-8")), "内存总量")

    def test_utf8_bom_is_stripped(self):
        self.assertEqual(decode_output("\ufeff{\"a\":1}".encode("utf-8")), '{"a":1}')

    def test_gbk_fallback(self):
        # 中文机器上控制台默认 GBK：UTF-8 解不开时必须回落到 GBK，而不是乱码
        self.assertEqual(decode_output("可用内存".encode("gbk")), "可用内存")

    def test_binary_garbage_does_not_raise(self):
        got = decode_output(b"\xff\xfe\x00\x01")
        self.assertIsInstance(got, str)

    def test_empty(self):
        self.assertEqual(decode_output(b""), "")
        self.assertEqual(decode_output(None), "")


class TestParseJsonPayload(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(parse_json_payload('{"a":1}'), {"a": 1})

    def test_plain_array(self):
        self.assertEqual(parse_json_payload("[1,2]"), [1, 2])

    def test_leading_warning_lines_are_tolerated(self):
        self.assertEqual(parse_json_payload('WRN 缓存可能未刷新\n{"a":1}'), {"a": 1})

    def test_leading_warning_lines_before_array(self):
        self.assertEqual(parse_json_payload('WRN 提示\n[{"a":1}]'), [{"a": 1}])

    def test_empty_output_raises_probe_error(self):
        with self.assertRaises(SystemProbeError) as ctx:
            parse_json_payload("   ")
        self.assertEqual(ctx.exception.code, "SYSTEM_PROBE_ERROR")

    def test_non_json_output_raises_probe_error(self):
        with self.assertRaises(SystemProbeError) as ctx:
            parse_json_payload("这不是 JSON")
        self.assertEqual(ctx.exception.code, "SYSTEM_PROBE_ERROR")


class TestSmallHelpers(unittest.TestCase):
    def test_as_records_wraps_single_object(self):
        self.assertEqual(as_records({"a": 1}), [{"a": 1}])
        self.assertEqual(as_records(None), [])
        self.assertEqual(as_records([1, 2]), [1, 2])

    def test_used_percent(self):
        self.assertEqual(used_percent(100, 25), 75.0)
        self.assertIsNone(used_percent(0, 0))
        self.assertIsNone(used_percent(None, 10))
        self.assertIsNone(used_percent(100, None))


class TestParseOsInfo(unittest.TestCase):
    def test_normal(self):
        got = parse_os_info(OS_JSON)
        self.assertEqual(got["caption"], "Microsoft Windows 11 Pro")
        self.assertEqual(got["build"], "22631")
        self.assertEqual(got["arch"], "64-bit")
        # CIM 报 KB，输出统一为字节
        self.assertEqual(got["memory_total_bytes"], 16777216 * 1024)
        self.assertEqual(got["memory_available_bytes"], 4194304 * 1024)

    def test_single_object_is_accepted(self):
        got = parse_os_info('{"Caption":"Sample OS","TotalVisibleMemorySize":1024}')
        self.assertEqual(got["caption"], "Sample OS")
        self.assertEqual(got["memory_total_bytes"], 1048576)

    def test_missing_fields_become_none_not_zero(self):
        got = parse_os_info('{"Caption":"Sample OS"}')
        self.assertIsNone(got["build"])
        self.assertIsNone(got["memory_total_bytes"])

    def test_empty_array_raises(self):
        with self.assertRaises(SystemProbeError):
            parse_os_info("[]")


class TestParseCpuInfo(unittest.TestCase):
    def test_multi_socket_is_summed(self):
        got = parse_cpu_info(CPU_JSON)
        self.assertEqual(got["physical_cores"], 16)
        self.assertEqual(got["logical_cores"], 32)
        self.assertEqual(got["socket_count"], 2)

    def test_single_object(self):
        got = parse_cpu_info('{"Name":"Sample CPU","NumberOfLogicalProcessors":"16"}')
        self.assertEqual(got["logical_cores"], 16)
        self.assertIsNone(got["physical_cores"])

    def test_missing_logical_cores(self):
        got = parse_cpu_info('[{"Name":"Sample CPU"}]')
        self.assertIsNone(got["logical_cores"])
        self.assertIsNone(got["physical_cores"])


class TestParseDisks(unittest.TestCase):
    def test_normal(self):
        got = parse_disks(DISKS_JSON)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["drive"], "C:")
        self.assertEqual(got[0]["used_bytes"], 511000000000 - 211000000000)
        self.assertEqual(got[0]["used_percent"], 58.7)
        self.assertIsNone(got[1]["used_percent"])  # 可用空间未知 → 占比未知，不编 0/100

    def test_null_free_space_from_cim(self):
        got = parse_disks('[{"DeviceID":"X:","Size":"1000","FreeSpace":null}]')
        self.assertIsNone(got[0]["free_bytes"])
        self.assertIsNone(got[0]["used_bytes"])
        self.assertIsNone(got[0]["used_percent"])

    def test_zero_size_does_not_divide_by_zero(self):
        got = parse_disks('[{"DeviceID":"Z:","Size":"0","FreeSpace":"0"}]')
        self.assertIsNone(got[0]["used_percent"])

    def test_non_dict_records_are_skipped(self):
        self.assertEqual(parse_disks('[1, "x", {"DeviceID":"C:"}]')[0]["drive"], "C:")


class TestParseProcesses(unittest.TestCase):
    def test_sorted_by_memory_desc_and_capped(self):
        got = parse_processes(PROCS_JSON, top_n=2)
        self.assertEqual([p["name"] for p in got], ["big.exe", "mid.exe"])
        self.assertEqual(got[0]["memory_mib"], 2048.0)

    def test_top_zero_returns_empty(self):
        self.assertEqual(parse_processes(PROCS_JSON, top_n=0), [])

    def test_single_object_is_accepted(self):
        got = parse_processes('{"ProcessId":7,"Name":"one.exe","WorkingSetSize":"1048576"}')
        self.assertEqual(got[0]["pid"], 7)
        self.assertEqual(got[0]["memory_mib"], 1.0)

    def test_missing_name_and_memory(self):
        got = parse_processes('[{"ProcessId":8}]')
        self.assertIsNone(got[0]["name"])
        self.assertIsNone(got[0]["memory_mib"])

    def test_missing_memory_sorts_last(self):
        got = parse_processes(
            '[{"ProcessId":1,"Name":"nok","WorkingSetSize":null},'
            '{"ProcessId":2,"Name":"has","WorkingSetSize":"100"}]'
        )
        self.assertEqual([p["name"] for p in got], ["has", "nok"])


class TestNormalizePowerStatus(unittest.TestCase):
    def test_on_ac(self):
        self.assertTrue(normalize_power_status(1, 8, 88)["on_ac"])

    def test_on_battery(self):
        self.assertFalse(normalize_power_status(0, 1, 42)["on_ac"])

    def test_unknown_ac_status_is_none(self):
        self.assertIsNone(normalize_power_status(255, 1, 42)["on_ac"])

    def test_battery_flags_with_a_real_battery(self):
        # 1=电量高、8=充电中：都表示"有电池"
        self.assertTrue(normalize_power_status(1, 1, 90)["battery_present"])
        self.assertTrue(normalize_power_status(1, 8, 50)["battery_present"])

    def test_no_system_battery_flag_128(self):
        # MSDN：BatteryFlag=128 是"本机没有系统电池"（台式机实测就是这个），不是"有电池"
        got = normalize_power_status(1, 128, 255)
        self.assertFalse(got["battery_present"])
        self.assertIsNone(got["charge_percent"])

    def test_unreadable_battery_flag_is_none_not_false(self):
        for flag in (255, 0, None):
            with self.subTest(flag=flag):
                self.assertIsNone(normalize_power_status(1, flag, 50)["battery_present"])

    def test_unknown_percent_is_none(self):
        self.assertIsNone(normalize_power_status(1, 1, 255)["charge_percent"])

    def test_missing_inputs_do_not_crash(self):
        got = normalize_power_status()
        self.assertIsNone(got["on_ac"])
        self.assertIsNone(got["battery_present"])
        self.assertIsNone(got["charge_percent"])


class TestDescribeSystem(unittest.TestCase):
    def test_full_probe_with_stubs(self):
        got = describe_system(SystemSpec(top_processes=3), runner=_runner_factory(), power_reader=_power_stub())
        self.assertTrue(got["ok"])
        self.assertFalse(got["partial"])
        self.assertEqual(got["errors"], [])
        self.assertEqual(got["os"]["build"], "22631")
        self.assertEqual(got["cpu"]["logical_cores"], 32)
        self.assertEqual(got["memory"]["total_bytes"], 16777216 * 1024)
        self.assertEqual(got["memory"]["used_percent"], 75.0)
        self.assertEqual(len(got["disks"]), 2)
        self.assertEqual(got["top_n"], 3)
        self.assertLessEqual(len(got["processes_top"]), 3)
        self.assertTrue(got["power"]["on_ac"])
        self.assertIn("plan_power_action", got["note"])

    def test_section_failure_is_recorded_and_others_survive(self):
        calls: list[str] = []

        def runner(script: str) -> str:
            if "LogicalDisk" in script:
                raise SystemProbeError("PowerShell 退出码 1", "样例 stderr")
            return _runner_factory()(script)

        got = describe_system(SystemSpec(), runner=runner, power_reader=_power_stub())
        self.assertTrue(got["ok"])
        self.assertTrue(got["partial"])
        self.assertEqual(got["disks"], [])
        self.assertEqual([e["section"] for e in got["errors"]], ["disks"])
        self.assertEqual(got["errors"][0]["code"], "SYSTEM_PROBE_ERROR")
        self.assertIsNotNone(got["os"])

    def test_unexpected_exception_is_normalized(self):
        def runner(script: str) -> str:
            raise ValueError("样例异常")

        got = describe_system(SystemSpec(), runner=runner, power_reader=_power_stub())
        self.assertTrue(got["partial"])
        self.assertEqual(got["errors"][0]["code"], "UNEXPECTED")
        self.assertIsNone(got["os"])

    def test_power_reader_failure_recorded(self):
        def bad_power() -> dict:
            raise SystemProbeError("原生调用失败", "样例")

        got = describe_system(SystemSpec(), runner=_runner_factory(), power_reader=bad_power)
        self.assertIsNone(got["power"])
        self.assertEqual([e["section"] for e in got["errors"]], ["power"])

    def test_top_processes_zero_skips_process_command(self):
        calls: list[str] = []
        got = describe_system(
            SystemSpec(top_processes=0),
            runner=_runner_factory(calls=calls),
            power_reader=_power_stub(),
        )
        self.assertEqual(got["processes_top"], [])
        # 注意别用 "Win32_Process" 做标记——"Win32_Processor" 也含这个子串
        self.assertFalse(any("WorkingSetSize" in script for script in calls))

    def test_top_processes_cap(self):
        got = describe_system(
            SystemSpec(top_processes=9999), runner=_runner_factory(), power_reader=_power_stub()
        )
        self.assertEqual(got["top_n"], 200)

    def test_default_spec(self):
        got = describe_system(runner=_runner_factory(), power_reader=_power_stub())
        self.assertEqual(got["top_n"], 15)


class TestPlanPowerAction(unittest.TestCase):
    EXPECTED = {
        "shutdown": ["shutdown", "/s", "/t", "60"],
        "restart": ["shutdown", "/r", "/t", "60"],
        "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
        "logoff": ["shutdown", "/l"],
    }

    def test_each_action_returns_intent_only(self):
        for action, argv in self.EXPECTED.items():
            with self.subTest(action=action):
                got = plan_power_action(action)
                self.assertEqual(got["ok"], True)
                self.assertEqual(got["requires_confirm"], True)
                self.assertEqual(got["intent"], f"power.{action}")
                self.assertEqual(got["would_run"], argv)
                self.assertEqual(got["note"], POWER_NOTE)
                self.assertEqual(
                    sorted(got.keys()), ["intent", "note", "ok", "requires_confirm", "would_run"]
                )

    def test_action_name_is_case_and_space_insensitive(self):
        self.assertEqual(plan_power_action("  SHUTDOWN "), plan_power_action("shutdown"))

    def test_unknown_action_raises_with_contract_code(self):
        with self.assertRaises(UnknownAction) as ctx:
            plan_power_action("reboot-now")
        self.assertEqual(ctx.exception.code, "UNKNOWN_ACTION")
        self.assertIn("shutdown", ctx.exception.detail)

    def test_empty_action_raises(self):
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                with self.assertRaises(UnknownAction):
                    plan_power_action(bad)  # type: ignore[arg-type]

    def test_nothing_is_executed(self):
        # plan_power_action 连 runner 都不接：用打桩的 subprocess.run 证明无人调用
        with mock.patch("winuse.core.system.subprocess.run") as run:
            plan_power_action("shutdown")
            plan_power_action("logoff")
        run.assert_not_called()

    def test_returned_would_run_is_a_copy(self):
        first = plan_power_action("shutdown")
        first["would_run"].append("--被改过")
        self.assertEqual(plan_power_action("shutdown")["would_run"], self.EXPECTED["shutdown"])
        self.assertEqual(POWER_ACTIONS["shutdown"]["would_run"], self.EXPECTED["shutdown"])


class TestRetryAndTimeout(unittest.TestCase):
    """命令级失败（超时/退出码非 0）重试：偶发 WMI 卡顿不该让整段数据消失。"""

    def test_transient_command_failure_is_retried_once(self):
        state = {"n": 0}

        def runner(script: str) -> str:
            if "LogicalDisk" in script:
                state["n"] += 1
                if state["n"] == 1:
                    raise SystemProbeError("PowerShell 超时（>30s）", "样例：WMI 卡顿")
            return _runner_factory()(script)

        got = describe_system(SystemSpec(), runner=runner, power_reader=_power_stub())
        self.assertEqual(state["n"], 2)
        self.assertFalse(got["partial"])
        self.assertEqual(len(got["disks"]), 2)

    def test_retries_zero_stops_after_one_attempt(self):
        calls: list[str] = []

        def runner(script: str) -> str:
            if "LogicalDisk" in script:
                calls.append(script)
                raise SystemProbeError("PowerShell 超时（>30s）", "样例")
            return _runner_factory()(script)

        got = describe_system(SystemSpec(retries=0), runner=runner, power_reader=_power_stub())
        self.assertEqual(len(calls), 1)
        self.assertEqual([e["section"] for e in got["errors"]], ["disks"])

    def test_persistent_failure_is_reported_not_hidden(self):
        calls: list[str] = []

        def runner(script: str) -> str:
            if "LogicalDisk" in script:
                calls.append(script)
                raise SystemProbeError("PowerShell 退出码 1", "样例")
            return _runner_factory()(script)

        got = describe_system(SystemSpec(retries=2), runner=runner, power_reader=_power_stub())
        self.assertEqual(len(calls), 3)
        self.assertEqual([e["section"] for e in got["errors"]], ["disks"])
        self.assertEqual(got["errors"][0]["code"], "SYSTEM_PROBE_ERROR")

    def test_default_runner_receives_the_configured_timeout(self):
        with mock.patch("winuse.core.system._powershell", return_value="[]") as probe:
            describe_system(SystemSpec(timeout_seconds=7.5, retries=0), power_reader=_power_stub())
        timeouts = {call.kwargs.get("timeout") for call in probe.call_args_list}
        self.assertEqual(timeouts, {7.5})


if __name__ == "__main__":
    unittest.main(verbosity=2)
