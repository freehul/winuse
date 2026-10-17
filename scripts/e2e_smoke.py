"""端到端冒烟：真机上验证「取状态 → 元素索引动作 → 回读校验」的闭环（PR-3 验收脚本）。

设计原则：
- **不碰用户正在用的应用，也不借第三方应用当"隔离窗口"**：本脚本自己用 PowerShell +
  WinForms 起一个**一次性测试窗口**（自带一个文本框），跑完就结束进程。
  教训（2026-09-26 真实事故）：曾借记事本当隔离窗口，而 Windows 11 记事本会**恢复上次会话**，
  驱动新起的实例直接打开了用户上次的文件标签；`set_value` 在 Document 上是**整段替换**，
  于是覆盖了用户的编辑器缓冲区（磁盘文件未动）。**第三方应用不能当隔离窗口。**
- **启动不靠驱动**：自己 `subprocess` 起窗口（`-WindowStyle Hidden` 隐藏控制台窗口，
  窗体不 Activate → 不抢前台焦点），随后按 pid 让驱动做元素级动作。
- **只做可逆动作**：往自建文本框写一段探针文字，不落盘、不动任何用户文件。
- **写入前门禁**：目标元素非空就拒绝写入（自建窗口本应为空；留下这道闸防回归）。
- **必须回读校验**：动作返回成功 ≠ 生效，重新取状态比对值才算通过。

用法（仓库根目录）：
    python scripts/e2e_smoke.py                       # 完整跑
    python scripts/e2e_smoke.py --keep                # 结束后不关窗口（排查用）
    python scripts/e2e_smoke.py --report out.json     # 结构化报告存文件
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winuse.config import Config  # noqa: E402
from winuse.core.act import ActSpec, run_act  # noqa: E402
from winuse.core.state import StateOptions, build_state  # noqa: E402

PROBE_TEXT = "WinUse E2E 探针 · 元素索引写值"
WINDOW_TITLE = "WinUseE2E Probe"

# 纯 ASCII：避免 Windows PowerShell 按 ANSI 读脚本、把中文注释当命令执行
# （见 windows-toolchain-pitfalls 第 22 条）
PS_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = 'WinUseE2E Probe'
$form.ClientSize = New-Object System.Drawing.Size(420, 160)
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(60, 60)
$box = New-Object System.Windows.Forms.TextBox
$box.Name = 'WinUseProbeBox'
$box.AccessibleName = 'WinUseProbeBox'
$box.Multiline = $true
$box.Size = New-Object System.Drawing.Size(380, 100)
$box.Location = New-Object System.Drawing.Point(15, 20)
$box.Text = ''
$form.Controls.Add($box)
[void]$form.Show()
[System.Windows.Forms.Application]::Run($form)
"""


def launch_test_window() -> subprocess.Popen:
    """起一次性 WinForms 测试窗口（自己起，不经驱动；不抢焦点、不碰用户文件）。

    用 `-EncodedCommand`（UTF-16LE + base64）传脚本：绕开 .ps1 文件的编码/换行坑。
    """
    encoded = base64.b64encode(PS_SCRIPT.encode("utf-16-le")).decode("ascii")
    return subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-EncodedCommand",
            encoded,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_window(driver, pid: int, timeout_s: float = 25.0) -> dict | None:
    """轮询窗口清单，等自建窗口出现在驱动视野里。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            payload = driver.call("list_windows", {}) or {}
        except Exception:  # noqa: BLE001 - 驱动尚未就绪时继续轮询
            time.sleep(0.5)
            continue
        windows = payload.get("windows") if isinstance(payload, dict) else payload
        for w in windows or []:
            if int(w.get("pid") or -1) == int(pid) and WINDOW_TITLE in str(w.get("title") or ""):
                return w
        time.sleep(0.5)
    return None


def find_editable(elements: list[dict]) -> dict | None:
    """找一个能写值的元素（优先 Edit，其次任何暴露 set_value 的元素）。"""
    for e in elements:
        if e.get("role") == "Edit" and "set_value" in (e.get("actions") or []):
            return e
    for e in elements:
        if "set_value" in (e.get("actions") or []):
            return e
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="结束后保留测试窗口")
    ap.add_argument("--report", default=None, help="把结构化报告写到该路径")
    args = ap.parse_args()

    cfg = Config.load()
    driver = cfg.driver()
    report: dict = {"steps": [], "ok": False, "window": {"title": WINDOW_TITLE}}
    proc = None
    pid = None

    def step(label: str, **fields) -> None:
        report["steps"].append({"step": label, **fields})

    try:
        proc = launch_test_window()
        pid = proc.pid
        step("launch_test_window", pid=pid, how="powershell -EncodedCommand (WinForms)")
        window = wait_for_window(driver, pid)
        if window is None:
            step("abort", reason="自建测试窗口未在驱动视野里出现（超时）")
            _finish(report, args)
            return 1
        step(
            "window_visible",
            window_id=window.get("window_id"),
            title=window.get("title"),
            minimized=window.get("minimized"),
            is_on_screen=window.get("is_on_screen"),
        )

        # 1) 取状态（纯文本路径，不下发截图）
        st = build_state(driver, StateOptions(pid=pid, max_elements=200, include_windows=False))
        step(
            "state",
            snapshot_id=st.get("snapshot_id"),
            elements=len(st["elements"]),
            driver_elements=st["stats"].get("driver_element_count"),
        )
        target = find_editable(st["elements"])
        if not target:
            step("abort", reason="窗口里找不到可写元素", roles=[e["role"] for e in st["elements"]][:12])
            _finish(report, args)
            return 1
        step("pick_element", role=target["role"], index=target["index"], actions=target["actions"])

        # 1.5) 写入前门禁：目标元素必须是空的
        # （曾经的记事本事故就是覆盖了非空缓冲区；留这道闸防回归）
        pre = target.get("value") or ""
        step("pre_check", element_empty=(pre.strip() == ""), existing_chars=len(pre))
        if pre.strip():
            step("abort", reason="目标元素非空，拒绝写入（避免覆盖用户正在编辑的内容）")
            _finish(report, args)
            return 1

        ctx = {"pid": pid, "window_id": st["target"]["window_id"]}
        spec = ActSpec(action="set_value", element_index=target["index"], value=PROBE_TEXT)

        # 2) 干跑：先看将要下发的调用长什么样（默认行为，不碰桌面）
        dry = run_act(driver, spec, ctx, st["snapshot_id"], execute=False)
        step("dry_run", would_call=dry["would_call"], ok=dry["ok"])

        # 3) 真动作
        res = run_act(driver, spec, ctx, st["snapshot_id"], execute=True)
        step("act", ok=res["ok"], error=res.get("error"))

        # 4) 回读校验：动作"成功"不等于生效
        st2 = build_state(driver, StateOptions(pid=pid, max_elements=200, include_windows=False))
        after = next((e for e in st2["elements"] if e["index"] == target["index"]), None)
        got = (after or {}).get("value")
        step("verify", expected=PROBE_TEXT, got=got, match=(got == PROBE_TEXT))
        report["ok"] = bool(res["ok"] and got == PROBE_TEXT)
    finally:
        if proc is not None and not args.keep:
            try:
                proc.terminate()
                proc.wait(timeout=10)
                step("close_test_window", pid=pid, method="terminate")
            except Exception as exc:  # noqa: BLE001
                step("close_test_window_failed", error=str(exc)[:120])
                try:
                    driver.call("kill_app", {"pid": pid})
                    step("close_test_window", pid=pid, method="driver kill_app")
                except Exception as exc2:  # noqa: BLE001
                    step("close_test_window_failed", error=str(exc2)[:120])

    _finish(report, args)
    return 0 if report["ok"] else 1


def _finish(report, args) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
