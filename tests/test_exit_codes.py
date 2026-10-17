"""R-16 + R-17：进程退出码必须参与成功判定，不能只看输出内容。

两个问题：
- **R-16**：`CuaDriver.version()` 直接从 stdout/stderr 里正则抓版本号，**不先看退出码**。
  驱动进程真失败（或输出被别的程序伪造）但恰好打印了合法版本号时，`health()` 仍报
  `ok=true`，doctor 会放行后续调用。
- **R-17**：`run_ocr()` 接住 runner 的 `returncode` 却不用——只要 stdout 里带成功标记
  就返回 `ok=true`。PowerShell 失败但留下部分输出时会被报告成"识别成功"。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from unittest import mock

import pytest

from winuse.core import ocr as ocr_mod
from winuse.driver.cua import CuaDriver
from winuse.driver.errors import DriverError


def _completed(code: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["fake"], returncode=code, stdout=stdout, stderr=stderr)


class TestDriverVersionExitCode:
    def test_nonzero_exit_is_refused_even_with_valid_version_text(self):
        driver = CuaDriver(command=["fake-driver"])
        with mock.patch.object(driver, "_run", return_value=_completed(2, stdout="cua-driver 9.9.9\n")):
            with pytest.raises(DriverError) as ctx:
                driver.version(refresh=True)
        assert "退出码" in str(ctx.value)

    def test_zero_exit_with_version_is_accepted(self):
        driver = CuaDriver(command=["fake-driver"])
        with mock.patch.object(driver, "_run", return_value=_completed(0, stdout="cua-driver 9.9.9\n")):
            assert driver.version(refresh=True) == (9, 9, 9)

    def test_nonzero_exit_without_version_is_still_refused(self):
        driver = CuaDriver(command=["fake-driver"])
        with mock.patch.object(driver, "_run", return_value=_completed(1, stderr="boom\n")):
            with pytest.raises(DriverError):
                driver.version(refresh=True)


@pytest.fixture()
def image_path():
    """只为过"文件存在"校验：假 runner 不会真读它。"""
    handle, path = tempfile.mkstemp(suffix=".png")
    os.close(handle)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestOcrExitCode:
    def _runner(self, code: int, stdout: str, stderr: str = ""):
        def run(_argv):
            return code, stdout, stderr

        return run

    def test_nonzero_exit_is_reported_as_run_failed(self, image_path):
        """stdout 里有成功标记也不认——退出码非零就是没跑成。"""
        ok_stdout = 'WINUSE_OCR_JSON:{"ok": true, "text": "发票号码"}\n'
        out = ocr_mod.run_ocr(
            ocr_mod.OcrSpec(image_path=image_path),
            runner=self._runner(1, ok_stdout, "Access is denied"),
            which=lambda exe: True,
        )
        assert out["ok"] is False
        assert out["code"] == ocr_mod.OcrRunFailed.code

    def test_zero_exit_but_missing_marker_is_engine_unavailable(self, image_path):
        """退出码为 0 但没有成功标记 → 引擎这一侧的问题（既有语义不变）。"""
        out = ocr_mod.run_ocr(
            ocr_mod.OcrSpec(image_path=image_path),
            runner=self._runner(0, "没有任何标记的输出\n"),
            which=lambda exe: True,
        )
        assert out["ok"] is False
        assert out["code"] == ocr_mod.OcrEngineUnavailable.code

    def test_zero_exit_with_valid_output_succeeds(self, image_path):
        stdout = (
            "WINUSE_OCR_OK\n"
            'WINUSE_OCR_JSON:{"ok": true, "text": "验收", "language": "zh-Hans",'
            ' "lines": [{"text": "验收", "frame": [0, 0, 10, 10]}]}\n'
        )
        out = ocr_mod.run_ocr(
            ocr_mod.OcrSpec(image_path=image_path),
            runner=self._runner(0, stdout),
            which=lambda exe: True,
        )
        assert out["ok"] is True
