"""把 WinUse 插件部署到 Hermes（幂等，可反复执行）。

用法：
    python adapters/hermes/install.py                 # 部署到默认 HERMES_HOME
    python adapters/hermes/install.py --home <HERMES_HOME>
    python adapters/hermes/install.py --home <HERMES_HOME> --verify   # 只校验，不改任何文件
    python adapters/hermes/install.py --uninstall

为什么源码在项目里、部署位只放副本：插件是**项目的产物**，要随项目 git 管理、
随项目分发；运行时目录里的副本随时可重建，不属于版本管理对象。

为什么还要两道门（各对应一次真实翻车）：

- **目标门**：目标写错时副本会装进**项目目录内**。踩过的形态是在 git-bash 里用反斜杠写
  ``--home <HERMES_HOME>``，反斜杠被 shell 吃掉、参数退化成裸目录名，于是落到
  ``<当前目录>/HermesAgent``——混进 git 视野且没有任何报错。判据只取两条确定的
  （是否落在项目内 / 像不像 Hermes 主目录），其余交给 ``--force``；过度聪明的启发式
  比不检查更糟。
- **一致性门**：副本是**静态文件**，源码改了不重跑就等于**静默过期**。此时运行中的
  agent 用的是旧翻译层，而工具表却由核心现读、看起来是新的——这种「schema 新、翻译层旧」
  的半新错位只能靠比对内容发现。部署后立刻比对，把它变成一次失败而不是一次疑惑。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

PLUGIN_NAME = "winuse"
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
#: 只复制这些（白名单比黑名单安全：不会把探针/临时文件带进部署位）
COPY_FILES = ("__init__.py", "core_bridge.py", "plugin.yaml", "README.md")
#: Hermes 主目录的标志物（小写比对；任一命中即认）。用来识别「目标像个 Hermes 家」。
HOME_MARKERS = frozenset({"config.yaml", "soul.md", ".env", "plugins", "skills", "state.db", "auth.json"})
#: 记录核心位置的文件名（install 写、verify 读：字面量只在这里出现一次）
LOCATION_FILE = "_location.py"


def location_text() -> str:
    """``_location.py`` 的期望内容（install 与 verify 共用，避免两处字面量漂移）。"""
    return (
        "# 由 install.py 生成：核心（业务逻辑唯一副本）所在项目根\n"
        f'PROJECT_ROOT = r"{PROJECT_ROOT}"\n'
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refuse_reason(home: Path) -> str | None:
    """目标合理性判据：返回拒绝理由；``None`` = 放行。

    只判两件确定的事，不猜用户意图：

    1. 目标落在**项目目录内**（含项目根本身）——副本装进项目一定错；
    2. 目标**已存在且非空**，却没有一个 Hermes 主目录标志物——那多半是别的目录。
       空目录放行（全新安装常是空目录），不存在也放行（首次创建）。
    """
    target = home.resolve()
    project = PROJECT_ROOT.resolve()
    if target == project or project in target.parents:
        return f"目标落在项目目录内：{target}（部署副本不该装进项目，通常是 --home 写错了）"
    if home.is_dir():
        names = {p.name.lower() for p in home.iterdir()}
        if names and not (names & HOME_MARKERS):
            return (
                f"{home} 看着不像 Hermes 主目录（既没有 config.yaml / .env，"
                f"也没有 plugins / skills 目录）"
            )
    return None


def verify(home: Path) -> list[str]:
    """比对部署副本与项目源，返回问题清单（空 = 一致）。**只读**，不修任何文件。

    逐文件比对内容而不是时间戳：时间戳在复制、git 检出、云盘同步下都不可靠。
    """
    dest = home / "plugins" / PLUGIN_NAME
    if not dest.is_dir():
        return [f"未部署：{dest} 不存在"]

    problems: list[str] = []
    for name in COPY_FILES:
        src = HERE / name
        if not src.is_file():
            continue  # 源缺失由 install 报告，不算副本的问题
        copy = dest / name
        if not copy.is_file():
            problems.append(f"缺少 {name}")
        elif _digest(copy) != _digest(src):
            problems.append(f"{name} 与项目源不一致（部署副本过期，重跑 install.py）")

    location = dest / LOCATION_FILE
    if not location.is_file():
        problems.append(f"缺少 {LOCATION_FILE}（核心位置未记录）")
    elif location.read_text(encoding="utf-8") != location_text():
        problems.append(f"{LOCATION_FILE} 记录的核心位置与当前项目不符")
    return problems


def resolve_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".hermes"


def install(home: Path, *, force: bool = False) -> int:
    reason = refuse_reason(home)
    if reason and not force:
        print(f"[x] 拒绝部署：{reason}", file=sys.stderr)
        print("    确认目标无误后加 --force 跳过这道检查。", file=sys.stderr)
        return 1

    dest = home / "plugins" / PLUGIN_NAME
    if not (PROJECT_ROOT / "winuse" / "__init__.py").is_file():
        print(f"[x] 项目核心不在预期位置：{PROJECT_ROOT}", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)

    for name in COPY_FILES:
        src = HERE / name
        if src.is_file():
            shutil.copy2(src, dest / name)
        else:
            print(f"[!] 缺少 {name}（跳过）", file=sys.stderr)

    # 部署时记录核心位置：部署副本可随时重建，不依赖环境变量
    (dest / LOCATION_FILE).write_text(location_text(), encoding="utf-8")

    # 清掉可能残留的字节码，避免旧代码被误用
    for cache in dest.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    # 部署后立刻比对内容：副本过期是静默故障，必须在同一步里变成一次失败
    problems = verify(home)
    if problems:
        print(f"[x] 已复制文件，但部署后自检未通过（{dest}）：", file=sys.stderr)
        for item in problems:
            print(f"    - {item}", file=sys.stderr)
        return 1

    print(f"[ok] 已部署到 {dest}")
    print(f"     核心位置：{PROJECT_ROOT}")
    print(f"     自检通过：{len(COPY_FILES)} 个文件与项目源逐字节一致")
    print("     下一步：hermes plugins enable winuse   # 插件默认 opt-in，不 enable 不会加载")
    return 0


def uninstall(home: Path, *, force: bool = False) -> int:
    dest = home / "plugins" / PLUGIN_NAME
    if not dest.exists():
        print(f"[i] 未安装：{dest}")
        return 0
    reason = refuse_reason(home)
    if reason and not force:
        # 删除同样要过目标门：删错目录的代价比装错更大
        print(f"[x] 拒绝卸载：{reason}", file=sys.stderr)
        print("    确认目标无误后加 --force 跳过这道检查。", file=sys.stderr)
        return 1
    shutil.rmtree(dest)
    print(f"[ok] 已移除 {dest}（记得 hermes plugins disable {PLUGIN_NAME}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="部署/校验/移除 WinUse Hermes 插件")
    ap.add_argument("--home", default=None, help="HERMES_HOME（默认取环境变量或 ~/.hermes）")
    ap.add_argument("--verify", action="store_true", help="只校验部署副本与项目源是否一致（不改文件）")
    ap.add_argument("--force", action="store_true", help="跳过目标合理性检查（目标确定无误时用）")
    ap.add_argument("--uninstall", action="store_true", help="移除部署副本")
    args = ap.parse_args()
    home = resolve_home(args.home)

    if args.verify:
        problems = verify(home)
        if problems:
            print(f"[x] 部署副本有问题（{home}）：")
            for item in problems:
                print(f"    - {item}")
            return 1
        print(f"[ok] 部署副本与项目源一致：{home / 'plugins' / PLUGIN_NAME}")
        return 0

    if args.uninstall:
        return uninstall(home, force=args.force)
    return install(home, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
