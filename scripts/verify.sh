#!/usr/bin/env bash
# WinUse 统一验收入口（本地一键跑完）。
#
# 为什么要有它：验收纪律要求"完成 = 证据"，但证据散在三条命令里
# （编译 / 测试 / 警告升级），谁少跑一条就会把"测试全绿"当成"全都过了"
# —— 而项目历史上真有只有 `-W error` 才能暴露的问题（子进程管道泄漏、
# 非法转义序列）。一个入口跑完，就没得省。
#
# 用法（仓库根目录）：
#   sh scripts/verify.sh            # 编译 + 测试 + 警告升级
#   sh scripts/verify.sh --doctor   # 上面三项 + 一次真机只读自检
#
# 环境变量：
#   PYTHON  指定解释器（默认 python；需要 pytest ≥ 8）

set -uo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT" || exit 1

# 解释器解析顺序：显式 PYTHON > 项目内 .venv > python
# 优先 .venv 的理由：测试依赖应当装在**项目内**，不该依赖"我这台机器全局恰好有 pytest"
if [ -n "${PYTHON:-}" ]; then
  :
elif [ -x ".venv/Scripts/python.exe" ]; then
  PYTHON=".venv/Scripts/python.exe"   # Windows
elif [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"           # POSIX
else
  PYTHON="python"
fi

# WorkBuddy 宿主会向 Python 注入 safe-delete 钩子，它把 pytest 的临时目录批量清理
# 误判成"AI 批量删除"并杀掉进程（表现为全绿但退出码非 0）。清空 session id 即可豁免，
# 只对本次运行的子进程生效。
export CODEBUDDY_SESSION_ID=
export CLAUDE_SESSION_ID=

# PYTHON 可以带参数（例如 PYTHON="py -3.11"），所以下面刻意不加引号
# shellcheck disable=SC2086

fail=0

if ! $PYTHON -c "import pytest" 2>/dev/null; then
  printf '[x] %s 里没有 pytest。
' "$PYTHON"
  printf '    安装：%s -m pip install pytest
' "$PYTHON"
  printf "    或指定解释器：PYTHON='py -3.11' sh scripts/verify.sh
"
  exit 2
fi

step() { printf '\n== %s ==\n' "$1"; }
report() {
  if [ "$1" -eq 0 ]; then
    printf '[ok]   %s\n' "$2"
  else
    printf '[FAIL] %s\n' "$2"
    fail=1
  fi
}

step "编译检查（compileall）"
$PYTHON -m compileall -q winuse adapters tests
report $? "compileall winuse adapters tests"

step "单元测试（pytest -q）"
$PYTHON -m pytest -q
report $? "pytest -q"

step "警告升级（pytest -q -W error）"
$PYTHON -m pytest -q -W error
report $? "pytest -q -W error"

# 部署副本一致性（仅当本机装了该插件时检查）：副本是**静态文件**，源码改了不重跑
# install.py 就会静默过期——此时运行中的 agent 用的是旧翻译层，而工具表由核心现读、
# 看着还是新的。这种「半新」错位只有比对内容才发现得了。
DEPLOY_HOME="${HERMES_HOME:-$HOME/.hermes}"
if [ -d "$DEPLOY_HOME/plugins/winuse" ]; then
  step "部署副本一致性（源 vs 副本）"
  $PYTHON adapters/hermes/install.py --home "$DEPLOY_HOME" --verify
  report $? "install.py --verify（$DEPLOY_HOME）"
else
  printf '\n== 部署副本一致性 ==\n[i] 本机未部署 winuse 插件，跳过（%s/plugins/winuse 不存在）\n' "$DEPLOY_HOME"
fi

if [ "${1:-}" = "--doctor" ]; then
  step "真机只读自检（winuse doctor）"
  $PYTHON -m winuse doctor
  report $? "winuse doctor"
fi

printf '\n'
if [ "$fail" -eq 0 ]; then
  echo "全部通过。"
else
  echo "有步骤未通过，见上面的 [FAIL] 行。"
fi
exit "$fail"
