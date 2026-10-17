"""winuse_screenshot：把**像素**交出去的唯一出口（window / desktop / region 三模式）。

为什么需要它：WinUse 是 text-first 门面——取状态只回元素索引、绝不回图
（`core/state.py` 显式带 `include_screenshot:false`）。但有一类事文本做不到：
canvas / 视频 / WebGL / 自绘界面没有元素树，图标、小字、颜色只能看图。所以截图不是
"另一种取状态"，而是一个**显式升级动作**：默认路径仍是纯文本，要看像素得专门调这个出口。

三条关键做法：

1. **三种模式的语义不同，不能混成一个"截图"**：
   - `window`：截**一个窗口**（驱动 `get_window_state`），顺带把 UIA 树**关掉**
     （`include_accessibility_tree:false`）。元素树在这个出口里纯属浪费——真机实测同一个
     窗口的树是几百到 5000 个元素，而这里只要像素。
   - `desktop`：整屏**真像素**（驱动 `get_desktop_state`）。实测 3840×2160 屏上
     `screen_width == screenshot_width`（不缩放），所以它可以直接当全屏动作的坐标源。
   - `region`：区域放大（驱动 `zoom`）。三种里唯一**带上下文**的：入参坐标是
     `get_window_state` 那张图的像素空间（不是原生桌面坐标），区域宽上限 500 px
     （驱动硬限制），出图按「窗口截图空间 ÷ 原生分辨率」的比放大——**比例不固定**
     （实测同一个 100×100 请求，一次回来 130×130、另一次 417×417，取决于驱动此刻的
     截图空间），而且这个模式的图是 **JPEG**（另两个模式是 PNG）。这些都必须如实告诉
     上层（见 `REGION_NOTE` 与 `region.padded_ratio`）。
2. **落盘，不把图塞进回包**：回包只有 `path / width / height / bytes / scale / …` 这几十个
   字符，图在数据目录的 `screenshots/` 下。这是 text-first 的延续：一张 4K 图的 base64
   ≈ 2.6 MB ≈ 数千 token，还会挤掉真正要读的文本。文件名只带**模式 + 时间戳**——
   窗口标题、应用名、路径属于用户隐私，绝不进文件名；扩展名跟着**文件头**走
   （`.png` / `.jpg`），不给上层一个名不副实的文件。
3. **宽高与格式以落盘文件为准**：驱动报的 `screenshot_width/height` 与文件头对不上时
   以文件为准，并把差异标出来（`size_mismatch` / `driver_reported_size`）。
   「报多大的图」与「发出去多大的图」不一致时，坐标换算会**静默**错，这类错最难查。

失败分流（与 `core/ocr.py` / `core/notify.py` 同口径）：
- **调用方错误**（模式名 / 矩形 / options / out_dir）→ 抛 `WinUseError` 子类，
  且**在下发驱动之前**（参数错就别浪费一次驱动调用）
- **运行期失败**（驱动报错 / 没回图 / 图坏了 / 落盘失败）→ 返回 `ok:false` + 稳定 `code`，
  绝不吞掉（"落盘失败"必须让上层看得见，否则 agent 会拿到一个不存在的路径去读图）

坐标口径（AGENTS.md）：`scale` **逐窗口取**（出图宽 ÷ 窗口原生宽），绝不用任何全局估算。
驱动 0.28.2 实测**不回报** `screenshot_scale` 字段，所以这个值只能现算（`window_bounds`
与文件宽高都在手上）。
"""

from __future__ import annotations

import base64
import binascii
import os
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..driver.errors import WinUseError
from .state import StateOptions, pick_window

#: 支持的模式（顺序即文档顺序）
MODES = ("window", "desktop", "region")

#: 数据目录下的子目录（与 config.data_dir 同源，见 `default_out_dir`）
SUBDIR = "screenshots"

#: 认不出格式时的兜底扩展名（驱动目前只会回 png / jpeg，见 FORMAT_EXTENSIONS）
EXTENSION = ".png"

#: 实际格式 → 落盘扩展名。**扩展名绝不撒谎**：实测 `zoom` 回的是 JPEG（`ffd8ff`），
#: 而 `get_window_state` / `get_desktop_state` 回 PNG——统一写 .png 就是给上层一个
#: 名不副实的文件（OCR、预览、用户双击都可能因此走错分支）。
FORMAT_EXTENSIONS = {"png": ".png", "jpeg": ".jpg"}

#: 驱动报的 mime → 扩展名（magic bytes 认不出时的兜底）
MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}

#: JPEG 的 magic（SOI + 第一个段标记）
JPEG_SIGNATURE = b"\xff\xd8\xff"

#: JPEG 里承载尺寸的段（SOF0…SOF15，排除 DHT 0xC4 / JPG 0xC8 / DAC 0xCC——它们同区段但不是 SOF）
JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

#: 落盘文件名前缀（只带模式与时间戳，不带窗口身份——数据卫生）
FILE_PREFIX = "screenshot"

#: 原子落盘用的临时后缀（写完 `os.replace`，绝不留下半截 PNG）
PART_SUFFIX = ".part"

#: 图片字节上限（防"驱动回了个不该有的巨物"把内存/磁盘打满）
MAX_IMAGE_BYTES = 64 * 1024 * 1024

#: `zoom` 的区域宽上限——驱动硬限制（实测 600 px 被拒，且回的是**非 JSON 文本**，
#: 经 driver.call 会变成查不出原因的 "驱动输出不是合法 JSON"，所以这里先拦）
MAX_ZOOM_WIDTH = 500

#: 驱动工具名（只有一处出处，避免各门面各写一份字符串）
TOOL_LIST_WINDOWS = "list_windows"
TOOL_WINDOW = "get_window_state"
TOOL_DESKTOP = "get_desktop_state"
TOOL_REGION = "zoom"

#: 模式 → 实际下发的驱动工具
SOURCE_TOOL = {"window": TOOL_WINDOW, "desktop": TOOL_DESKTOP, "region": TOOL_REGION}

#: 驱动回包里承载图片的字段。0.28.2 实测为 `screenshot_png_b64`（整个二进制里
#: 只有这一个 `*_b64` 字段名，window / desktop / zoom 三种回包都用它）；
#: 其余是同类形状的容错别名。
IMAGE_B64_KEYS = ("screenshot_png_b64", "screenshot_b64", "png_b64", "image_b64")

#: 驱动直写文件时的路径字段（`screenshot_out_file` / 省上下文的 CLI 路径）
IMAGE_FILE_KEYS = ("screenshot_file_path", "screenshot_file", "image_path")

#: 驱动侧"为什么没有图"的字段（原样带进 detail，免得对着空回包猜）
IMAGE_ERROR_KEYS = ("screenshot_error",)

#: 驱动回报的图片尺寸字段（仅用于与落盘文件交叉复核）
DRIVER_SIZE_KEYS = (("screenshot_width", "screenshot_height"), ("width", "height"))

#: 支持的 options 键（严格校验：写错键就报，不静默忽略）
SUPPORTED_OPTIONS = ("max_dimension", "allow_minimized", "env_path")

SCALE_NOTE_WINDOW = (
    "scale = 出图宽 ÷ 目标窗口原生宽度（逐窗口取，见 AGENTS.md 坐标口径）。"
    "驱动 0.28.2 不回报 screenshot_scale，所以这个值是现算的；换算坐标必须用它，"
    "不要用任何全局缩放估算值。"
)

SCALE_NOTE_DESKTOP = (
    "scale = 出图宽 ÷ 屏幕宽；1.0 = 真像素不缩放（get_desktop_state 的语义）。"
    "screen_scale_factor 是主屏的 DPI 缩放比，**不是**截图缩放比，两者别混用。"
)

REGION_NOTE = (
    "region 的 x1/y1/x2/y2 是 get_window_state 那张截图的像素空间（图被缩放过也按它算），"
    "不是原生桌面坐标；驱动按「窗口截图空间 ÷ 原生分辨率」的比放大出图，比例**不固定**"
    "（实测同一个 100×100 请求：一次回来 130×130、另一次同窗同矩形回来 417×417，"
    "取决于驱动此刻的截图空间），所以出图尺寸看 width/height 与 region.padded_ratio 的"
    "实测值，别假设固定倍数；zoom 回的是 JPEG（window / desktop 回 PNG），"
    "落盘扩展名因此是 .jpg；区域宽上限 500 px 是驱动硬限制。"
)

TARGET_IGNORED_NOTE = (
    "desktop 模式截的是整屏，传入的 target 被忽略（这里如实标出来，"
    "免得上层以为截的是那个窗口）。"
)

TARGET_REQUIRED_HINT = (
    "驱动 zoom 需要 pid + window_id（坐标是那张窗口截图的像素空间）："
    "请传 target={'pid': <PID>, 'window_id': <HWND>}，或目标选择器 {'app': 'notepad'}"
)

REGION_HINT = '矩形要具名传：region={"x1": 0, "y1": 0, "x2": 100, "y2": 100}（坐标是截图像素）'


class ScreenshotModeUnknown(WinUseError):
    """mode 不在 window|desktop|region 里。"""

    code = "SCREENSHOT_MODE_UNKNOWN"


class ScreenshotTargetRequired(WinUseError):
    """region 模式没给目标窗口（zoom 必须落在某个窗口上）。"""

    code = "SCREENSHOT_TARGET_REQUIRED"


class ScreenshotTargetInvalid(WinUseError):
    """target 形状不对（不是 dict，也不是可识别的选择器）。"""

    code = "SCREENSHOT_TARGET_INVALID"


class ScreenshotRegionInvalid(WinUseError):
    """矩形缺失、非数字、或反了（x2 <= x1）。"""

    code = "SCREENSHOT_REGION_INVALID"


class ScreenshotRegionTooWide(WinUseError):
    """矩形宽超过驱动硬限制 500 px。

    为什么在核心拦：驱动拒这条时回的是**非 JSON 文本**（`Zoom region too wide: …`），
    经 `driver.call` 会变成 `DRIVER_ERROR: 驱动输出不是合法 JSON`——上层完全看不出
    是自己矩形给宽了。
    """

    code = "SCREENSHOT_REGION_TOO_WIDE"


class ScreenshotOptionUnknown(WinUseError):
    """options 里有不认识的键——不静默忽略（写错的键必须当场被看见）。"""

    code = "SCREENSHOT_OPTION_UNKNOWN"


class ScreenshotOptionInvalid(WinUseError):
    """options 不是映射，或取值非法（out_dir 不是路径也算这里）。"""

    code = "SCREENSHOT_OPTION_INVALID"


class ScreenshotNoImage(WinUseError):
    """驱动没回图：字段缺失、驱动明说没截到、或驱动报的落盘文件读不到。"""

    code = "SCREENSHOT_NO_IMAGE"


class ScreenshotImageInvalid(WinUseError):
    """回图不可用：base64 解不出、解出 0 字节、或无法确定尺寸。"""

    code = "SCREENSHOT_IMAGE_INVALID"


class ScreenshotImageTooLarge(WinUseError):
    """图片超过 `MAX_IMAGE_BYTES`（写这么大的文件不是调用方的本意）。"""

    code = "SCREENSHOT_IMAGE_TOO_LARGE"


class ScreenshotWriteFailed(WinUseError):
    """落盘失败（建目录、写文件、原子替换任一步）。

    必须给明确错误码而不是吞掉：上层拿到 `path` 就会去读图，落盘失败还回成功
    等于让 agent 去读一个不存在的文件。
    """

    code = "SCREENSHOT_WRITE_FAILED"


@dataclass(frozen=True)
class ScreenshotSpec:
    """一次截图的完整描述（门面把它翻译成这个，业务逻辑只在这里）。"""

    mode: str
    #: 目标窗口选择器（优先级 window_id > pid > app，与 `state.pick_window` 一致）
    app: str | None = None
    pid: int | None = None
    window_id: int | None = None
    #: region 专用：截图像素空间的矩形 (x1, y1, x2, y2)，已校验与取整
    region: tuple[int, int, int, int] | None = None
    #: window 专用：出图长边上限（缩略图，省的是驱动侧的编码与传输）
    max_dimension: int | None = None
    #: 允许对最小化窗口截图（默认拒绝：最小化窗口的像素没有意义）
    allow_minimized: bool = False
    #: 解析数据目录用的 .env 路径（默认 None = 项目内 .env，见 `default_out_dir`）
    env_path: str | None = None


# --------------------------------------------------------------------------
# 纯函数：入口参数校验（**在下发驱动之前**）
# --------------------------------------------------------------------------


def normalize_mode(mode: Any) -> str:
    """模式归一到规范值（允许大小写与首尾空白）；认不出一律抛。"""
    text = str(mode or "").strip().lower()
    if text not in MODES:
        raise ScreenshotModeUnknown(
            f"不支持的截图模式：{mode}",
            "可用：" + "、".join(MODES) + "（window=一个窗口 / desktop=整屏真像素 / region=区域放大）",
        )
    return text


def _positive_int(value: Any) -> int | None:
    """取正整数；非数字/布尔/<=0 一律 None（宁可不要，也不猜）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if out > 0 else None


def _coord(value: Any, name: str) -> int:
    """矩形的一个边：必须是数字（布尔不算数字——`True` 是 int 子类，最容易蒙混）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScreenshotRegionInvalid(
            f"矩形 {name} 必须是数字",
            f"收到 {type(value).__name__}：{value!r}。{REGION_HINT}",
        )
    return int(round(float(value)))


def parse_region(region: Any) -> tuple[int, int, int, int]:
    """纯函数：矩形入参 → 已校验的 (x1, y1, x2, y2)。

    只认**具名**矩形：`[0, 0, 100, 100]` 这种按下标传太容易错序（错序的图上看着
    "有内容"，但放大的是别的地方），所以直接拒并给出正确写法。
    """
    if not isinstance(region, dict):
        raise ScreenshotRegionInvalid(
            f"region 必须是具名矩形（收到 {type(region).__name__}）",
            REGION_HINT,
        )
    missing = [name for name in ("x1", "y1", "x2", "y2") if region.get(name) is None]
    if missing:
        raise ScreenshotRegionInvalid(
            "矩形缺少边：" + "、".join(missing),
            REGION_HINT,
        )
    x1 = _coord(region["x1"], "x1")
    y1 = _coord(region["y1"], "y1")
    x2 = _coord(region["x2"], "x2")
    y2 = _coord(region["y2"], "y2")
    if x2 <= x1 or y2 <= y1:
        raise ScreenshotRegionInvalid(
            f"矩形无效：x2={x2} 必须 > x1={x1}，y2={y2} 必须 > y1={y1}",
            "反了的矩形驱动会直接报错；坐标是截图像素空间，不是桌面坐标",
        )
    if x2 - x1 > MAX_ZOOM_WIDTH:
        raise ScreenshotRegionTooWide(
            f"矩形宽 {x2 - x1} px 超过驱动上限 {MAX_ZOOM_WIDTH} px",
            "驱动对 zoom 的硬限制是区域宽 ≤ 500 px（实测 600 px 被拒）。"
            "请缩小矩形分次放大，或改用 window 模式取整窗。",
        )
    return x1, y1, x2, y2


def parse_options(options: Any) -> dict:
    """纯函数：options 校验并归一（未知键**报错**，不静默忽略）。"""
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ScreenshotOptionInvalid(
            f"options 必须是映射（收到 {type(options).__name__}）",
            "支持的键：" + "、".join(SUPPORTED_OPTIONS),
        )
    unknown = sorted(str(k) for k in options if k not in SUPPORTED_OPTIONS)
    if unknown:
        raise ScreenshotOptionUnknown(
            "不支持的 options 键：" + "、".join(unknown),
            "支持的键：" + "、".join(SUPPORTED_OPTIONS),
        )

    out: dict[str, Any] = {}
    if "max_dimension" in options:
        max_dimension = _positive_int(options["max_dimension"])
        if max_dimension is None:
            raise ScreenshotOptionInvalid(
                f"max_dimension 必须是正整数（收到 {options['max_dimension']!r}）",
                "它是出图长边的像素上限；0 或负数会被驱动拒绝，也别用字符串",
            )
        out["max_dimension"] = max_dimension
    if "allow_minimized" in options:
        value = options["allow_minimized"]
        if not isinstance(value, bool):
            raise ScreenshotOptionInvalid(
                f"allow_minimized 必须是布尔（收到 {type(value).__name__}）",
                "请用 true / false，不要用 1/0 或 \"yes\"（bool 是 int 子类，最容易蒙混）",
            )
        out["allow_minimized"] = value
    if "env_path" in options and options["env_path"] is not None:
        out["env_path"] = str(options["env_path"])
    return out


def _target_selectors(target: Any) -> dict:
    """纯函数：target → 选择器字段（只认 dict；顺序语义交给 `state.pick_window`）。"""
    if target is None:
        return {}
    if not isinstance(target, dict):
        raise ScreenshotTargetInvalid(
            f"target 必须是映射（收到 {type(target).__name__}）",
            "例如 {'window_id': <HWND>} / {'pid': <PID>} / {'app': 'notepad'}；"
            "都不给则用系统前台窗口",
        )
    return {
        "app": target.get("app"),
        "pid": target.get("pid"),
        "window_id": target.get("window_id"),
    }


def has_selectors(selectors: dict) -> bool:
    """选择器里**真的有**一个非 None 值？

    为什么要单独判：门面常写成 `{"window_id": args.get("window_id")}`——键一直在，
    值是 None。只看"有没有键"会把"没给目标"误判成"给了目标"，region 模式就会
    **静默**落到系统前台窗口上（截到了别的窗口，还看不出哪里错了）。
    """
    return any(selectors.get(name) is not None for name in ("app", "pid", "window_id"))


def build_spec(mode: Any, target: Any, region: Any, opts: dict) -> ScreenshotSpec:
    """纯函数：入口参数 → `ScreenshotSpec`（校验全在这里，不下发任何调用）。"""
    normalized = normalize_mode(mode)
    selectors = _target_selectors(target)
    if normalized == "region" and not has_selectors(selectors):
        raise ScreenshotTargetRequired(
            "region 模式必须给出目标窗口",
            TARGET_REQUIRED_HINT,
        )
    rect = parse_region(region) if normalized == "region" else None
    return ScreenshotSpec(
        mode=normalized,
        region=rect,
        max_dimension=opts.get("max_dimension"),
        allow_minimized=bool(opts.get("allow_minimized", False)),
        env_path=opts.get("env_path"),
        **selectors,
    )


# --------------------------------------------------------------------------
# 纯函数：落盘目录 / 图片解析
# --------------------------------------------------------------------------


def default_out_dir(env_path: str | Path | None = None) -> Path:
    """默认落盘目录 = 配置的数据目录下的 `screenshots/`（与 `config.data_dir` 同源）。

    数据目录的解析**只有一份出处**（`config.Config.load`：`.env` 的 `WINUSE_DATA_DIR`
    → 默认 `~/.winuse`），这里不另写一套口径。`env_path` 只为显式指定/测试服务。
    """
    from ..config import Config  # 延迟导入：避免 core ← config 的导入时序耦合

    return Path(Config.load(env_path=env_path).data_dir) / SUBDIR


def png_size(data: bytes) -> tuple[int, int] | None:
    """纯函数：PNG 字节 → (宽, 高)；不是 PNG（或缺 IHDR）给 None。

    为什么自己解图片头（而不是信驱动报的宽高）：回包里"报的尺寸"和"真发出去的图"
    是两件事，前者错了坐标换算会静默错。文件在手上，就以文件为准——只读签名 + IHDR，
    纯 stdlib 的几行。
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 24:
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", bytes(data[16:24]))
    return (width, height) if width > 0 and height > 0 else None


def image_format(data: Any) -> str | None:
    """纯函数：magic bytes → 格式名（`png` / `jpeg`）；认不出给 None。

    为什么看文件头而不是信 mime 字段：实测 `zoom` 回 JPEG、窗口/整屏回 PNG，
    而两者都叫 `screenshot_png_b64`（驱动字段名就这么写的）。跟着字段名或 mime
    推断格式，一旦漂移就是"文件改了名还是错格式"——文件头不会骗人。
    """
    if not isinstance(data, (bytes, bytearray)):
        return None
    if bytes(data[:8]) == b"\x89PNG\r\n\x1a\n":
        return "png"
    if bytes(data[:3]) == JPEG_SIGNATURE:
        return "jpeg"
    return None


def jpeg_size(data: Any) -> tuple[int, int] | None:
    """纯函数：JPEG 字节 → (宽, 高)（顺序扫段找 SOF）；读不到给 None。"""
    view = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
    index = 2  # 跳过 SOI
    limit = len(view)
    while index + 4 <= limit:
        if view[index] != 0xFF:
            index += 1
            continue
        marker = view[index + 1]
        # 填充 FF 与无长度段（RSTn / TEM / SOI / EOI）不参与长度推进
        if marker == 0xFF:
            index += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            index += 2
            continue
        length = int.from_bytes(view[index + 2 : index + 4], "big")
        if length < 2:
            return None
        if marker in JPEG_SOF_MARKERS:
            if index + 9 > limit:
                return None
            height = int.from_bytes(view[index + 5 : index + 7], "big")
            width = int.from_bytes(view[index + 7 : index + 9], "big")
            return (width, height) if width > 0 and height > 0 else None
        index += 2 + length
    return None


def image_size(data: Any) -> tuple[int, int] | None:
    """纯函数：图片字节 → (宽, 高)（PNG 头 / JPEG SOF）；认不出给 None。"""
    fmt = image_format(data)
    if fmt == "png":
        return png_size(data)
    if fmt == "jpeg":
        return jpeg_size(data)
    return None


def image_extension(fmt: str | None, raw: Any) -> str:
    """纯函数：实际格式（优先）→ 驱动报的 mime → 兜底 .png。

    三级顺序的理由：格式看得见就以文件为准；认不出才退回驱动自报的 mime；
    都没有（比如驱动哪天少字段）才用 .png 兜底——宁可扩展名含糊，也不写错格式。
    """
    if fmt in FORMAT_EXTENSIONS:
        return FORMAT_EXTENSIONS[fmt]
    mime = ""
    if isinstance(raw, dict):
        mime = str(raw.get("screenshot_mime_type") or raw.get("mime_type") or "").strip().lower()
    return MIME_EXTENSIONS.get(mime, EXTENSION)


def image_mime(fmt: str | None, raw: Any) -> str:
    """纯函数：对外报的 mime（同样以文件为准，其次是驱动自报）。"""
    if fmt:
        return f"image/{fmt}"
    if isinstance(raw, dict):
        mime = str(raw.get("screenshot_mime_type") or raw.get("mime_type") or "").strip().lower()
        if mime:
            return mime
    return "image/png"


def decode_image_b64(text: str) -> bytes:
    """纯函数：base64（含 `data:` URL 前缀）→ 图片字节；解不出抛 `ScreenshotImageInvalid`。"""
    payload = str(text).strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ScreenshotImageInvalid(
            "驱动回的截图不是合法 base64",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    if not data:
        raise ScreenshotImageInvalid(
            "驱动回的截图解码后是空字节",
            "字段存在但没有内容——空图不是图，不能当成功",
        )
    return data


def extract_image(raw: Any) -> tuple[bytes, dict]:
    """驱动回包 → (图片字节, 来源说明)。取不到就抛 `ScreenshotNoImage`。"""
    raw = raw if isinstance(raw, dict) else {}

    for key in IMAGE_B64_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return decode_image_b64(value), {"image_source": key}

    for key in IMAGE_FILE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ScreenshotNoImage(
                    "驱动说图写到了文件，但那个文件读不到",
                    f"{path.name}：{type(exc).__name__}: {str(exc)[:120]}",
                ) from exc
            if not data:
                raise ScreenshotImageInvalid(
                    f"驱动写的文件是空的：{path.name}",
                    "空文件不是图，不能当成功",
                )
            return data, {"image_source": key}

    raise ScreenshotNoImage("驱动没有回图", _no_image_detail(raw))


def _no_image_detail(raw: dict) -> str:
    """把"为什么没图"的线索凑齐：驱动明说的原因 > has_screenshot=false > 有哪些键。"""
    hints: list[str] = []
    for key in IMAGE_ERROR_KEYS:
        value = raw.get(key)
        if value:
            hints.append(f"{key}={value}")
    if raw.get("has_screenshot") is False:
        hints.append("has_screenshot=false")
    if not raw:
        hints.append("驱动回包是空的")
    elif not hints:
        keys = "、".join(sorted(str(k) for k in raw)[:12])
        hints.append(f"回包里既没有图片字段也没有错误字段；实际字段：{keys}")
    return "；".join(hints)[:400]


def _driver_size(raw: Any) -> tuple[int | None, int | None]:
    """驱动回包自己报的图片尺寸（只用于交叉复核）。"""
    if not isinstance(raw, dict):
        return None, None
    for width_key, height_key in DRIVER_SIZE_KEYS:
        width = _positive_int(raw.get(width_key))
        height = _positive_int(raw.get(height_key))
        if width and height:
            return width, height
    return None, None


def image_scale(width: int | None, native_width: Any) -> float | None:
    """纯函数：出图宽 ÷ 原生宽（逐窗口/逐屏取）。原生宽不可信时给 None，不猜。"""
    native = _positive_int(native_width)
    if not width or not native:
        return None
    return round(width / native, 4)


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


def _now_stamp() -> str:
    """文件名里的时间戳（本地时间，微秒精度：同一秒内连续截图也不撞名）。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """挑一个还不存在的文件名——**绝不覆盖**已存在的截图（证据不能互相顶掉）。"""
    candidate = directory / f"{stem}{suffix}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def save_image(image: bytes, dest_dir: Path, mode: str, suffix: str = EXTENSION) -> Path:
    """把图片落到 `dest_dir`，返回最终路径（失败抛 `ScreenshotWriteFailed`）。

    先写 `<名字>.part` 再 `os.replace`：看到图片文件就是完整的图，不存在"半截图
    被上层读走"。文件名只含模式与时间戳——窗口标题/应用名/路径都不进文件名；
    `suffix` 由实际格式决定（见 `image_extension`），不含任何用户信息。
    """
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScreenshotWriteFailed(
            f"无法创建截图目录：{dest_dir}",
            f"{type(exc).__name__}: {str(exc)[:200]}",
        ) from exc
    if not dest_dir.is_dir():
        raise ScreenshotWriteFailed(
            f"截图落盘目录不可用：{dest_dir}",
            "该路径存在但不是目录（同名文件占位？）",
        )

    final = _unique_path(dest_dir, f"{FILE_PREFIX}-{mode}-{_now_stamp()}", suffix)
    partial = final.with_name(final.name + PART_SUFFIX)
    try:
        partial.write_bytes(image)
        os.replace(partial, final)
    except OSError as exc:
        try:  # 尽力清理：失败路径不留临时文件（清理失败也不该掩盖原始错误）
            partial.unlink()
        except OSError:
            pass
        raise ScreenshotWriteFailed(
            f"截图落盘失败：{final.name}",
            f"{type(exc).__name__}: {str(exc)[:200]}",
        ) from exc
    return final


# --------------------------------------------------------------------------
# 驱动调用
# --------------------------------------------------------------------------


def _resolve_target(driver, spec: ScreenshotSpec) -> dict:
    """选目标窗口：复用 `state.pick_window`（选择语义只有一份，不复制）。"""
    resp = driver.call(TOOL_LIST_WINDOWS, {})
    windows = (resp.get("windows") if isinstance(resp, dict) else resp) or []
    return pick_window(
        windows,
        StateOptions(
            app=spec.app,
            pid=spec.pid,
            window_id=spec.window_id,
            allow_minimized=spec.allow_minimized,
        ),
    )


def _slim_target(target: dict) -> dict:
    """对外目标信息（与 `state._slim_window` 同字段，避免两套口径）。"""
    return {
        "app_name": target.get("app_name"),
        "pid": target.get("pid"),
        "window_id": target.get("window_id"),
        "title": target.get("title"),
        "bounds": target.get("bounds"),
        "minimized": target.get("minimized"),
    }


def _call_driver(driver, spec: ScreenshotSpec, target: dict | None) -> dict:
    """下发这一次截图调用（desktop 不需要目标窗口，故不枚举窗口）。

    驱动从不自己挑窗口（`get_window_state` 的 window_id 必须属于 pid），所以
    window / region 的 pid、window_id 一律来自 `_resolve_target` 的解析结果。
    """
    args: dict[str, Any] = {}
    if spec.mode != "desktop":
        target = target or {}
        args = {"pid": target.get("pid"), "window_id": target.get("window_id")}
        if spec.mode == "window":
            # 纯截图路径：关掉 UIA 树。真机实测不带这个开关会白拉一份几百到 5000 元素的树
            args["include_screenshot"] = True
            args["include_accessibility_tree"] = False
            if spec.max_dimension:
                args["max_dimension"] = spec.max_dimension
        else:
            x1, y1, x2, y2 = spec.region or (0, 0, 0, 0)
            args.update({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
    resp = driver.call(SOURCE_TOOL[spec.mode], args)
    return resp if isinstance(resp, dict) else {}


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def _failure(
    spec: ScreenshotSpec,
    exc: WinUseError,
    *,
    target: dict | None = None,
    out_dir: Path | None = None,
) -> dict:
    """失败结构：与成功结构同族（键位一致），带上稳定 code 供上层分支。"""
    out: dict[str, Any] = {
        "ok": False,
        "mode": spec.mode,
        "path": None,
        "width": None,
        "height": None,
        "bytes": None,
        "scale": None,
        "target": target,
        "out_dir": str(out_dir) if out_dir is not None else None,
        "code": exc.code,
        "error": exc.to_dict(),
    }
    if spec.region is not None:
        out["region"] = _region_report(spec.region, None)
    return out


def _region_report(rect: tuple[int, int, int, int], image: tuple[int, int] | None) -> dict:
    """region 回声：请求的矩形 + 出图相对请求的放大倍数（padding 的实测痕迹）。"""
    x1, y1, x2, y2 = rect
    report: dict[str, Any] = {
        "requested": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
        "requested_size": {"width": x2 - x1, "height": y2 - y1},
    }
    if image and image[0] and image[1]:
        report["padded_ratio"] = round(image[0] / max(1, x2 - x1), 4)
    return report


def _resolve_out_dir(out_dir: Any, env_path: str | None) -> Path:
    """落盘目录：显式 out_dir 优先，否则用配置的数据目录下的 `screenshots/`。"""
    if out_dir is None or out_dir == "":
        return default_out_dir(env_path)
    try:
        path = Path(out_dir).expanduser()
    except TypeError as exc:
        raise ScreenshotOptionInvalid(
            f"out_dir 必须是路径（收到 {type(out_dir).__name__}）",
            "例如 out_dir='D:/shots'；留空则用配置的数据目录下的 screenshots/",
        ) from exc
    return path if path.is_absolute() else Path(os.path.abspath(str(path)))


def run_screenshot(
    driver,
    *,
    mode: str,
    target: dict | None = None,
    out_dir: str | Path | None = None,
    region: dict | None = None,
    options: dict | None = None,
) -> dict:
    """截一张图并落到数据目录，回路径与尺寸（**不下发 base64**）。

    返回（成功）：

        {"ok": true, "mode": "window|desktop|region",
         "path": "…/screenshots/screenshot-window-20260927-165733-123456.png",
         "width": 1080, "height": 1085, "bytes": 194722, "scale": 0.9982,
         "mime": "image/png", "image_source": "screenshot_png_b64",
         "source_tool": "get_window_state", "out_dir": "…/screenshots",
         "target": {"app_name": …, "pid": …, "window_id": …, "title": …, "bounds": …, "minimized": …},
         "scale_note": "…"}

    `desktop` 模式额外带 `screen_size` / `screen_scale_factor`（target 为 null）；
    `region` 模式额外带 `region`（请求矩形 + `padded_ratio`）与 `region_note`；
    驱动报的尺寸与落盘文件不一致时带 `size_mismatch` / `driver_reported_size` / `size_note`。

    失败（驱动报错 / 没回图 / 图坏了 / 落盘失败）不抛异常，统一收口成：

        {"ok": false, "mode": …, "path": null, "width": null, "height": null,
         "bytes": null, "code": "SCREENSHOT_NO_IMAGE", "error": {…}}

    **调用方错误**（模式名 / 矩形 / options / out_dir 形状）直接抛 `WinUseError` 子类，
    且在**下发任何驱动调用之前**——参数错就别浪费一次驱动，也别让上层以为"碰过桌面"。
    """
    opts = parse_options(options)
    spec = build_spec(mode, target, region, opts)
    dest_dir = _resolve_out_dir(out_dir, spec.env_path)

    target_info: dict | None = None
    target_window: dict | None = None
    try:
        if spec.mode != "desktop":
            target_window = _resolve_target(driver, spec)
            target_info = _slim_target(target_window)
        raw = _call_driver(driver, spec, target_window)
        image, meta = extract_image(raw)
        if len(image) > MAX_IMAGE_BYTES:
            raise ScreenshotImageTooLarge(
                f"驱动回的截图 {len(image)} 字节超过上限 {MAX_IMAGE_BYTES} 字节",
                "这么大的图不该走截图出口：先缩小范围（region / max_dimension）再取",
            )
        fmt = image_format(image)
        size = image_size(image)
        driver_size = _driver_size(raw)
        if size is None and driver_size[0] and driver_size[1]:
            # 认不出的格式（驱动目前只会回 png / jpeg）：退回驱动报的尺寸，别吞掉这张图
            size = (driver_size[0], driver_size[1])
        if size is None:
            raise ScreenshotImageInvalid(
                "无法确定图片尺寸（既不是 PNG/JPEG 头，驱动也没报宽高）",
                "坐标换算依赖宽高，宁可报错也不瞎给一个数",
            )
        width, height = size
        path = save_image(image, dest_dir, spec.mode, image_extension(fmt, raw))
    except WinUseError as exc:
        return _failure(spec, exc, target=target_info, out_dir=dest_dir)

    out: dict[str, Any] = {
        "ok": True,
        "mode": spec.mode,
        "path": str(path),
        "width": width,
        "height": height,
        "bytes": len(image),
        "scale": None,
        "mime": image_mime(fmt, raw),
        "image_source": meta.get("image_source"),
        "source_tool": SOURCE_TOOL[spec.mode],
        "out_dir": str(dest_dir),
        "target": target_info,
    }

    if spec.mode == "window":
        out["scale"] = image_scale(width, (target_info or {}).get("bounds", {}).get("width"))
        out["scale_note"] = SCALE_NOTE_WINDOW
    elif spec.mode == "desktop":
        screen_w = _positive_int(raw.get("screen_width"))
        screen_h = _positive_int(raw.get("screen_height"))
        out["scale"] = image_scale(width, screen_w)
        out["scale_note"] = SCALE_NOTE_DESKTOP
        out["screen_size"] = {"width": screen_w, "height": screen_h}
        out["screen_scale_factor"] = raw.get("scale_factor")
        if target is not None:
            out["target_note"] = TARGET_IGNORED_NOTE
    else:
        out["region"] = _region_report(spec.region or (0, 0, 0, 0), size)
        out["region_note"] = REGION_NOTE

    if driver_size[0] and driver_size[1] and driver_size != (width, height):
        out["size_mismatch"] = True
        out["driver_reported_size"] = {"width": driver_size[0], "height": driver_size[1]}
        out["size_note"] = (
            f"驱动报 {driver_size[0]}×{driver_size[1]}，落盘文件实际 {width}×{height}"
            "（以文件为准）；坐标系对不上时先查这里"
        )
    return out
