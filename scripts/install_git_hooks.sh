#!/bin/sh
# 启用本仓库的 git 钩子（每台机器执行一次）。
#
# 为什么需要手工执行：`core.hooksPath` 是**本地仓库配置**，不随克隆传播。
# 不执行 = 数据卫生门禁不生效（这是踩过的坑：以为有门禁，其实没启用）。
set -e

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

if [ ! -d .githooks ]; then
  echo "找不到 .githooks 目录，请在仓库根目录执行。" >&2
  exit 1
fi

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "已启用 git 钩子：core.hooksPath = $(git config core.hooksPath)"
echo "验证：随意暂存一个含真实值的文件后尝试提交，应被 pre-commit 拦下。"
