"""假驱动：模拟 cua-driver CLI 的输出形态，供单测使用。

为什么要有它：真实驱动会真的读写桌面，单测必须能在**完全不接触桌面**的
前提下验证封装层（输出解析、错误归一化、超时、版本门禁）。

调用方式（与真实 CLI 同形）：
    python tests/fake_driver.py --version
    python tests/fake_driver.py call <tool> '<json>'
"""

from __future__ import annotations

import json
import sys
import time

FAKE_VERSION = "0.28.2"


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(f"cua-driver {FAKE_VERSION}")
        return 0

    if len(argv) >= 3 and argv[1] == "call":
        tool = argv[2]
        raw = argv[3] if len(argv) > 3 else "{}"

        if tool == "boom":
            print("驱动内部错误：模拟失败", file=sys.stderr)
            return 2

        if tool == "garbage":
            print("WARN 缓存未刷新，以下内容不是 JSON")
            print("这不是 JSON")
            return 0

        if tool == "slow":
            time.sleep(3)
            print(json.dumps({"tool": tool, "slept": 3}))
            return 0

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print("参数不是 JSON", file=sys.stderr)
            return 64

        # 真实 CLI 会在 JSON 之前打 WARN 行，专门验证这里能被正确剥掉
        print("WARN 模拟提示：缓存可能未刷新")
        print(json.dumps({"tool": tool, "args": payload, "ok": True}, ensure_ascii=False))
        return 0

    print("usage: fake_driver [--version | call <tool> <json>]", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
