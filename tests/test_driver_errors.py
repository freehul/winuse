"""R-10：驱动 IO 边界是**唯一**的异常归一化点，且对外错误不带本机信息。

报告 R-10：
- `CuaDriver._run()` 只捕获 `TimeoutExpired` 与 `FileNotFoundError`——权限错误
  （`PermissionError`）、路径过长、管道破裂等其它 `OSError` 会原样上抛，
  CLI 直接 traceback，MCP 只能把它变成"内部异常名 + 原始消息"的通用错误。
- 错误 detail 里带着**完整命令行**（含本机绝对路径）——对外错误不该泄露机器信息。

本文件钉住：四类 IO 故障各有稳定错误码，且对外文本里出现不了本机路径。
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from winuse.driver.cua import CuaDriver
from winuse.driver.errors import (
    DriverError,
    DriverLaunchFailed,
    DriverNotFound,
    DriverTimeout,
)

#: 刻意写成像真的本机路径：如果它出现在对外错误里，就是泄露
LOCAL_PATH = "C:\\Users\\someone\\tools\\cua-driver.exe"


def _driver() -> CuaDriver:
    return CuaDriver(command=[LOCAL_PATH])


@pytest.mark.parametrize(
    "exc,expect",
    [
        (PermissionError(13, "Access is denied"), DriverLaunchFailed),
        (OSError(206, "文件名或扩展名太长"), DriverLaunchFailed),
        (FileNotFoundError(2, "no such file"), DriverNotFound),
        (subprocess.TimeoutExpired(cmd="cua-driver", timeout=1), DriverTimeout),
    ],
)
def test_io_failures_have_stable_codes(exc, expect):
    driver = _driver()
    with mock.patch.object(subprocess, "run", side_effect=exc):
        with pytest.raises(expect) as ctx:
            driver._run(["call", "list_windows", "{}"])
    assert ctx.value.code == expect.code


def test_launch_failure_message_does_not_leak_the_full_path():
    driver = _driver()
    with mock.patch.object(subprocess, "run", side_effect=PermissionError(13, "denied")):
        with pytest.raises(DriverLaunchFailed) as ctx:
            driver._run(["call", "list_windows", "{}"])
    blob = str(ctx.value) + str(ctx.value.to_dict())
    assert LOCAL_PATH not in blob, "对外错误不得带本机绝对路径"
    assert "cua-driver.exe" in blob, "但应保留可识别的驱动名，便于排查"


def test_timeout_message_does_not_leak_the_full_path():
    driver = _driver()
    with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
        with pytest.raises(DriverTimeout) as ctx:
            driver._run(["call", "list_windows", "{}"])
    assert LOCAL_PATH not in str(ctx.value)


def test_call_checks_returncode_before_parsing():
    """非零退出码 → 稳定错误码，绝不尝试解析它的输出。"""
    driver = _driver()
    fake = subprocess.CompletedProcess(args=["x"], returncode=3, stdout='{"ok": true}', stderr="boom")
    with mock.patch.object(driver, "_run", return_value=fake):
        with mock.patch.object(driver, "ensure_ready", return_value=(9, 9, 9)):
            with pytest.raises(DriverError) as ctx:
                driver.call("list_windows", {})
    assert "退出码" in str(ctx.value)


def test_json_parse_failure_is_normalized_and_truncated():
    driver = _driver()
    fake = subprocess.CompletedProcess(args=["x"], returncode=0, stdout="不是 JSON" * 200, stderr="")
    with mock.patch.object(driver, "_run", return_value=fake):
        with mock.patch.object(driver, "ensure_ready", return_value=(9, 9, 9)):
            with pytest.raises(DriverError) as ctx:
                driver.call("list_windows", {})
    payload = ctx.value.to_dict()
    assert payload["code"] == "DRIVER_ERROR"
    assert len(str(payload)) < 900, "原始输出必须截断，不能整段塞进对外错误"


class TestCliNeverTracesBack:
    """R-10 的最后一环：CLI 顶层兜底，意外异常也变结构化结论。"""

    def test_unexpected_exception_is_structured_not_a_traceback(self, monkeypatch, capsys):
        from winuse import cli

        def boom(*_a, **_k):
            raise RuntimeError(f"内部炸了：{LOCAL_PATH}")

        monkeypatch.setattr("winuse.config.Config.driver", boom, raising=True)
        code = cli.main(["act", "--do", "click", "--pid", "1", "--index", "0", "--snapshot", "s"])
        out = capsys.readouterr()
        assert code == cli.EXIT_FAIL
        assert "Traceback" not in out.err and "Traceback" not in out.out
        assert "INTERNAL_ERROR" in out.out
        assert LOCAL_PATH not in out.out, "对外文本不得带本机绝对路径"
