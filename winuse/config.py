"""WinUse 配置：**只从项目内 .env 读取，刻意不读进程环境变量**。

为什么要这么"别扭"（用户纪律 2026-08-23）：
- 项目级变量一律走项目配置文件，读外部环境变量会引入"在我机器上能跑"的
  隐式耦合，别人踩坑时无从查起。
- 唯一的例外是系统级位置（LOCALAPPDATA 这类），那属于操作系统事实，
  不是应用配置，见 driver/cua.py 的 candidate_paths。

用法：
    cfg = Config.load()              # 读 <仓库根>/.env（不存在则全用默认值）
    cfg = Config.load(env_path=...)  # 显式指定（测试用）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import audit as audit_mod
from .driver.cua import CuaDriver
from .driver.errors import ConfigError

#: 仓库根目录（扁平布局下 = 本包的父目录）
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 键名常量（避免各门面各写一份字符串漂移）
ENV_CUA_DRIVER = "WINUSE_CUA_DRIVER"
ENV_TIMEOUT_S = "WINUSE_TIMEOUT_S"
#: Everything 命令行工具 `es.exe` 的路径（`winuse_search` 用；空 = 用默认安装位置）
ENV_ES_PATH = "WINUSE_ES_PATH"
#: 键名只有一处出处（`audit` 里定义）：审计文件与数据目录本就是同一件事的两级配置
ENV_DATA_DIR = audit_mod.ENV_DATA_DIR
ENV_STATE_MAX_ELEMENTS = "WINUSE_STATE_MAX_ELEMENTS"

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_DATA_DIR = "~/.winuse"
DEFAULT_STATE_MAX_ELEMENTS = 200
#: Everything 的默认安装位置（`Everything` 安装器默认就装在这里）。
#: 注意与 `cua_driver` 的区别：那个是**自动探测候选**（见 driver/cua.py），
#: 这里是"装完不动就是这个路径"的安装事实；真不在的话 `search.resolve_es`
#: 还有 PATH 一级兜底，所以写死一个默认值不会把别人机器上的搜索堵死。
DEFAULT_ES_PATH = r"C:\Program Files\Everything\es.exe"


def parse_env_file(text: str) -> dict[str, str]:
    """解析 .env 文本。

    支持：`#` 注释、空行、`export KEY=VALUE` 前缀。
    **值一律原样返回（不剥引号）**——引号交给下游按用途处理：
    标量值走 `unquote`，命令行值走 `split_command`（它自己懂引号）。
    这样才不会被 `WINUSE_CUA_DRIVER="C:\\Program Files\\..."` 这种写法坑到。
    不支持多行值与变量插值（用不上，保持简单可预测）。
    """
    out: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            out[key] = value
    return out


def unquote(value: str) -> str:
    """标量值去引号（仅当整个值被同一对引号包住）。"""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def split_command(value: str) -> tuple[str, ...] | None:
    """把配置里的命令行拆成参数元组。

    ⚠️ **刻意不用 `shlex.split`**：它在 POSIX 模式下把反斜杠当转义符，
    Windows 路径会被吃成 `D:ProjectsWinUse...`（2026-09-26 实测踩过，
    假驱动路径就是这样被撕开的）。这里用极简分词器：
    空白分隔、单/双引号成对、**反斜杠保持字面量**。

    支持：
        C:\\tools\\cua-driver.exe                → (C:\\tools\\cua-driver.exe,)
        "C:\\Program Files\\cua\\cua-driver.exe" --quiet
                                              → (C:\\Program Files\\cua\\cua-driver.exe, --quiet)
    """
    text = (value or "").strip()
    if not text:
        return None
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if quote:
        raise ConfigError(f"{ENV_CUA_DRIVER} 引号没有闭合", f"收到：{value!r}")
    if current:
        tokens.append("".join(current))
    return tuple(tokens) or None


def _as_command(value: str) -> tuple[str, ...] | None:
    return split_command(value)


def _as_float(key: str, value: str, default: float) -> float:
    value = unquote(value)
    if not value:
        return default
    try:
        out = float(value)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是数字", f"收到：{value!r}") from exc
    if out <= 0:
        raise ConfigError(f"{key} 必须为正数", f"收到：{value!r}")
    return out


def _as_int(key: str, value: str, default: int) -> int:
    value = unquote(value)
    if not value:
        return default
    try:
        out = int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数", f"收到：{value!r}") from exc
    if out <= 0:
        raise ConfigError(f"{key} 必须为正整数", f"收到：{value!r}")
    return out


@dataclass(frozen=True)
class Config:
    """一份不可变配置快照。"""

    cua_driver: tuple[str, ...] | None
    timeout_s: float
    data_dir: Path
    state_max_elements: int
    env_path: Path | None = None
    #: `es.exe` 路径（`winuse_search` 定位一级；空 = 走 `DEFAULT_ES_PATH` → PATH）。
    #: 放在**最后**且带默认值：既有 `Config(...)` 构造与门面的假配置对象都不受影响。
    es_path: str | None = None

    # ------------------------------------------------------------------ 构造
    @classmethod
    def load(cls, env_path: Path | str | None = None) -> "Config":
        """读取配置。env_path 为 None 时读 `<仓库根>/.env`（不存在则用默认值）。"""
        path = Path(env_path) if env_path is not None else REPO_ROOT / ".env"
        values: dict[str, str] = {}
        if path.is_file():
            values = parse_env_file(path.read_text(encoding="utf-8"))
            used_path: Path | None = path
        else:
            used_path = None

        driver = _as_command(values.get(ENV_CUA_DRIVER, ""))
        timeout_s = _as_float(ENV_TIMEOUT_S, values.get(ENV_TIMEOUT_S, ""), DEFAULT_TIMEOUT_S)
        max_elements = _as_int(
            ENV_STATE_MAX_ELEMENTS,
            values.get(ENV_STATE_MAX_ELEMENTS, ""),
            DEFAULT_STATE_MAX_ELEMENTS,
        )
        data_dir_raw = values.get(ENV_DATA_DIR, "").strip() or DEFAULT_DATA_DIR
        data_dir = Path(data_dir_raw).expanduser()
        # `unquote`：`WINUSE_ES_PATH="C:\Program Files\..."` 这种带引号的写法要能用
        # （值本身按字面保留反斜杠，见 parse_env_file 的注释）
        es_path = unquote(values.get(ENV_ES_PATH, "")) or None

        return cls(
            cua_driver=driver,
            timeout_s=timeout_s,
            data_dir=data_dir,
            state_max_elements=max_elements,
            env_path=used_path,
            es_path=es_path,
        )

    # ------------------------------------------------------------------ 派生
    def driver(self) -> CuaDriver:
        """按配置造一个驱动客户端。"""
        return CuaDriver(command=self.cua_driver, timeout_s=self.timeout_s)

    def describe(self) -> dict:
        """给 doctor / 审计用的可打印摘要。"""
        return {
            "env_path": str(self.env_path) if self.env_path else None,
            "cua_driver": list(self.cua_driver) if self.cua_driver else None,
            "timeout_s": self.timeout_s,
            "data_dir": str(self.data_dir),
            "state_max_elements": self.state_max_elements,
            "es_path": self.es_path,
        }
