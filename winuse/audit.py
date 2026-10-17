"""审计日志：每个桌面动作留一条结构化记录。

三条硬纪律：
1. **不阻断业务**：写盘失败返回 None，绝不把异常抛给调用方
2. **不成为泄露源**：疑似凭据一律脱敏、长文本截断（教训：测试/日志样例里落真实 key = 泄露）；
   查询入口读出时同样过一遍脱敏——日志文件可能被手工改动或篡改
3. **不无限增长**：超过上限自动轮转（`audit.jsonl` → `audit.jsonl.1` → …），保留份数有上限

路径：默认 `<用户目录>/.winuse/audit.jsonl`；可由**项目内 .env** 的 `WINUSE_AUDIT_PATH` 覆盖
（沿用项目纪律：刻意不读进程环境变量）。轮转策略同样只从 .env 读：
`WINUSE_AUDIT_MAX_BYTES`（上限字节，缺省 5 MiB；`0` = 关闭轮转，只追加）/ `WINUSE_AUDIT_KEEP`
（保留归档份数，缺省 3）。**查询与写入用同一个路径口径**，否则"查到的"不是"写下的"。

查询：`read_recent(n)` 取最近 n 条（连续跨轮转归档）、`describe_log()` 看容量与归档概况；
CLI 入口 `winuse audit --recent N` / `winuse audit --stats`。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_DIRNAME = ".winuse"

#: `.env` 键名（唯一定义处；`config.py` 从这个模块取，避免两处字面量漂移）
ENV_AUDIT_PATH = "WINUSE_AUDIT_PATH"
ENV_DATA_DIR = "WINUSE_DATA_DIR"
AUDIT_FILENAME = "audit.jsonl"
TITLE_LIMIT = 80
TEXT_LIMIT = 120

#: 轮转：单文件上限（字节）与保留的归档份数
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 3
ENV_MAX_BYTES = "WINUSE_AUDIT_MAX_BYTES"
ENV_KEEP = "WINUSE_AUDIT_KEEP"

#: 疑似凭据的形状（只屏蔽"值"，不动正常词汇，例如 keyboard 不受影响）
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:sk|ark|agt)[-_][A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)(\s*[:=]\s*)\S+"),
)

#: 本机用户目录（R-20）：驱动 / 子进程的错误里常带完整路径，这些字符串会进审计与
#: agent 上下文。项目数据卫生铁律就是"不出现本机用户目录"，这里把整条路径
#: 换成 `<USER_HOME>`——既守住卫生，又保留"曾经有过路径"这个排查线索。
_USER_PATH_PATTERN = re.compile(r"(?i)[A-Z]:\\Users\\[^\s\"'，。）)]*")


def _env_value(env_path: str | Path | None, key: str) -> str | None:
    """从**项目内 .env** 读一个键；缺失/空值/读失败一律 None（刻意不读进程环境变量）。"""
    if not env_path:
        return None
    try:
        for line in Path(env_path).read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            if name.strip() != key:
                continue
            candidate = value.strip().strip('"').strip("'")
            if candidate:
                return candidate
    except Exception:  # noqa: BLE001 —— 读不到就按缺省处理，不阻断
        return None
    return None


def default_path(env_path: str | Path | None = None) -> Path:
    """审计文件路径。三级规则（Optional-4）：

    1. `WINUSE_AUDIT_PATH`：显式指定文件 → 直接用
    2. `WINUSE_DATA_DIR`：指定数据目录 → `数据目录/audit.jsonl`
    3. 都没有 → `~/.winuse/audit.jsonl`

    为什么要有第 2 级：`config.data_dir` 原先**声明了却没人消费**（`.env` 里写了
    `WINUSE_DATA_DIR` 对审计落盘毫无影响）——"配置骗人"比没有这项配置更糟。
    """
    override = _env_value(env_path, ENV_AUDIT_PATH)
    if override:
        return Path(override)
    data_dir = _env_value(env_path, ENV_DATA_DIR)
    if data_dir:
        return Path(data_dir).expanduser() / AUDIT_FILENAME
    return Path.home() / DEFAULT_DIRNAME / AUDIT_FILENAME


def redact_text(text: Any, limit: int = TEXT_LIMIT) -> Any:
    """脱敏 + 截断。None/空串原样返回；截断时标明原长度，便于排查。"""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    out = _USER_PATH_PATTERN.sub("<USER_HOME>", out)
    # bearer 那条会把 "Bearer xxx" 整段替换，上面已统一成 [REDACTED]
    if len(out) <= limit:
        return out
    return f"{out[:limit]}…(共{len(out)}字)"


def _redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_obj(v) for v in value]
    return value


def _clean_target(target: dict | None) -> dict:
    target = target or {}
    return {
        "app": target.get("app_name") or target.get("app"),
        "pid": target.get("pid"),
        "window_id": target.get("window_id"),
        "title": redact_text(target.get("title"), TITLE_LIMIT),
    }


def build_entry(
    event: str,
    *,
    action: str | None = None,
    ok: bool | None = None,
    code: str | None = None,
    target: dict | None = None,
    detail: dict | None = None,
    **extra: Any,
) -> dict:
    """构造一条审计记录（已脱敏）。事件名约定：act.dry_run / act.executed / act.blocked 等。"""
    entry: dict = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
    }
    if action is not None:
        entry["action"] = action
    if ok is not None:
        entry["ok"] = bool(ok)
    if code is not None:
        entry["code"] = code
    if target:
        entry["target"] = _clean_target(target)
    if detail:
        entry["detail"] = _redact_obj(detail)
    for key, value in extra.items():
        entry[key] = _redact_obj(value)
    return entry


def env_sink(env_path: str | Path | None, source: str):
    """造一个"落盘到指定 .env 口径文件"的 sink，并打上来源标记（`cli` / `mcp`）。

    只在这里实现一次：门面各写一份闭包会漂移（比如某处忘了把 `record` 的 None 翻成
    False，审计失败又变静默）。返回值遵循 `sink_event` 协议。
    """

    def sink(entry: dict):
        payload = dict(entry)
        payload.setdefault("source", source)
        return record(payload, env_path=env_path) is not None

    return sink


def sink_event(sink, **fields) -> bool | None:
    """把一条审计事件交给 sink 落盘；返回**是否写入成功**（`None` = 没有 sink）。

    协议（R-1）：sink 是 `entry -> Any` 的可调用对象，**只有显式返回 `False` 才表示
    写失败**（返回 `None`/`True`/其它一律视为成功）——这样 `list.append` 这种最朴素的
    sink 也能直接用，同时真实落盘实现（如 `record`）可以用 `is not None` 明确表态。

    为什么要有返回值：审计失败原先被静默吞掉，"宣称完整审计"就成了无法证伪的说法。
    核心把结果放进回执 `audit_written`，让调用方当场看得见。
    """
    if sink is None:
        return None
    try:
        return sink(build_entry(**fields)) is not False
    except Exception:  # noqa: BLE001 —— 审计绝不阻断业务，但失败要能被看见
        return False


def _as_int(value: str | None, fallback: int) -> int:
    """把 .env 里的字符串读成整数；缺失/非法一律回退（坏配置不该变成崩溃）。"""
    if value is None:
        return fallback
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def rotation_policy(env_path: str | Path | None = None) -> dict[str, int]:
    """轮转策略 `{"max_bytes": 单文件上限, "keep": 保留归档份数}`（只从项目内 .env 读）。

    - `WINUSE_AUDIT_MAX_BYTES`：≤ 0 表示**显式关闭轮转**（只追加，牺牲上限换取证完整性）
    - `WINUSE_AUDIT_KEEP`：负数按 0 处理（不保留归档）
    """
    max_bytes = _as_int(_env_value(env_path, ENV_MAX_BYTES), DEFAULT_MAX_BYTES)
    keep = max(0, _as_int(_env_value(env_path, ENV_KEEP), DEFAULT_KEEP))
    return {"max_bytes": max_bytes, "keep": keep}


# ------------------------------------------------------------------ 轮转


def _rotated_path(path: Path, index: int) -> Path:
    """归档文件名：`audit.jsonl.1`。**1 = 最近一次轮转**，数字越大越旧。"""
    return path.with_name(f"{path.name}.{index}")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _archives(path: Path) -> list[tuple[int, Path]]:
    """`[(索引, 归档文件)]`，按索引升序（.1 在前）。读不到一律当没有，绝不抛。"""
    target = Path(path)
    found: list[tuple[int, Path]] = []
    try:
        parent = target.parent
        if not parent.is_dir():
            return []
        prefix = target.name + "."
        for child in parent.iterdir():
            if not child.name.startswith(prefix):
                continue
            index_text = child.name[len(prefix) :]
            if index_text.isdigit() and child.is_file():
                found.append((int(index_text), child))
    except Exception:  # noqa: BLE001 —— 列目录失败不能挡住查询/写入
        return []
    found.sort(key=lambda item: item[0])
    return found


def rotated_paths(path: str | Path) -> list[Path]:
    """归档文件列表（索引升序：第 1 个是最近一次轮转，越靠后越旧）。"""
    return [archive for _, archive in _archives(Path(path))]


def rotate_if_needed(
    path: str | Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
    incoming_bytes: int = 0,
) -> bool:
    """超过上限就把当前文件滚成 `.1`（`.1→.2`、…、最旧的丢弃），返回是否真轮转；**绝不抛**。

    用「当前大小 + 本次要写的字节数 > 上限」判断，避免写完一整行才超限；
    单条记录本身就大于上限时本轮不滚（否则会无限滚动），写完下一轮再滚。
    """
    try:
        target = Path(path)
        if max_bytes is None or max_bytes <= 0:
            return False
        if not target.is_file():
            return False
        size = target.stat().st_size
        if size <= 0 or size + max(0, int(incoming_bytes)) <= max_bytes:
            return False

        limit = max(0, int(keep))
        if limit == 0:
            _unlink_quietly(target)  # 不保留归档：旧日志直接丢弃
            return True
        archives = _archives(target)  # 一次目录扫描拿到现状（升序）
        for index, archive in archives:
            if index >= limit:
                _unlink_quietly(archive)  # 含本轮要被挤出去的最旧一份；顺带清掉超编的历史残留
        # 只搬**实际存在**的归档（而不是盲扫 0..keep）：keep 设大时（比如 200）
        # 每轮扫 200 次 is_file()，而真实归档可能只有几十份——那是纯浪费，
        # 而且在锁内放大成可观测的延迟（并发用例实测从 22s 降到 1s 级）
        for index in sorted((i for i, _ in archives if i < limit), reverse=True):
            _rotated_path(target, index).replace(_rotated_path(target, index + 1))
        target.replace(_rotated_path(target, 1))
        return True
    except Exception:  # noqa: BLE001 —— 轮转失败不能挡住业务写入
        return False


# ------------------------------------------------------------------ 并发

#: 锁文件后缀（与日志同目录，形如 `audit.jsonl.lock`）
LOCK_SUFFIX = ".lock"

#: 拿锁的最长等待与重试间隔（Windows 用非阻塞锁 + 退避，见 `_lock_file`）
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_RETRY_SECONDS = 0.005


def _lock_file(handle) -> None:
    """对已打开的 handle 加**独占锁**（阻塞直到拿到或超时）。

    Windows 上刻意用 `LK_NBLCK` + 自己退避，**不用** `LK_LOCK`：
    `LK_LOCK` 的争用重试粒度是**整秒**（每秒一次，共 10 次），8 个写者并发时
    单条审计要等几百毫秒——并发用例实测 200 条要 22 秒。换成 5ms 退避后回到
    亚秒级，功能不变。
    """
    if os.name == "nt":
        import msvcrt
        import time as _time

        # msvcrt 锁的是"从当前位置起的 N 字节"，空文件先写 1 字节占位
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = _time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if _time.monotonic() >= deadline:
                    raise
                _time.sleep(LOCK_RETRY_SECONDS)
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _audit_lock(path: str | Path):
    """审计"轮转 + 追加写"的**跨进程**临界区（Optional-5）。

    为什么必须有：两个写者同时判断"超限了"，会各自把当前文件搬进 `.1`——其中一个
    搬走的可能是对方**刚创建的新文件**，于是两份写入互相覆盖。实测（8 线程 × 25 行、
    上限 400 字节）**200 条只落了 189 条**，丢 11 条。这种"日志偶尔少几条"最难查：
    正常路径完全看不出来。

    拿不到锁**不阻断**审计（退化成无锁）：审计的定位是"尽力留痕"，绝不能因为
    锁文件不可写就让业务动作失败。`record` 里也不再嵌套加锁（Windows 的
    `msvcrt.locking` 是按文件区域锁的，同进程嵌套会自锁）。
    """
    handle = None
    locked = False
    try:
        try:
            lock_path = Path(str(path) + LOCK_SUFFIX)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")  # noqa: SIM115 —— 生命周期由本函数管
            _lock_file(handle)
            locked = True
        except Exception:  # noqa: BLE001 —— 锁不可用不能挡住审计
            handle = None
            locked = False
        yield
    finally:
        if handle is not None:
            try:
                if locked:
                    _unlock_file(handle)
            except Exception:  # noqa: BLE001
                pass
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ 查询


def _parse_line(raw: str) -> dict:
    """一行 → 一条记录。坏行不丢（标 `unparsable` 便于发现丢数据），且照样过脱敏。"""
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001 —— 半截行/手工改动都算坏行
        return {"raw": redact_text(raw), "unparsable": True}
    if isinstance(obj, dict):
        return _redact_obj(obj)
    return {"raw": redact_text(raw), "unparsable": True}


def _log_files(target: Path) -> list[Path]:
    """候选文件按「新 → 旧」：主文件在前，归档按索引升序（.1 更近）跟在后面。"""
    return [target, *rotated_paths(target)]


def read_recent(
    count: int = 20,
    path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> list[dict]:
    """读最近 `count` 条，按时间先后返回（最后一条最新）。**绝不抛异常**。

    - 连续跨轮转：主文件不够就接着从 `.1`（最近一次轮转）往前补，读的是一条连续历史
    - 坏行不丢：非 JSON 行以 `{"raw": ..., "unparsable": true}` 原样带出（已脱敏）
    - 读出前再脱敏一遍：日志文件可能被手工改动，查询入口也不能成为泄露源
    """
    try:
        limit = int(count)
    except (TypeError, ValueError):
        return []
    if limit <= 0:
        return []
    target = Path(path) if path is not None else default_path(env_path)
    collected: list[dict] = []
    for candidate in _log_files(target):
        if not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:  # noqa: BLE001 —— 单个文件读不了就跳过，别让整次查询失败
            continue
        for raw in reversed(lines):  # 从文件末尾往前读 = 从新到旧
            stripped = raw.strip()
            if not stripped:
                continue
            collected.append(_parse_line(stripped))
            if len(collected) >= limit:
                break
        if len(collected) >= limit:
            break
    collected.reverse()
    return collected


def describe_log(path: str | Path | None = None, env_path: str | Path | None = None) -> dict:
    """日志与轮转概况（只读）：大小、条数、归档文件、当前策略。**绝不抛异常**。

    `error` 只回错误类型名或短代码（如 `NOT_A_FILE`），**不回显正文**——日志里可能带
    路径/凭据等敏感内容。
    """
    target = Path(path) if path is not None else default_path(env_path)
    policy = rotation_policy(env_path)
    info: dict = {
        "path": str(target),
        "exists": False,
        "size_bytes": 0,
        "lines": 0,
        "rotated_files": [],
        "max_bytes": policy["max_bytes"],
        "keep": policy["keep"],
        "error": None,
    }
    try:
        if target.is_file():
            info["exists"] = True
            info["size_bytes"] = target.stat().st_size
            text = target.read_text(encoding="utf-8", errors="replace")
            info["lines"] = sum(1 for line in text.splitlines() if line.strip())
        elif target.exists():
            info["error"] = "NOT_A_FILE"  # 目录/设备之类：别假装是一份空日志
        info["rotated_files"] = [
            {"path": str(archive), "size_bytes": archive.stat().st_size} for _, archive in _archives(target)
        ]
    except Exception as exc:  # noqa: BLE001 —— 概况读不到也要给出结果，不抛
        info["error"] = type(exc).__name__
    return info


def record(entry: dict, path: str | Path | None = None, env_path: str | Path | None = None) -> dict | None:
    """追加写一条记录（写前按需轮转）；成功返回落盘的那条，失败返回 None（绝不抛）。"""
    payload = _redact_obj(dict(entry))
    payload.setdefault("ts", datetime.now().astimezone().isoformat(timespec="seconds"))
    target_path = Path(path) if path is not None else default_path(env_path)
    policy = rotation_policy(env_path)
    try:
        line = json.dumps(payload, ensure_ascii=False)
        # 「判超限 → 轮转 → 追加写」必须在**同一个**跨进程临界区里（Optional-5）：
        # 分开做的话，两个写者会同时判定超限，各自把当前文件搬进 `.1`——其中一个
        # 搬走的可能是对方刚创建的新文件，于是互相覆盖。实测 8 线程 × 25 行时
        # 200 条只落 189 条，丢 11 条，且丢得**分散**（最难查的形态）。
        with _audit_lock(target_path):
            target_path.parent.mkdir(parents=True, exist_ok=True)
            rotate_if_needed(
                target_path,
                max_bytes=policy["max_bytes"],
                keep=policy["keep"],
                incoming_bytes=len(line.encode("utf-8")) + 1,
            )
            with target_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:  # noqa: BLE001 —— 审计失败不能影响业务
        return None
    return payload
