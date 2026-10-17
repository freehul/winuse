"""winuse_ocr：本地图片文字识别（Windows.Media.Ocr / WinRT，零第三方依赖）。

为什么要绕 PowerShell 而不是 ctypes：`Windows.Media.Ocr` 是 WinRT 组件，
Python 标准库**没有** WinRT 投影（`winrt` / `winsdk` 都是第三方包，违反本项目
「零第三方依赖」硬约束）。Windows 自带的 Windows PowerShell 5.1 内建 WinRT 投影，
经它调 `OcrEngine` 是唯一的零依赖路径。
注意必须是 5.1（`powershell.exe`）：PowerShell 7（`pwsh.exe`）基于 .NET Core，
不带内建 WinRT 投影，**不能**当替代品。

三条关键做法：
1. **构造与解析分离**：`build_ocr_script` / `build_ocr_command` 只构造命令（纯函数），
   `parse_ocr_output` 只解析引擎输出（纯函数）；真跑经可注入的 `runner`，
   单测因此完全不依赖本机是否装了语言包。
2. **`-EncodedCommand` 杜绝转义地狱**：脚本以 UTF-16LE base64 下发，图片路径与语言
   再各自 base64 一次、在脚本内解码——路径里的引号、空格、中文、`$` 都不会被
   shell 或 PowerShell 二次解释（这是注入面，不能只做"引号转义"）。
3. **引擎不可用必须显式报错**：WinRT 组件缺失 / 语言包未装 / PowerShell 调用失败，
   一律给出 `code=OCR_ENGINE_UNAVAILABLE`，绝不返回空串假装识别成功——
   空文本只有一种合法来源：引擎真的跑了，而图上确实没有文字。

实测备注（真机跑通后的观察，供上层取舍）：WinRT 的 `OcrResult.Text` 会在**汉字之间插空格**
（"发票号码" 回来是 "发 票 号 码"），这是引擎自身行为，不是解析问题。
于是结果里额外给一个**紧凑文本**字段 `compact_text`（Backlog T-24，别名 `join_words`）：
按 `compact_ocr_text` 的规则逐行抹掉引擎自插的空格（行边界取自 `lines`，不是 `Text`），
`text` / `lines` / `words` 仍是引擎原样输出，一个字节都不动，需要判读原始分词时继续用它们。
词级 `bounds` 是图片内的**像素**坐标，与屏幕坐标无关，不可直接当点击目标。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from .. import limit as limit_mod
from ..driver.errors import WinUseError

#: 输出标记：引擎输出里唯一可信的一行（前面可能有 PS 的警告/进度噪音）
OUTPUT_MARKER = "WINUSE_OCR_JSON:"

#: 只用 Windows PowerShell 5.1（原因见模块 docstring）
POWERSHELL_EXE = "powershell.exe"

#: 识别整张图的上限时间（秒）；OCR 是 CPU 密集，给足但别无限等
OCR_TIMEOUT_SECONDS = 60

#: `runner(argv) -> (returncode, stdout, stderr)`，可注入以便单测与替换执行方式
Runner = Callable[[list[str]], "tuple[int, str, str]"]
Which = Callable[[str], "str | None"]


class OcrInputMissing(WinUseError):
    """调用方没给图片路径——编程错误，直接抛。"""

    code = "OCR_INPUT_MISSING"


class OcrImageNotFound(WinUseError):
    """图片文件不存在或不是普通文件。本期只支持本地图片文件。"""

    code = "OCR_IMAGE_NOT_FOUND"


class OcrEngineUnavailable(WinUseError):
    """OCR 引擎不可用：WinRT 组件缺失、语言包未安装、PowerShell 调不通。

    绝不降级成"返回空文本"——那会让上层把"没识别"当成"图上没字"。
    """

    code = "OCR_ENGINE_UNAVAILABLE"


class OcrOutputInvalid(WinUseError):
    """引擎有输出但不可解析：标记行不是合法 JSON（不是市场噪音，是真的坏了）。"""

    code = "OCR_OUTPUT_INVALID"


class OcrRunFailed(WinUseError):
    """引擎跑起来了但执行失败（图片损坏、解码失败等）——**不是**引擎不可用，别混成一个码。"""

    code = "OCR_RUN_FAILED"


class OcrImageTooLarge(WinUseError):
    """图片超过大小上限（Optional-2）。

    为什么要有：图片内容由脚本宿主侧读取，超大图会让 OCR 引擎吃满内存或直接超时
    ——表现是"OCR 莫名其妙卡住"，排查时完全看不出原因。提前拒掉比事后超时清楚得多。
    """

    code = "OCR_IMAGE_TOO_LARGE"


class OcrImageUnsupported(WinUseError):
    """扩展名不在白名单里（Optional-2）。"""

    code = "OCR_IMAGE_UNSUPPORTED"


@dataclass
class OcrSpec:
    """一次识别的输入（门面把它翻译成这个，业务逻辑只在这里）。"""

    #: 本地图片文件路径（本期只支持本地文件，不接 URL / 剪贴板 / 屏幕区域）
    image_path: str = ""
    #: BCP-47 语言标记；zh-Hans 对应简体中文识别器
    language: str = "zh-Hans"


# --------------------------------------------------------------------------
# 纯函数：构造命令
# --------------------------------------------------------------------------

_OCR_SCRIPT_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await-Op($operation, $resultType) {
  $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($operation))
  $task.Wait(-1) | Out-Null
  return $task.Result
}
function Write-WinUseResult($payload) {
  Write-Output ('WINUSE_OCR_JSON:' + ($payload | ConvertTo-Json -Depth 10 -Compress))
}
try {
  $language = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__LANGUAGE_B64__'))
  $path = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__IMAGE_B64__'))
  $path = [System.IO.Path]::GetFullPath($path.Replace('/', '\'))
  $engine = $null
  try {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language -ArgumentList $language))
  } catch { $engine = $null }
  if ($null -eq $engine) {
    $available = @()
    try { $available = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }) } catch { }
    Write-WinUseResult @{ ok = $false; code = 'OCR_ENGINE_UNAVAILABLE'; message = ('OCR 引擎无法按语言 ' + $language + ' 创建（语言包未安装或 WinRT 不可用）'); languages = $available }
    exit 0
  }
  $file = Await-Op ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
  $stream = Await-Op ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await-Op ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-Op ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await-Op ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $lines = @()
  foreach ($line in $result.Lines) {
    $words = @()
    foreach ($word in $line.Words) {
      $rect = $word.BoundingRect
      $words += [ordered]@{ text = "$($word.Text)"; bounds = [ordered]@{ x = [int]$rect.X; y = [int]$rect.Y; width = [int]$rect.Width; height = [int]$rect.Height } }
    }
    $lines += [ordered]@{ text = "$($line.Text)"; words = @($words) }
  }
  Write-WinUseResult ([ordered]@{ ok = $true; text = "$($result.Text)"; lines = @($lines) })
} catch {
  Write-WinUseResult @{ ok = $false; code = 'OCR_RUN_FAILED'; message = "$($_.Exception.Message)" }
}
"""


def _b64_utf8(text: str) -> str:
    """UTF-8 → base64（纯函数）。用于把外部字符串送进脚本而不经过任何引号解析。"""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def build_ocr_script(spec: OcrSpec) -> str:
    """纯函数：生成调用 WinRT OCR 的 PowerShell 脚本文本。"""
    return _OCR_SCRIPT_TEMPLATE.replace("__LANGUAGE_B64__", _b64_utf8(spec.language)).replace(
        "__IMAGE_B64__", _b64_utf8(spec.image_path)
    )


def encode_powershell(script: str) -> str:
    """纯函数：脚本 → `-EncodedCommand` 参数值（UTF-16LE base64，powershell.exe 的官方口径）。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def build_ocr_command(spec: OcrSpec) -> list[str]:
    """纯函数：`OcrSpec` → 下发给 subprocess 的 argv。

    用 argv 列表 + `-EncodedCommand`：脚本本体不过命令行解析器，
    所以路径里的引号/中文/`;` 都不构成注入面。
    """
    return [
        POWERSHELL_EXE,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encode_powershell(build_ocr_script(spec)),
    ]


# --------------------------------------------------------------------------
# 纯函数：解析引擎输出
# --------------------------------------------------------------------------


def _as_list(value: Any) -> list:
    """归一成列表：PS 5.1 的 ConvertTo-Json 把**单元素数组**序列化成对象（不是数组），
    这是实测踩到的坑，解析端必须兜住，否则只有一行文字的图会被判成无结构输出。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _int_at(bounds: dict, key: str) -> int:
    """取整数坐标；缺字段或不是数字时给 0，不抛（坐标缺失不该毁掉整段文本）。"""
    try:
        return int(round(float(bounds.get(key, 0))))
    except (TypeError, ValueError):
        return 0


def _norm_word(word: Any) -> dict:
    word = word if isinstance(word, dict) else {}
    bounds = word.get("bounds") if isinstance(word.get("bounds"), dict) else {}
    return {
        "text": str(word.get("text") or ""),
        "bounds": {
            "x": _int_at(bounds, "x"),
            "y": _int_at(bounds, "y"),
            "width": _int_at(bounds, "width"),
            "height": _int_at(bounds, "height"),
        },
    }


def _norm_line(line: Any) -> dict:
    line = line if isinstance(line, dict) else {}
    return {
        "text": str(line.get("text") or ""),
        "words": [_norm_word(w) for w in _as_list(line.get("words")) if isinstance(w, dict)],
    }


# --------------------------------------------------------------------------
# 纯函数：紧凑文本（T-24：抹掉引擎自己插进去的空格）
# --------------------------------------------------------------------------

#: 引擎会在这类字符之间插空格（它按"词"切分，中文几乎每字一个词）：
#: 汉字（含扩展 A 与兼容区）、假名、CJK 部首、々/〇
_CJK_CLASS = (
    "\u2e80-\u2eff"
    "\u3005\u3007"
    "\u3040-\u30ff"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
)

#: CJK / 全角标点：全角标点自带字面间距，源文里不会在它两侧留半角空格
_CJK_PUNCT_CLASS = "\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"

#: 数字（含全角）——引擎会把一个数字拆成多段，段间用空格隔开
_DIGIT_CLASS = "0-9\uff10-\uff19"

#: 空白段（**不含**换行：换行是行结构，不是引擎插的空格，必须原样留着）
_SPACE_RUN_RE = re.compile(r"[ \t\u3000\v\f]+")
_CJK_RE = re.compile(rf"[{_CJK_CLASS}]")
_CJK_PUNCT_RE = re.compile(rf"[{_CJK_PUNCT_CLASS}]")
_DIGIT_RE = re.compile(rf"[{_DIGIT_CLASS}]")

#: 数字被拆段时，至少有一段很短（`12345` 被拆成 `1` + `2345`）。
#: 两侧都 ≥4 位更像两个独立的数（`2020 2021`），宁可不粘也不要粘错——粘错是不可逆的。
_MAX_SPLIT_DIGITS = 3


def _digit_run(line: str, index: int, step: int) -> int:
    """从 `index` 沿 `step` 方向数连续数字个数（`index` 指向该数字本身）。"""
    count = 0
    i = index
    while 0 <= i < len(line) and _DIGIT_RE.match(line[i]):
        count += 1
        i += step
    return count


def _space_is_artifact(line: str, start: int, end: int, left: str, right: str) -> bool:
    """判断 `line[start:end]` 这段空白是不是引擎自插的（是 → 删掉，不是 → 压成一个空格）。

    只有三种情况算自插：
    1. 两侧都是汉字/假名——引擎逐字切词的产物（`发 票 号 码`）；
    2. 任一侧是 CJK/全角标点——全角标点自带间距（`号码 ：`）；
    3. 两侧都是数字且至少一侧不足 4 位——一个数字被拆段（`1 2345`）。
    其余一律保留一个半角空格：拉丁词与词之间（`WinUse OCR`）、中英之间、数与单位之间
    的空格在源文里是真实存在的，删了就是把两样东西粘成一样。
    """
    if _CJK_RE.match(left) and _CJK_RE.match(right):
        return True
    if _CJK_PUNCT_RE.match(left) or _CJK_PUNCT_RE.match(right):
        return True
    if _DIGIT_RE.match(left) and _DIGIT_RE.match(right):
        left_run = _digit_run(line, start - 1, -1)
        right_run = _digit_run(line, end, +1)
        return min(left_run, right_run) <= _MAX_SPLIT_DIGITS
    return False


def _compact_line(line: str) -> str:
    """单行紧凑化：删掉自插空白段，其余空白段压成一个半角空格。"""
    out: list[str] = []
    i = 0
    length = len(line)
    while i < length:
        match = _SPACE_RUN_RE.match(line, i)
        if match is None:
            out.append(line[i])
            i += 1
            continue
        end = match.end()
        left = line[i - 1] if i > 0 else ""
        right = line[end] if end < length else ""
        if not (left and right) or not _space_is_artifact(line, i, end, left, right):
            out.append(" ")
        i = end
    return "".join(out)


def compact_ocr_text(text: str | None) -> str:
    """纯函数：OCR 文本 → 紧凑文本（T-24）。

    WinRT 的 `Text` 会把一个汉字段落切成"每字一词"再用空格拼回去，直接拿它做
    子串匹配 / 断言 / 填表一律失配（真机：源图 `WinUse OCR 验收 12345`，
    引擎给 `WinUse OCR 验 收 1 2345`）。本函数按 `_space_is_artifact` 的规则
    只删引擎自插的空格，**行结构（`\\n`）原样保留**，每行首尾空白去掉。

    这是纯字符串处理，不依赖引擎、不猜字符：删不掉的信息仍然在 `lines`/`words` 里。
    幂等：`compact_ocr_text(compact_ocr_text(s)) == compact_ocr_text(s)`。
    `None`（引擎 JSON 里的 null）与空串都归一成 `""`，不抛异常——上层不该为"图上没字"写分支。

    传整段文本时行结构按 `\\n` 保留；`parse_ocr_output` 是**逐行**调用它、再按 `lines`
    拼回去的（真机上 `Text` 会把多行粘成一行，行边界只在 `lines` 里，见那里注释）。
    """
    if not text:
        return ""
    return "\n".join(_compact_line(line).strip() for line in str(text).split("\n"))


def parse_ocr_output(raw: str, language: str) -> dict:
    """纯函数：引擎 stdout → 对外结构。

    只认标记行（`OUTPUT_MARKER` 之后的部分），PS 的警告噪音一律忽略；
    取**最后**一条标记行，避免前面的杂音抢先。
    引擎报错（ok:false）与"根本没输出标记"都抛 `OcrEngineUnavailable`；
    标记行不是合法 JSON 抛 `OcrOutputInvalid`——两类故障原因不同，不能混成一个码。
    """
    payload: dict | None = None
    bad_json: str | None = None

    for line in (raw or "").splitlines():
        if OUTPUT_MARKER not in line:
            continue
        tail = line.split(OUTPUT_MARKER, 1)[1].strip()
        try:
            parsed = json.loads(tail)
        except json.JSONDecodeError:
            bad_json = tail[:200]
            payload = None
            continue
        bad_json = None
        if isinstance(parsed, dict):
            payload = parsed

    if payload is None:
        if bad_json is not None:
            raise OcrOutputInvalid(
                "OCR 引擎输出了标记行但不是合法 JSON",
                f"原始内容片段：{bad_json}",
            )
        raise OcrEngineUnavailable(
            "OCR 引擎没有产出任何可解析结果（未见输出标记）",
            "常见原因：powershell.exe 不可用，或 PowerShell 7 被当成 5.1 使用"
            "（pwsh 7 无内建 WinRT 投影）。原始输出尾部：" + (raw or "").strip()[-200:],
        )

    if not payload.get("ok", False):
        code = str(payload.get("code") or "OCR_ENGINE_UNAVAILABLE")
        detail = str(payload.get("message") or "")
        langs = payload.get("languages")
        if isinstance(langs, list) and langs:
            detail += "；本机已安装的识别语言：" + "、".join(str(x) for x in langs)
        if code == OcrEngineUnavailable.code:
            raise OcrEngineUnavailable("OCR 引擎不可用", detail)
        # 引擎在跑，但这一步失败了（图片损坏等）——如实报，不冒充"引擎不可用"
        raise OcrRunFailed(f"OCR 执行失败（{code}）：{payload.get('message') or ''}", detail)

    lines = [_norm_line(ln) for ln in _as_list(payload.get("lines")) if isinstance(ln, dict)]
    text = payload.get("text")
    if not isinstance(text, str):
        # 兜底：引擎没给整段文本时，按行拼（不丢内容，也不编内容）
        text = "\n".join(ln["text"] for ln in lines)

    compact = (
        # 真机观察：`Text` 会把多行用空格粘成一行（两行样张回来是一行 + 一个空格），
        # 真正的行边界只在 `lines` 里。紧凑文本按行拼，绝不跨行粘——行是信息，不是空格。
        "\n".join(compact_ocr_text(ln["text"]) for ln in lines)
        if lines
        else compact_ocr_text(text)
    )

    return {
        "ok": True,
        "engine": "winrt-ocr",
        "language": language,
        # 引擎原样输出：既有字段语义不变，判读原始分词靠它
        "text": text,
        # 紧凑文本（T-24）：抹掉引擎自插的空格，可直接拿去做匹配/断言
        "compact_text": compact,
        # Backlog T-24 里的命名，与 `compact_text` 是同一个值（别名，不是另一套口径）
        "join_words": compact,
        "lines": lines,
    }


# --------------------------------------------------------------------------
# 真跑
# --------------------------------------------------------------------------


def has_powershell(which: Which | None = None) -> bool:
    """本机是否有 Windows PowerShell 5.1（可注入 which 以便单测）。"""
    probe = which or shutil.which
    return bool(probe(POWERSHELL_EXE))


def default_runner(argv: list[str]) -> tuple[int, str, str]:
    """默认 runner：真起 powershell.exe。以 utf-8 读输出（脚本内已把控制台设为 UTF-8）。"""
    proc = subprocess.run(  # noqa: S603 - argv 由本模块构造，无 shell
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=OCR_TIMEOUT_SECONDS,
    )
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def _failure(code: str, message: str, detail: str = "") -> dict:
    """失败结构：与成功结构同族，带上稳定 code 供上层分支。"""
    out: dict[str, Any] = {"ok": False, "engine": "winrt-ocr", "code": code, "message": message}
    if detail:
        out["detail"] = detail
    return out


def validate_ocr_spec(spec: OcrSpec) -> None:
    """纯函数：调用方错误直接抛（编程错误应当被看见，不回收成 ok:false）。"""
    if not spec.image_path or not str(spec.image_path).strip():
        raise OcrInputMissing(
            "缺少图片路径（image_path）",
            "本期只支持本地图片文件，例如：winuse ocr --image <IMAGE_PATH>",
        )
    if not os.path.isfile(spec.image_path):
        raise OcrImageNotFound(
            f"图片文件不存在：{spec.image_path}",
            "请确认路径正确且是普通文件（本期不支持目录、URL、剪贴板）",
        )

    # 格式与大小（Optional-2）：扩展名白名单挡住"随手传个 .exe"，
    # 大小上限挡住"超大图把 OCR 引擎拖到超时"——两者都给明确错误码
    ext = os.path.splitext(str(spec.image_path))[1].lower()
    if ext not in limit_mod.OCR_IMAGE_EXTENSIONS:
        raise OcrImageUnsupported(
            f"不支持的图片扩展名：{ext or '(无扩展名)'}",
            "允许：" + "、".join(limit_mod.OCR_IMAGE_EXTENSIONS),
        )
    try:
        size = os.path.getsize(spec.image_path)
    except OSError as exc:  # 权限不足 / 文件刚被删：按"读不到"处理，给稳定错误码
        raise OcrImageNotFound(f"无法读取图片信息：{spec.image_path}", str(exc)[:200]) from exc
    if size > limit_mod.MAX_OCR_IMAGE_BYTES:
        raise OcrImageTooLarge(
            f"图片 {size} 字节超过上限 {limit_mod.MAX_OCR_IMAGE_BYTES} 字节",
            "大图会让 OCR 引擎吃满内存或超时；请先裁剪/缩放后再识别",
        )


def run_ocr(
    spec: OcrSpec,
    runner: Runner | None = None,
    which: Which | None = None,
) -> dict:
    """识别本地图片上的文字。

    - 调用方错误（缺路径 / 文件不存在）→ 抛 `WinUseError` 子类
    - 引擎侧失败（WinRT 缺失、语言包未装、脚本报错）→ 返回 `ok:false` + `code`
      （`OCR_ENGINE_UNAVAILABLE` 等），让上层 agent 读 code 决定下一步
    """
    validate_ocr_spec(spec)

    if not has_powershell(which):
        return _failure(
            OcrEngineUnavailable.code,
            "找不到 Windows PowerShell 5.1（powershell.exe），无法调用 WinRT OCR",
            "PowerShell 7（pwsh.exe）没有内建 WinRT 投影，不能替代；"
            "WinRT OCR 本身还需要系统安装对应语言的识别语言包",
        )

    run = runner or default_runner
    try:
        code, stdout, stderr = run(build_ocr_command(spec))
    except FileNotFoundError as exc:
        return _failure(OcrEngineUnavailable.code, "无法启动 powershell.exe", str(exc))
    except subprocess.TimeoutExpired:
        return _failure(
            OcrEngineUnavailable.code,
            f"OCR 调用超时（>{OCR_TIMEOUT_SECONDS}s）",
            "图片过大或系统繁忙，可先裁剪图片再试",
        )
    except OSError as exc:  # 权限、路径过长等
        return _failure(OcrEngineUnavailable.code, f"启动 powershell.exe 失败：{exc}", "")

    # 退出码先判（R-17）：PowerShell 失败时也可能在 stdout 留下**部分输出**，
    # 只要里面恰好带成功标记就会被报告成"识别成功"——退出码非零就是没跑成
    if code != 0:
        return _failure(
            OcrRunFailed.code,
            f"OCR 进程退出码非零（{code}）",
            (stderr or "").strip()[-200:] or "PowerShell 没有给出错误输出",
        )

    try:
        return parse_ocr_output(stdout, spec.language)
    except WinUseError as exc:
        detail = exc.detail or stderr.strip()[-200:]
        return _failure(exc.code, exc.message, detail)
