r"""winuse_registry：注册表只读盘点 + 写操作意图（PR-8）。

设计落点：
1. **只读用标准库 `winreg`**（零第三方依赖），且**只以 KEY_READ 打开**——
   本模块没有任何真写路径。
2. **安全硬门（写死在代码里，不是配置项）**：`SAM` 与 `SECURITY` 两棵子树
   **一律拒绝**，无论调用方是否管理员、无论读还是写。
   为什么必须硬拒：
   - `SAM\SAM`（以及整个 `HKLM\\SAM`）存的是**本地账户口令的哈希**；
     `SECURITY` 存 LSA 机密（缓存域凭据、密钥、策略）。
   - 这两处的读权限一旦被 agent 拿到，等于把"离线破解口令"的数据源交出去；
     写权限则能直接造成登录不可用或凭据被替换。
   - 上层是自动化 agent，无法像人一样在 regedit 里"看到就明白不该动"，
     所以把"不该有"做成结构性拒绝（`REGISTRY_PATH_DENIED`），而不是靠提示词提醒。
   需要这类数据的人应当自己开 regedit 显式操作，不走本门面。
3. **纯逻辑与 winreg 分离**：路径归一化、敏感路径判定、值渲染、子键遍历
   都是纯函数/依赖注入（`backend` 可替换成假对象），单测**完全不碰真实注册表**。
4. **写操作默认干跑**：`plan_registry_write` 只返回意图对象（`requires_confirm: true`
   + 将运行的 `reg.exe` 参数预览），本模块不落任何写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .. import limit as limit_mod
from ..driver.errors import MissingParam, WinUseError

#: 支持的根键（短名 → 长名别名也在 `_HIVE_ALIASES` 里）
HIVES: tuple[str, ...] = ("HKCU", "HKLM", "HKCR", "HKU")

#: 长名别名（用户从别处复制来的写法）
_HIVE_ALIASES: dict[str, str] = {
    "HKEY_CURRENT_USER": "HKCU",
    "HKEY_LOCAL_MACHINE": "HKLM",
    "HKEY_CLASSES_ROOT": "HKCR",
    "HKEY_USERS": "HKU",
}

#: 敏感子树的**第一级**名字：命中即拒（大小写不敏感）
SENSITIVE_SUBTREES: frozenset[str] = frozenset({"SAM", "SECURITY"})

#: 写操作允许的值类型（`reg.exe /t` 口径）
REG_WRITE_TYPES: tuple[str, ...] = (
    "REG_NONE",
    "REG_SZ",
    "REG_EXPAND_SZ",
    "REG_BINARY",
    "REG_DWORD",
    "REG_QWORD",
    "REG_MULTI_SZ",
)

#: 注册表类型号 → 名字（写死数值，避免非 Windows 环境 import winreg 失败）
_REG_TYPE_NAMES: dict[int, str] = {
    0: "REG_NONE",
    1: "REG_SZ",
    2: "REG_EXPAND_SZ",
    3: "REG_BINARY",
    4: "REG_DWORD",
    5: "REG_DWORD_BIG_ENDIAN",
    6: "REG_LINK",
    7: "REG_MULTI_SZ",
    8: "REG_RESOURCE_LIST",
    9: "REG_FULL_RESOURCE_DESCRIPTOR",
    10: "REG_RESOURCE_REQUIREMENTS_LIST",
    11: "REG_QWORD",
}

#: 二进制值只预览这么多字节（避免把大 blob 灌进上下文）
BINARY_PREVIEW_BYTES = 32

#: 字符串值单条截断长度
VALUE_MAX_CHARS = 512

#: 读输出的口径声明
REGISTRY_NOTE = (
    "只读：以 KEY_READ 打开键；SAM 与 SECURITY 子树被结构性拒绝（REGISTRY_PATH_DENIED），"
    "无论权限高低；REG_BINARY 只给长度与前若干字节预览。"
)


class RegistryPathDenied(WinUseError):
    """命中敏感路径（SAM / SECURITY）——结构性拒绝，不是权限问题。"""

    code = "REGISTRY_PATH_DENIED"


class RegistryHiveUnknown(WinUseError):
    """根键名不在支持清单里。"""

    code = "REGISTRY_HIVE_UNKNOWN"


class RegistryKeyNotFound(WinUseError):
    """键不存在。"""

    code = "REGISTRY_KEY_NOT_FOUND"


class RegistryValueNotFound(WinUseError):
    """指定了 value_name 但该键下没有这个值。"""

    code = "REGISTRY_VALUE_NOT_FOUND"


class RegistryAccessDenied(WinUseError):
    """键存在但当前权限不足（如未提权访问 HKLM 受保护位置）。"""

    code = "REGISTRY_ACCESS_DENIED"


class RegistryEnumFailed(WinUseError):
    """枚举值/子键时出错（R-18b）。

    为什么单列：枚举过程中"没有更多项"（`ERROR_NO_MORE_ITEMS`）**才是正常的结尾**，
    其余 `OSError`（权限不足、句柄失效…）都被当成结尾的话，权限错误会静默变成
    空列表——上层看到的是"这个键没有任何值"，而不是"你读不到"。
    """

    code = "REGISTRY_ENUM_FAILED"


#: Win32 `ERROR_NO_MORE_ITEMS`：枚举正常结束的唯一判据
ERROR_NO_MORE_ITEMS = 259


def _raise_unless_end_of_enumeration(exc: OSError, key: Any) -> None:
    """枚举循环的统一出口：只有 `ERROR_NO_MORE_ITEMS` 才算正常结束。"""
    # winerror 是 Windows 上的规范字段；有些路径只填 errno（同值 259），两者都认
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if code == ERROR_NO_MORE_ITEMS:
        return
    raise RegistryEnumFailed(
        f"枚举注册表项失败（winerror={getattr(exc, 'winerror', None)}）",
        f"原始错误：{str(exc)[:200]}；句柄：{key!r}",
    ) from exc


class RegistryUnavailable(WinUseError):
    """当前环境没有 winreg（非 Windows）或标准库导入失败。"""

    code = "REGISTRY_UNAVAILABLE"


class UnknownValueType(WinUseError):
    """写入意图里的值类型不是 reg.exe 认识的那几种。"""

    code = "UNKNOWN_VALUE_TYPE"


@dataclass
class RegReadSpec:
    """只读请求（门面把它翻译成这个，业务逻辑只在这里）。"""

    hive: str
    path: str = ""
    #: 指定则只回这一个值；`""` 是**默认值**（未命名值），与"不指定"不同
    value_name: str | None = None
    #: True 时递归展开子键（返回相对路径），False 只列一层子键名
    recurse: bool = False
    max_depth: int = 3
    max_subkeys: int = 200


@dataclass
class RegWriteSpec:
    """写请求：只用来生成意图对象，本模块不落盘。"""

    hive: str
    path: str
    value_name: str | None = None
    value: str | None = None
    value_type: str = "REG_SZ"
    #: True 表示删除该值而不是写入
    delete: bool = False


class RegistryBackend(Protocol):
    """注册表访问的最小接口（真实实现用 winreg，单测注入假对象）。"""

    def open_key(self, hive: str, path: str) -> Any: ...

    def enum_values(self, key: Any) -> list[tuple[str, Any, int]]: ...

    def enum_subkeys(self, key: Any) -> list[str]: ...

    def close(self, key: Any) -> None: ...


# --------------------------------------------------------------------------
# 纯函数：归一化 / 敏感路径判定 / 值渲染
# --------------------------------------------------------------------------


def normalize_hive(hive: str) -> str:
    """根键名归一化（大小写不敏感，兼容长名别名）。不认识就抛，不猜默认根键。"""
    text = str(hive or "").strip().upper()
    if text in _HIVE_ALIASES:
        text = _HIVE_ALIASES[text]
    if text not in HIVES:
        raise RegistryHiveUnknown(
            f"不认识的根键：{hive}",
            "可用：" + "、".join(HIVES),
        )
    return text


def normalize_path(path: str | None) -> str:
    """键路径归一化：统一反斜杠、去掉首尾与重复分隔符。

    刻意不留首尾反斜杠：`HKLM\\SAM\\` 与 `SAM` 必须落到同一个判定上，
    否则敏感路径门禁能被一个尾随反斜杠绕过。
    """
    if path is None:
        return ""
    text = str(path).replace("/", "\\").strip()
    return "\\".join(part for part in text.split("\\") if part)


def path_components(path: str | None) -> list[str]:
    """归一化路径的各级名字（大写，便于比较）。"""
    return [part.upper() for part in normalize_path(path).split("\\") if part]


def full_key_path(hive: str, path: str) -> str:
    """拼出给人看/给 reg.exe 用的完整键路径。

    根键名先归一化（`hklm` → `HKLM`），认不出就原样保留——
    这是展示/预览用的字符串，不该因为拼写陌生而抛错。
    """
    try:
        hive_name = normalize_hive(hive)
    except RegistryHiveUnknown:
        hive_name = str(hive or "").strip()
    normalized = normalize_path(path)
    return f"{hive_name}\\{normalized}" if normalized else hive_name


def is_denied_path(hive: str, path: str | None) -> bool:
    """是否命中敏感子树（纯函数，便于穷举断言）。

    判定规则（只在 HKLM 下生效）：
    - 第一级名字是 `SAM` 或 `SECURITY` → 拒（含 `SAM\\SAM`、`SECURITY\\Policy\\Secrets`）
    - `SAM` 开头但只是前缀的键（如 `SAMPLE`、`SAMClients`）→ **不拒**，
      否则会误伤正常软件的自建键
    """
    if normalize_hive(hive) != "HKLM":
        return False
    components = path_components(path)
    if not components:
        return False
    return components[0] in SENSITIVE_SUBTREES


def check_path_allowed(hive: str, path: str | None) -> None:
    """敏感路径硬门：命中直接抛 `REGISTRY_PATH_DENIED`。"""
    if is_denied_path(hive, path):
        raise RegistryPathDenied(
            f"拒绝访问敏感注册表路径：{full_key_path(hive, normalize_path(path))}",
            "SAM 与 SECURITY 子树存有口令哈希与 LSA 机密：读=泄露凭据数据源，"
            "写=破坏登录。本门面对 agent 结构性拒绝（与人是否提权无关）；"
            "确需查看请由人工在 regedit 中显式操作。",
        )


def value_type_name(type_id: Any) -> str:
    """注册表类型号 → 名字；未知类型如实标出原始号（不猜成 REG_SZ）。"""
    try:
        return _REG_TYPE_NAMES.get(int(type_id), f"REG_UNKNOWN({type_id})")
    except (TypeError, ValueError):
        return f"REG_UNKNOWN({type_id})"


def is_default_value(name: Any) -> bool:
    """是否是"默认值"（未命名值，`reg.exe` 里写作 `/ve`）。"""
    return name is None or str(name) == ""


def render_value(value: Any) -> Any:
    """把注册表值渲染成 JSON 可序列化、可读且不灌爆上下文的形态。

    - `bytes`（REG_BINARY 等）：只给长度 + 前若干字节十六进制预览
    - `list`（REG_MULTI_SZ）：逐项转字符串
    - 长字符串：截断并留省略号标记
    """
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        preview = " ".join(f"{byte:02x}" for byte in raw[:BINARY_PREVIEW_BYTES])
        return {
            "kind": "binary",
            "length": len(raw),
            "preview": preview + (" …" if len(raw) > BINARY_PREVIEW_BYTES else ""),
        }
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str):
        if len(value) > VALUE_MAX_CHARS:
            return value[:VALUE_MAX_CHARS] + "…"
        return value
    return value


def render_values(raw_values: Any) -> list[dict]:
    """把后端返回的 (名字, 值, 类型号) 三元组列表渲染成对外结构。"""
    out: list[dict] = []
    for item in raw_values or []:
        if isinstance(item, dict):
            name, value, type_id = item.get("name"), item.get("value"), item.get("type")
        else:
            try:
                name, value, type_id = item
            except (TypeError, ValueError):
                continue
        out.append(
            {
                "name": "" if is_default_value(name) else str(name),
                "type": value_type_name(type_id),
                "value": render_value(value),
            }
        )
    return out


def collect_subkeys(
    backend: RegistryBackend,
    hive: str,
    path: str,
    recurse: bool = False,
    max_depth: int = 3,
    max_subkeys: int = 200,
) -> dict:
    """列子键名。

    - `recurse=False`：只列一层子键名（相对于**起始键**的名字）
    - `recurse=True`：深度优先展开，返回相对于起始键的路径（`A\\B`），
      到 `max_depth` 或 `max_subkeys` 为止；**截断必须显式标出**
      （`truncated: true`），不静默丢信息
    """
    start = normalize_path(path)
    result: dict[str, Any] = {
        "names": [],
        "truncated": False,
        "recurse": bool(recurse),
        #: 递归时被跳过的敏感子树（显式列出，不静默丢信息——R-18a）
        "skipped_sensitive": [],
    }

    def walk(current_full: str, rel_prefix: str, depth: int) -> None:
        if result["truncated"]:
            return
        key = backend.open_key(hive, current_full)
        try:
            children = sorted(backend.enum_subkeys(key) or [])
        finally:
            backend.close(key)
        for name in children:
            if len(result["names"]) >= max(0, int(max_subkeys)):
                result["truncated"] = True
                return
            relative = f"{rel_prefix}\\{name}" if rel_prefix else str(name)
            child_full = f"{current_full}\\{name}" if current_full else str(name)
            # 每一级候选都要重查敏感路径（R-18a）：只在起始路径判一次的话，
            # 从 HKLM 根递归就能一路打开 HKLM\SAM 及其子树——门禁形同虚设。
            # 跳过而不是抛：用户并不是想看它，只是递归扫过；但要显式记下来。
            if is_denied_path(hive, child_full):
                result["skipped_sensitive"].append(relative)
                continue
            result["names"].append(relative if recurse else str(name))
            if recurse and depth + 1 < max(0, int(max_depth)):
                walk(child_full, relative, depth + 1)

    walk(start, "", 0)
    return result


# --------------------------------------------------------------------------
# winreg 后端（唯一真 IO 边界；单测注入假对象替换它）
# --------------------------------------------------------------------------


class _WinregBackend:
    """标准库 winreg 后端：**只以 KEY_READ 打开**，没有任何写方法。"""

    def __init__(self) -> None:
        try:
            import winreg  # noqa: PLC0415 - 只在真正要用时导入，便于非 Windows 跑纯逻辑
        except ImportError as exc:  # pragma: no cover - 非 Windows 才会走到
            raise RegistryUnavailable("当前环境没有 winreg", "本模块只在 Windows 上做真实读取") from exc
        self._winreg = winreg
        self._roots = {
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCR": winreg.HKEY_CLASSES_ROOT,
            "HKU": winreg.HKEY_USERS,
        }

    def open_key(self, hive: str, path: str) -> Any:
        winreg = self._winreg
        root = self._roots[normalize_hive(hive)]
        try:
            return winreg.OpenKey(root, normalize_path(path), 0, winreg.KEY_READ)
        except FileNotFoundError as exc:
            raise RegistryKeyNotFound(
                f"键不存在：{full_key_path(hive, path)}", "检查路径拼写（不含根键名？）"
            ) from exc
        except PermissionError as exc:
            raise RegistryAccessDenied(
                f"没有读取权限：{full_key_path(hive, path)}", "该键需要更高权限；本模块不尝试提权"
            ) from exc
        except OSError as exc:
            raise RegistryAccessDenied(
                f"打开键失败：{full_key_path(hive, path)}", str(exc)[:200]
            ) from exc

    def enum_values(self, key: Any) -> list[tuple[str, Any, int]]:
        winreg = self._winreg
        out: list[tuple[str, Any, int]] = []
        index = 0
        while True:
            try:
                out.append(winreg.EnumValue(key, index))
            except OSError as exc:
                _raise_unless_end_of_enumeration(exc, key)
                return out
            index += 1

    def enum_subkeys(self, key: Any) -> list[str]:
        winreg = self._winreg
        out: list[str] = []
        index = 0
        while True:
            try:
                out.append(winreg.EnumKey(key, index))
            except OSError as exc:
                _raise_unless_end_of_enumeration(exc, key)
                return out
            index += 1

    def close(self, key: Any) -> None:
        try:
            self._winreg.CloseKey(key)
        except OSError:  # pragma: no cover - 关闭失败无需影响结果
            pass


def _default_backend() -> RegistryBackend:
    return _WinregBackend()


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def read_registry(
    spec: RegReadSpec,
    backend: RegistryBackend | None = None,
    confirmed: bool = False,
) -> dict:
    r"""只读读取注册表：键下的值（可指定单个）与子键名。

    两道门禁，顺序刻意固定（**先归一化 → 再判敏感 → 才打开键**，任何情况下都
    不会先碰到敏感键的句柄）：

    1. **结构性硬拒**（`check_path_allowed`）：`HKLM\SAM` / `HKLM\SECURITY`
       及其子树——那里是口令哈希与 LSA 机密，**不提供"确认后即可读"的通道**。
    2. **敏感读确认门**（R-11，`guard.assess_registry_read`）：
       `SYSTEM\CurrentControlSet` / `BCD00000000` 等系统配置区——读它们不改系统，
       但足以摸清机器配置，因此要显式 `confirmed=True` 才读。
       判据来自 `core.guard`，本模块不复制一份。
    """
    hive = normalize_hive(spec.hive)
    path = normalize_path(spec.path)
    check_path_allowed(hive, path)

    from . import guard as guard_mod

    verdict = guard_mod.assess_registry_read(hive, path)
    if verdict.dangerous and not confirmed:
        return {
            "ok": False,
            "requires_confirm": True,
            "danger": verdict.to_dict(),
            "hive": hive,
            "path": path,
            "key": full_key_path(hive, path),
            "error": {
                "code": verdict.code or "CONFIRM_REQUIRED",
                "message": verdict.reason,
                "detail": (
                    "确认方式：CLI 加 --confirm / MCP 传 confirm=true。"
                    "注意 HKLM\\SAM 与 HKLM\\SECURITY 是结构性硬拒，确认也读不了。"
                ),
            },
        }

    client = backend or _default_backend()
    key = client.open_key(hive, path)
    try:
        raw_values = client.enum_values(key)
    finally:
        client.close(key)

    values = render_values(raw_values)
    if spec.value_name is not None:
        wanted = "" if is_default_value(spec.value_name) else str(spec.value_name)
        matched = [item for item in values if item["name"] == wanted]
        if not matched:
            raise RegistryValueNotFound(
                f"键 {full_key_path(hive, path)} 下没有值：{wanted or '(默认值)'}",
                "现有值：" + ("、".join(item["name"] or "(默认值)" for item in values) or "（空）"),
            )
        values = matched

    # 递归参数上限（R-21）：没有上限时一次调用能扫完整棵注册表；
    # 这里**夹紧**而不是拒绝（读是只读的，夹紧更好用），并在回执里说明被夹过
    depth = min(max(int(spec.max_depth), 0), limit_mod.MAX_REGISTRY_DEPTH)
    subkeys_cap = min(max(int(spec.max_subkeys), 0), limit_mod.MAX_REGISTRY_SUBKEYS)
    clamped = {}
    if depth != int(spec.max_depth):
        clamped["max_depth"] = depth
    if subkeys_cap != int(spec.max_subkeys):
        clamped["max_subkeys"] = subkeys_cap

    subkeys = collect_subkeys(
        client,
        hive,
        path,
        recurse=bool(spec.recurse),
        max_depth=depth,
        max_subkeys=subkeys_cap,
    )
    out: dict[str, Any] = {
        "ok": True,
        "hive": hive,
        "path": path,
        "key": full_key_path(hive, path),
        "value_name": spec.value_name,
        "values": values,
        "subkeys": subkeys["names"],
        "subkeys_truncated": subkeys["truncated"],
        "recurse": bool(spec.recurse),
        "note": REGISTRY_NOTE,
    }
    if clamped:
        out["clamped"] = clamped
    if subkeys.get("skipped_sensitive"):
        # 递归时跳过的敏感子树：显式上报，别让上层以为"这下面什么都没有"
        out["skipped_sensitive"] = subkeys["skipped_sensitive"]
    return out


def _value_switch(value_name: str | None) -> list[str]:
    """`reg.exe` 的值选择参数：未命名（默认）值要用 `/ve`，否则 `/v <名字>`。"""
    if is_default_value(value_name):
        return ["/ve"]
    return ["/v", str(value_name)]


def plan_registry_write(spec: RegWriteSpec) -> dict:
    """生成注册表写操作的**意图对象**——本模块绝不写注册表。

    为什么只出意图：写注册表能改开机自启、浏览器策略、服务配置，属于"改了就可能
    让系统起不来"的操作。按项目铁律默认干跑：这里只回将运行的 `reg.exe` 参数，
    由门禁在用户确认后执行。
    """
    hive = normalize_hive(spec.hive)
    path = normalize_path(spec.path)
    check_path_allowed(hive, path)
    if not path:
        raise MissingParam("写注册表必须给出键路径（不能只给根键）", "用法示例：--path 'Software\\Vendor\\App'")
    if spec.value_name is None:
        raise MissingParam(
            "写注册表必须给出值名",
            "默认值（未命名值）请显式传空串 ''，本模块不替你猜是哪一个值",
        )

    key_path = full_key_path(hive, path)
    if spec.delete:
        would_run = ["reg", "delete", key_path, *_value_switch(spec.value_name), "/f"]
        intent = "registry.delete_value"
    else:
        value_type = str(spec.value_type or "").strip().upper()
        if value_type not in REG_WRITE_TYPES:
            raise UnknownValueType(
                f"不支持的值类型：{spec.value_type}",
                "可用：" + "、".join(REG_WRITE_TYPES),
            )
        if spec.value is None:
            raise MissingParam("写注册表必须给出值内容（--value）", "本模块不替你猜空值")
        would_run = [
            "reg",
            "add",
            key_path,
            *_value_switch(spec.value_name),
            "/t",
            value_type,
            "/d",
            str(spec.value),
            "/f",
        ]
        intent = "registry.set_value"

    return {
        "ok": True,
        "requires_confirm": True,
        "intent": intent,
        "would_run": would_run,
        "note": "需用户确认后才执行，本模块不写注册表",
    }
