"""cua-driver 调用封装 —— WinUse 唯一的 IO 边界。

设计约束（项目内部设计契约）：
- 驱动层只有 cua-driver 一套，**不装第二套驱动**（会撞焦点、撞坐标口径、
  绕过 Hermes 审批门禁）。
- 只走 CLI 子进程，**不起常驻 HTTP/SSE**（同样是为了不绕过审批门禁）。
- 零第三方依赖：只用标准库。

坐标口径提醒（务必遵守）：
- 驱动返回的坐标是**原生桌面坐标**（物理像素，虚拟桌面坐标系）。
- 换算 scale 必须**逐窗口取**（`screenshot_width ÷ 窗口 native 宽度`），
  绝不能用某次 capture 报的全局 `estimated scale` 去换算别的屏上的窗口。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .errors import (
    DriverError,
    DriverLaunchFailed,
    DriverNotFound,
    DriverTimeout,
    DriverVersionTooOld,
    WinUseError,
)

#: WinUse 要求的最低驱动版本（0.28.x 起 element_token / snapshot_id 语义稳定）
MIN_VERSION = (0, 28, 0)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

#: 自动探测候选（相对 home / LocalAppData）。
#:
#: ⚠️ **PATH 排在最后**，这是踩过的坑（2026-09-26）：PATH 里曾命中
#: `~/bin/cua-driver.exe`（0.6.8 旧副本），而 Hermes 实际在用
#: `~/.cua-driver/packages/current`（0.28.2）——先探测版本化安装目录，
#: 才不会被残留旧副本劫持。
CANDIDATE_SUFFIXES = (
    (".cua-driver", "packages", "current", "cua-driver.exe"),
    ("Programs", "Cua", "cua-driver", "bin", "cua-driver.exe"),
)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """从版本输出里取 (major, minor, patch)；取不到返回 None。"""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def check_min_version(version: Sequence[int], minimum: Sequence[int] = MIN_VERSION) -> bool:
    """version >= minimum（按 (major, minor, patch) 逐段比较）。"""
    v = tuple(int(x) for x in version)
    m = tuple(int(x) for x in minimum)
    return v >= m


def candidate_paths(home: Path | None = None, local_appdata: Path | None = None) -> list[Path]:
    """按优先级列出候选可执行文件路径（纯函数，便于单测）。"""
    home_dir = Path(home) if home is not None else Path.home()
    if local_appdata is not None:
        la = Path(local_appdata)
    else:
        la = Path(os.environ.get("LOCALAPPDATA") or (home_dir / "AppData" / "Local"))
    out: list[Path] = []
    for idx, suffix in enumerate(CANDIDATE_SUFFIXES):
        base = home_dir if idx == 0 else la
        out.append(base.joinpath(*suffix))
    return out


def _driver_label(cmd: Sequence[str]) -> str:
    """对外错误里的驱动标识：**只留文件名**，不带本机路径（R-10 数据卫生）。

    排查需要的只是"是哪个驱动"，而完整路径会暴露用户名与目录结构。
    """
    if not cmd:
        return "<空>"
    return os.path.basename(str(cmd[0])) or str(cmd[0])


@dataclass
class CuaDriver:
    """cua-driver 的 CLI 封装。

    command 留空 = 自动探测；测试时传入 `[sys.executable, "tests/fake_driver.py"]`
    即可完全不接触真实驱动与桌面。
    """

    command: Sequence[str] | None = None
    timeout_s: float = 120.0

    _resolved: list[str] | None = field(default=None, init=False, repr=False)
    _version: tuple[int, int, int] | None = field(default=None, init=False, repr=False)

    # ---------------------------------------------------------------- 解析
    def resolve(self) -> list[str]:
        """确定要执行的命令行（列表）。"""
        if self.command:
            return list(self.command)
        if self._resolved is not None:
            return self._resolved
        for path in candidate_paths():
            if path.is_file():
                self._resolved = [str(path)]
                return self._resolved
        found = shutil.which("cua-driver")
        if found:
            self._resolved = [found]
            return self._resolved
        raise DriverNotFound(
            "找不到 cua-driver",
            "已探测：" + "、".join(str(p) for p in candidate_paths()) + "，PATH 中也没有。"
            "请安装 cua-driver，或在 .env 里设置 WINUSE_CUA_DRIVER 指定绝对路径。",
        )

    # ---------------------------------------------------------------- 执行
    def _run(self, argv: Sequence[str], timeout_s: float | None = None) -> subprocess.CompletedProcess:
        cmd = self.resolve() + [str(a) for a in argv]
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # 不弹黑窗（插件/MCP 子进程调用时不打扰用户）
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(timeout_s if timeout_s is not None else self.timeout_s),
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise DriverTimeout(
                f"驱动调用超时（{timeout_s if timeout_s is not None else self.timeout_s}s）",
                f"驱动：{_driver_label(cmd)}；参数：{' '.join(str(a) for a in cmd[1:3])}",
            ) from exc
        except FileNotFoundError as exc:
            raise DriverNotFound(
                "驱动可执行文件无法启动（文件不存在）",
                f"驱动：{_driver_label(cmd)}",
            ) from exc
        except OSError as exc:
            # R-10：权限不足 / 路径过长 / 管道破裂等——原先这里没有兜底，
            # 异常会穿到 CLI 变成 traceback、到 MCP 变成"内部异常名 + 原始消息"
            raise DriverLaunchFailed(
                "驱动进程启动失败（OSError）",
                f"驱动：{_driver_label(cmd)}；原因：{type(exc).__name__}: {str(exc)[:120]}",
            ) from exc

    # ---------------------------------------------------------------- 解析输出
    @staticmethod
    def parse_output(stdout: str) -> Any:
        """把驱动 stdout 解析成对象。

        真实 CLI 会在 JSON 之前打 `WARN ...` 行（例如缓存未刷新），
        必须先剥掉，否则 json.loads 直接炸。
        """
        lines = [
            line
            for line in (stdout or "").splitlines()
            if line.strip() and not line.lstrip().startswith("WARN")
        ]
        text = "\n".join(lines).strip()
        if not text:
            raise DriverError("驱动没有输出任何内容", (stdout or "")[:400])
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DriverError("驱动输出不是合法 JSON", f"{exc}；原始输出：{text[:400]}") from exc

    # ---------------------------------------------------------------- 版本
    def version(self, refresh: bool = False) -> tuple[int, int, int]:
        if self._version is not None and not refresh:
            return self._version
        cp = self._run(["--version"], timeout_s=min(self.timeout_s, 30.0))
        # 先看退出码（R-16）：进程真失败、或输出被别的程序伪造时，也可能打印出
        # 一个合法版本号——不检查退出码就等于"谁能打印版本号谁就是健康驱动"
        if cp.returncode != 0:
            raise DriverError(
                f"驱动 --version 退出码非零（{cp.returncode}）",
                "输出：" + (((cp.stdout or "") + (cp.stderr or ""))[:200] or "<空>"),
            )
        parsed = parse_version((cp.stdout or "") + "\n" + (cp.stderr or ""))
        if parsed is None:
            raise DriverError(
                "无法解析驱动版本",
                "输出：" + ((cp.stdout or cp.stderr or "")[:200] or "<空>"),
            )
        self._version = parsed
        return parsed

    def version_str(self) -> str:
        return ".".join(str(x) for x in self.version())

    def ensure_ready(self) -> tuple[int, int, int]:
        """版本自检：低于 MIN_VERSION 直接拒绝（宁可不干活，也不在半残驱动上跑）。"""
        v = self.version()
        if not check_min_version(v):
            raise DriverVersionTooOld(
                f"驱动版本过低：{' '.join(str(x) for x in v)} < "
                f"{'.'.join(str(x) for x in MIN_VERSION)}",
                "请升级 cua-driver；旧版本的元素索引/坐标语义与 WinUse 不兼容。",
            )
        return v

    # ---------------------------------------------------------------- 业务
    def call(
        self,
        tool: str,
        args: dict | None = None,
        timeout_s: float | None = None,
        check_version: bool = True,
    ) -> Any:
        """调用一个驱动工具，返回解析后的对象。"""
        if check_version:
            self.ensure_ready()
        payload = json.dumps(args or {}, ensure_ascii=False)
        cp = self._run(["call", tool, payload], timeout_s=timeout_s)
        if cp.returncode != 0:
            raise DriverError(
                f"驱动调用失败：{tool}（退出码 {cp.returncode}）",
                ((cp.stderr or "") + (cp.stdout or ""))[:400],
            )
        return self.parse_output(cp.stdout)

    def health(self) -> dict:
        """健康检查：解析结果 + 版本 + 版本自检结论（不触发任何桌面动作）。"""
        out: dict[str, Any] = {"ok": False}
        try:
            out["command"] = self.resolve()
        except WinUseError as exc:
            out["error"] = exc.to_dict()
            return out
        try:
            out["version"] = self.version_str()
            out["min_version"] = ".".join(str(x) for x in MIN_VERSION)
            self.ensure_ready()
            out["ok"] = True
        except DriverVersionTooOld as exc:
            out["version"] = self.version_str()
            out["min_version"] = ".".join(str(x) for x in MIN_VERSION)
            out["error"] = exc.to_dict()
        except WinUseError as exc:
            # 含 DriverNotFound（显式指定的可执行文件不存在）——必须回收成报告，
            # 不能把原始异常抛给调用方（doctor 要给出可读结论）。
            out["error"] = exc.to_dict()
        return out
