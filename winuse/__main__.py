"""`python -m winuse` 入口。"""

import sys

from .cli import main
from .textio import force_utf8_streams

if __name__ == "__main__":
    # 编码口径先于一切：MCP 规范要求 UTF-8，Windows 空管道默认 cp936 → 中文必乱
    force_utf8_streams()
    sys.exit(main())
