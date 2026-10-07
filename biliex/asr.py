"""本地 ASR 后端（L3 的第二半）—— **可选组件，核心功能不依赖它**。

设计约定
========

1. **惰性导入**：本模块在**模块级不 import 任何第三方库**。没装 `faster-whisper` 时，
   导入本模块、跑 `probe()` 都不会抛异常，只会如实报告"不可用 + 怎么装"。
   核心路径（L1/L2/L4）完全不受影响。
2. **不可用就是不可用，不猜**：`probe()` 返回结构化的能力报告（含缺失原因与安装命令），
   CLI 据此决定是 fail-fast 还是静默降级。版本冲突（PyAV 19）也在这一步判出来，
   而不是等下载完音频再炸。
3. **GPU 失败要能退回 CPU**：本机实测 `get_cuda_device_count()` 返回 1、
   `get_supported_compute_types("cuda")` 也正常，但真正推理时才报缺 `cublas64_12.dll`
   —— 也就是**加载前没有可靠办法判断**。所以兜底放在两个点上：
   加载模型失败时退 CPU，推理失败（且报错像 CUDA 库问题）时也退 CPU。
   另外，针对"装了 pip 轮子却仍然找不到 DLL"这个具体坑，
   `ensure_cuda_dll_dirs()` 会在用 GPU 前把 `site-packages/nvidia/*/bin` 注册进
   DLL 搜索路径（实测这是让 GPU 真正跑通的关键一步，见该函数上方的对照表）。

模型权重从哪来
==============

实测（2026-10-07，本机）：

* `huggingface.co` —— TLS 握手超时，**不可达**；
* `hf-mirror.com` —— 一次成功（27s / 2KB），随后连续 6 次在 ~21s 被对端重置（`WinError 10054`）；
* `modelscope.cn` —— **稳定 0.3-0.4s**，Systran 与 turbo 的 CT2 权重都有镜像。

因此**默认从 ModelScope 取权重**（`resolve_model_ref`），取不到再退回 HuggingFace。
退回 HuggingFace 时，本模块会在导入 `huggingface_hub` **之前**把端点指向
`HF_ENDPOINT=https://hf-mirror.com`（除非用户已自设 `HF_ENDPOINT`，或设了
`BILIEX_ASR_NO_MIRROR=1`），并把 `HF_HUB_*_TIMEOUT` 放宽到 30/60 秒 ——
hub 默认 10 秒超时会把慢镜像直接判成"模型加载失败"。
用了哪个源会如实写进产物（`model_ref.source`），不静默替换。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import UpstreamChanged

if TYPE_CHECKING:  # 只在类型检查时导入，运行时**不做**模块级依赖
    from .content import Cue

# 默认模型：large-v3-turbo —— 4 层解码器，速度快、中文效果好，8GB 显存够用。
DEFAULT_MODEL = "large-v3-turbo"

# 更"小"的备选（CPU 或低配机器上更快）
SMALL_MODELS = ("tiny", "base", "small", "medium")

MIRROR_ENDPOINT = "https://hf-mirror.com"
DEFAULT_LANGUAGE = "zh"

# 装不上就用这条命令 —— 提示要能直接复制粘贴
INSTALL_HINT = 'pip install faster-whisper   # 或在本仓库里执行： pip install -e ".[asr]"'

# 关掉"自动套用镜像"的开关（要走官方源的用户设这个）
NO_MIRROR_ENV = "BILIEX_ASR_NO_MIRROR"

# 传输参数（**只在未设置时**生效）。
#
# 为什么必须放宽：huggingface_hub 默认的连接/下载超时是 10 秒，而实测镜像
# `hf-mirror.com` 的 TLS 握手 + 元数据请求可能要几十秒 —— 用默认值会直接
# `ConnectTimeout`，表现成"模型加载失败"。这是实跑踩出来的，不是理论顾虑。
# 超时只是**上限**，放宽不会让正常网络变慢。
HUB_TIMEOUT_DEFAULTS = {
    "HF_HUB_ETAG_TIMEOUT": "30",
    "HF_HUB_DOWNLOAD_TIMEOUT": "60",
}
# 只在走镜像时追加：hf-xet 会连它自己的传输端点（`*.xethub.hf.co`），
# 镜像不代理这些端点，开着反而容易卡住。
MIRROR_ONLY_DEFAULTS = {"HF_HUB_DISABLE_XET": "1"}

# 计算精度：GPU 上 float16 最快，CPU 上 int8 最快
CUDA_COMPUTE_TYPE = "float16"
CPU_COMPUTE_TYPE = "int8"

# PyAV 与 faster-whisper 的兼容边界。
#
# 实测（2026-10-07）：faster-whisper 1.2.1 在解码时调用
# `av.open(path, metadata_errors="ignore")`，而 **PyAV 19 去掉了这个参数**，
# 于是会在**解码音频**这一步抛 `TypeError: open() got an unexpected keyword argument
# 'metadata_errors'` —— 而 faster-whisper 的依赖声明只写 `av>=11`，没有上界，
# 装最新版就会踩中。这里显式判出来，在下载音频**之前**就 fail-fast。
AV_INCOMPATIBLE_MAJOR = 19
AV_FIX_HINT = 'pip install "av<19"'

# 平均 logprob 低于这个值就提示"模型不太确定"。
# 这是**启发式**：Whisper 对自己有把握的段通常远高于 -0.5，低于 -1.0 多半是音频质量、
# 方言口音或小模型能力不足。它只用来提示换模型，不参与"内容是否可信"的判定。
LOW_CONFIDENCE_LOGPROB = -1.0


@dataclass
class Capability:
    """ASR 能力报告。`available=False` 时 `reason` 说明缺什么、`hint` 说明怎么补。"""

    available: bool = False
    backend: str = "faster-whisper"
    version: str = ""
    reason: str = ""
    hint: str = ""
    devices: list[str] = field(default_factory=list)
    default_device: str = "cpu"
    hf_endpoint: str = ""
    mirror_applied: bool = False
    hub_env: dict[str, str] = field(default_factory=dict)
    detail: str = ""
    # 已注册进 DLL 搜索路径的 CUDA 运行时目录（pip 的 nvidia-*-cu12 轮子带来的）
    cuda_runtime: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.available:
            return f"本地 ASR 不可用：{self.reason}。安装：{self.hint}"
        devices = "、".join(self.devices) or "cpu"
        return (
            f"本地 ASR 可用：{self.backend} {self.version}；"
            f"设备：{devices}（默认 {self.default_device}）"
        )

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "backend": self.backend,
            "version": self.version,
            "reason": self.reason,
            "install_hint": self.hint,
            "devices": self.devices,
            "default_device": self.default_device,
            "hf_endpoint": self.hf_endpoint,
            "mirror_applied": self.mirror_applied,
            "hub_env": self.hub_env,
            "detail": self.detail,
            "cuda_runtime": self.cuda_runtime,
        }


@dataclass
class AsrSettings:
    """一次转写的参数。默认值来自环境变量，CLI 参数再覆盖。"""

    model: str = DEFAULT_MODEL
    device: str = "auto"
    compute_type: str = "auto"
    language: str = DEFAULT_LANGUAGE
    vad: bool = True
    beam_size: int = 5
    keep_audio: bool = False
    # 权重来源：auto（先 ModelScope）/ modelscope / huggingface
    source: str = "auto"
    # CTranslate2 的 cpu_threads（0 = 让库自己决定）
    cpu_threads: int = 0

    @classmethod
    def from_env(cls, **overrides: Any) -> "AsrSettings":
        """环境变量给默认值，显式参数（非 None）覆盖之。

        环境变量：`BILIEX_ASR_MODEL` / `BILIEX_ASR_DEVICE` / `BILIEX_ASR_COMPUTE_TYPE` /
        `BILIEX_ASR_LANG` / `BILIEX_ASR_VAD` / `BILIEX_ASR_SOURCE`。
        """
        settings = cls(
            model=os.environ.get("BILIEX_ASR_MODEL") or DEFAULT_MODEL,
            device=os.environ.get("BILIEX_ASR_DEVICE") or "auto",
            compute_type=os.environ.get("BILIEX_ASR_COMPUTE_TYPE") or "auto",
            language=os.environ.get("BILIEX_ASR_LANG") or DEFAULT_LANGUAGE,
            vad=_env_flag("BILIEX_ASR_VAD", default=True),
            source=os.environ.get(SOURCE_ENV) or "auto",
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(settings, key, value)
        return settings

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
            "language": self.language,
            "vad": self.vad,
            "beam_size": self.beam_size,
            "source": self.source,
            "cpu_threads": self.cpu_threads,
        }


@dataclass
class AsrResult:
    """一次转写的结果。"""

    cues: list[Cue] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    audio_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """转录覆盖到的时长 / 音频总时长。

        本地转写理论上应当接近 100%；明显偏低说明音频不完整或模型提前结束。
        """
        if not self.cues or self.audio_seconds <= 0:
            return 0.0
        return min(self.cues[-1].end / self.audio_seconds, 1.0)

    def to_dict(self) -> dict:
        return {
            "info": self.info,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "audio_seconds": round(self.audio_seconds, 1),
            "segment_count": len(self.cues),
            "coverage": round(self.coverage, 4),
            "warnings": self.warnings,
        }


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def apply_hub_transport_defaults(*, mirrored: bool) -> dict[str, str]:
    """设置模型仓库的传输参数，**只在用户没有自己设置时**生效。

    返回实际设置的那几项，便于在 `asr status` 里如实展示"我们替你设了什么"。
    """
    candidates = dict(HUB_TIMEOUT_DEFAULTS)
    if mirrored:
        candidates.update(MIRROR_ONLY_DEFAULTS)
    applied: dict[str, str] = {}
    for key, value in candidates.items():
        if not os.environ.get(key):
            os.environ[key] = value
            applied[key] = value
    return applied


def ensure_hf_endpoint() -> tuple[str, bool, dict[str, str]]:
    """在导入 huggingface_hub 之前决定用哪个模型仓库端点。

    返回 `(endpoint, 是否由本模块自动套用了镜像, 实际设置的传输参数)`。
    已设置 `HF_ENDPOINT` 时**尊重用户设置**，绝不覆盖。
    """
    current = os.environ.get("HF_ENDPOINT", "").strip()
    if current:
        return current, False, apply_hub_transport_defaults(mirrored=False)
    if _env_flag(NO_MIRROR_ENV, default=False):
        return "", False, apply_hub_transport_defaults(mirrored=False)
    os.environ["HF_ENDPOINT"] = MIRROR_ENDPOINT
    return MIRROR_ENDPOINT, True, apply_hub_transport_defaults(mirrored=True)


# ---------------------------------------------------------------- 模型权重从哪来
#
# 实测（2026-10-07，本机）：
#   huggingface.co  → TLS 握手超时，完全不可达；
#   hf-mirror.com   → 一次成功（27s/2KB），随后连续 6 次在 ~21s 被对端重置（WinError 10054）；
#   modelscope.cn   → 稳定 0.3-0.4s，且 Systran / turbo 的 CT2 权重都有镜像。
# 结论：**默认从 ModelScope 取权重**，取不到再退回 HuggingFace（含自动镜像）。
# faster-whisper 接受**本地目录**作为模型路径，所以这条路不需要改它的下载逻辑。

MODELSCOPE_API = "https://modelscope.cn/api/v1/models"

# 短名 → 两个仓库的 id。HF 侧与 `faster_whisper.utils._MODELS` 一致（已核对）。
MODEL_REPOS: dict[str, dict[str, str]] = {
    "tiny": {
        "hf": "Systran/faster-whisper-tiny",
        "modelscope": "Systran/faster-whisper-tiny",
    },
    "base": {
        "hf": "Systran/faster-whisper-base",
        "modelscope": "Systran/faster-whisper-base",
    },
    "small": {
        "hf": "Systran/faster-whisper-small",
        "modelscope": "Systran/faster-whisper-small",
    },
    "medium": {
        "hf": "Systran/faster-whisper-medium",
        "modelscope": "Systran/faster-whisper-medium",
    },
    "large-v3": {
        "hf": "Systran/faster-whisper-large-v3",
        "modelscope": "Systran/faster-whisper-large-v3",
    },
    "large-v3-turbo": {
        "hf": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "modelscope": "pengzhendong/faster-whisper-large-v3-turbo",
    },
}
MODEL_REPOS["turbo"] = MODEL_REPOS["large-v3-turbo"]
MODEL_REPOS["large"] = MODEL_REPOS["large-v3"]

# 模型源：auto（默认，先 ModelScope 后 HuggingFace）/ modelscope / huggingface
SOURCE_ENV = "BILIEX_ASR_SOURCE"
# 权重缓存目录（默认放在用户目录，**不进项目目录**）
CACHE_ENV = "BILIEX_ASR_CACHE"

# 判定"权重已经就位"需要的文件
_REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json")


def asr_cache_dir() -> Path:
    """权重缓存目录：`BILIEX_ASR_CACHE` → 否则用户配置目录下的 `models/`。"""
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from . import config  # 惰性导入，避免可选组件影响核心导入路径

    return config.config_dir() / "models"


@dataclass
class ModelRef:
    """权重最终从哪来。

    这东西要如实写进产物：用户有权知道某次转写用的模型是自己给的目录、
    还是从 ModelScope 下的、还是从 HuggingFace 拉的。
    """

    path: str
    source: str  # local | modelscope | huggingface
    repo: str = ""
    cached: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "source": self.source,
            "repo": self.repo,
            "cached": self.cached,
        }


def repo_ids(model: str) -> dict[str, str]:
    """把模型短名解析成 `{hf, modelscope}` 仓库 id。

    不是短名（例如 `org/name`）就按同名在两边找 —— 镜像上不存在时会自然失败并回退。
    """
    if model in MODEL_REPOS:
        return dict(MODEL_REPOS[model])
    if "/" in model:
        return {"hf": model, "modelscope": model}
    return {"hf": model, "modelscope": model}


def _http_get_json(url: str, *, timeout: float) -> dict:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": config_user_agent()})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def config_user_agent() -> str:
    from . import config

    return config.USER_AGENT


def check_modelscope(repo: str, *, timeout: float = 6.0) -> bool:
    """能不能从 ModelScope 拿到这个仓库 —— 只发一个很小的请求。

    ⚠️ **不要依赖 config.json 里具体有哪些键**：实测 tiny 的 config.json 只有
    `alignment_heads / lang_ids / suppress_ids / suppress_ids_begin` 四个键，
    按"猜字段"来判定会把整条 ModelScope 路径**静默判死**（这个坑我踩过一次）。
    这里改为：HTTP 200 + JSON 对象 + 非空 + 不是错误信封。
    """
    url = f"{MODELSCOPE_API}/{repo}/repo?Revision=master&FilePath=config.json"
    try:
        payload = _http_get_json(url, timeout=timeout)
    except Exception:  # noqa: BLE001 - 探测失败就是"不可用"，不往上抛
        return False
    if not isinstance(payload, dict) or not payload:
        return False
    # 出错时它也可能返回 200 + JSON（带 Code/Message），要和正常内容区分开
    return not ("Code" in payload and "Message" in payload)


def modelscope_files(repo: str, *, timeout: float = 20.0) -> list[dict]:
    """列出仓库里的文件（`Path` / `Size` / `Type`）。"""
    url = f"{MODELSCOPE_API}/{repo}/repo/files?Revision=master&Root="
    payload = _http_get_json(url, timeout=timeout)
    files = (payload.get("Data") or {}).get("Files") or []
    return [f for f in files if isinstance(f, dict) and f.get("Path")]


def _skip_file(name: str) -> bool:
    """这些是仓库自带的说明/元数据，转写用不到。"""
    return name in {".gitattributes", "README.md", "configuration.json", "msc_cfg.json"}


def download_from_modelscope(
    repo: str,
    dest: Path,
    *,
    completion: Callable[[int, int], None] | None = None,
    timeout: float = 60.0,
) -> Path:
    """把 ModelScope 上的 CT2 权重下到 `dest`，返回 `dest`。

    逐文件下载并**跳过已完整的文件**（`model.bin` 有几百 MB，
    中断一次就从零再来是不可接受的）。
    """
    import urllib.parse
    import urllib.request

    dest.mkdir(parents=True, exist_ok=True)
    files = [f for f in modelscope_files(repo) if not _skip_file(f["Path"])]
    if not files:
        raise UpstreamChanged(f"ModelScope 上 {repo} 没有可下载的文件")

    total = sum(int(f.get("Size") or 0) for f in files)
    done = 0
    for item in files:
        name = item["Path"]
        size = int(item.get("Size") or 0)
        target = dest / name
        if target.exists() and (not size or target.stat().st_size == size):
            done += size
            if completion is not None:
                completion(done, total)
            continue
        url = (
            f"{MODELSCOPE_API}/{repo}/repo?Revision=master"
            f"&FilePath={urllib.parse.quote(name)}"
        )
        request = urllib.request.Request(url, headers={"User-Agent": config_user_agent()})
        with urllib.request.urlopen(request, timeout=timeout) as response, open(target, "wb") as fh:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if completion is not None:
                    completion(min(done, total or done), total or done)
        # 下载完再校正计数（服务端 Size 与实际字节数可能略有出入）
        done = sum(
            (dest / f["Path"]).stat().st_size
            for f in files
            if (dest / f["Path"]).exists()
        )
    return dest


def has_weights(directory: Path) -> bool:
    """目录里是否已经有一份可用权重。"""
    return all((directory / name).exists() for name in _REQUIRED_FILES)


def resolve_model_ref(
    model: str,
    *,
    source: str = "auto",
    completion: Callable[[int, int], None] | None = None,
    timeout: float = 6.0,
) -> ModelRef:
    """把 `--asr-model` 解析成 faster-whisper 能直接吃的路径或仓库名。

    * 传的是**本地目录** → 直接用（留给离线/自备权重的人）；
    * `source=modelscope` → 必须从 ModelScope 下好，失败就报错（不静默改用别处）；
    * `source=huggingface` → 交回给 faster-whisper 自己按 `HF_ENDPOINT` 下载；
    * `source=auto`（默认）→ 先 ModelScope，探不通再回退 HuggingFace。
    """
    local = Path(model).expanduser()
    if local.is_dir():
        return ModelRef(path=str(local), source="local")

    ids = repo_ids(model)
    dest = asr_cache_dir() / ids["modelscope"].replace("/", "__")

    if source in {"auto", "modelscope"}:
        if has_weights(dest):
            return ModelRef(path=str(dest), source="modelscope", repo=ids["modelscope"], cached=True)
        if check_modelscope(ids["modelscope"], timeout=timeout):
            download_from_modelscope(ids["modelscope"], dest, completion=completion)
            return ModelRef(path=str(dest), source="modelscope", repo=ids["modelscope"])
        if source == "modelscope":
            raise UpstreamChanged(
                f"指定的模型源是 ModelScope，但 {ids['modelscope']} 不可达"
                f"（可能镜像上没有这个仓库）。改用 `--asr-source auto` 或 huggingface。"
            )

    # 回退：交回 faster-whisper / huggingface_hub（HF_ENDPOINT 已在前面的 ensure 里设好）
    return ModelRef(path=model, source="huggingface", repo=ids["hf"])


# ---------------------------------------------------------------- 能力探测


def _import_backend():
    """导入 faster-whisper。**唯一**的第三方导入点，失败一律转成 ImportError 语义。"""
    ensure_hf_endpoint()
    import faster_whisper  # noqa: PLC0415 - 刻意惰性导入

    return faster_whisper


_CAPABILITY: Capability | None = None


def check_av_compatibility(version: str) -> tuple[str, str] | None:
    """PyAV 版本是否和 faster-whisper 合得来。返回 `(原因, 修复命令)` 或 None。

    做成纯函数是为了能直接测各种版本串，不用真的装两个 PyAV。
    """
    raw = (version or "").strip()
    if not raw:
        return None
    try:
        major = int(raw.split(".")[0])
    except (ValueError, IndexError):
        return None
    if major >= AV_INCOMPATIBLE_MAJOR:
        return (
            f"已安装的 PyAV {raw} 与 faster-whisper 不兼容"
            "（faster-whisper 仍传 `metadata_errors`，PyAV 19 起已去掉该参数）",
            AV_FIX_HINT,
        )
    return None


def _av_incompatibility() -> tuple[str, str] | None:
    try:
        import av  # noqa: PLC0415 - faster-whisper 的解码后端

        return check_av_compatibility(str(getattr(av, "__version__", "") or ""))
    except Exception:  # noqa: BLE001 - 装不上/读不到版本就不做这个判断
        return None


def probe(*, refresh: bool = False, backend=None) -> Capability:
    """探测本地 ASR 是否可用。**不会抛异常，也不会下载任何模型。**

    `backend` 参数用于测试注入。
    """
    global _CAPABILITY
    if _CAPABILITY is not None and not refresh and backend is None:
        return _CAPABILITY

    endpoint, mirrored, hub_env = ensure_hf_endpoint()
    report = Capability(
        hf_endpoint=endpoint or "(官方 huggingface.co)",
        mirror_applied=mirrored,
        hub_env=hub_env,
        hint=INSTALL_HINT,
    )

    try:
        module = backend if backend is not None else _import_backend()
    except ImportError as exc:
        report.available = False
        report.reason = "未安装 faster-whisper"
        report.detail = str(exc)
        return _remember(report, backend)

    report.version = str(getattr(module, "__version__", "") or "")

    # 版本冲突要在**下载音频之前**就判出来，而不是等到解码时才炸
    conflict = _av_incompatibility()
    if conflict is not None:
        report.available = False
        report.reason, report.hint = conflict
        return _remember(report, backend)

    devices = ["cpu"]
    cuda_count = 0
    report.cuda_runtime = ensure_cuda_dll_dirs()
    try:
        import ctranslate2  # noqa: PLC0415 - faster-whisper 的推理后端

        cuda_count = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:  # noqa: BLE001 - 探测阶段不该抛
        report.detail = f"查询 CUDA 设备失败：{type(exc).__name__}: {exc}"
    if cuda_count > 0:
        devices.insert(0, "cuda")

    report.available = True
    report.devices = devices
    report.default_device = "cuda" if cuda_count > 0 else "cpu"
    return _remember(report, backend)


def _remember(report: Capability, backend) -> Capability:
    global _CAPABILITY
    if backend is None:
        _CAPABILITY = report
    return report


def is_available() -> bool:
    return probe().available


# ---------------------------------------------------------------- CUDA 运行时发现
#
# 实测（2026-10-07，RTX 3070 Ti / 驱动 591.86）：**把 `nvidia-cublas-cu12` 与
# `nvidia-cudnn-cu12` 装进虚拟环境并不够**。这两个轮子把 DLL 放在
# `site-packages/nvidia/*/bin`，而 Windows 的 DLL 搜索路径里**没有**这个目录，
# 于是现象极具误导性：
#
# * `ctranslate2.get_cuda_device_count()` 照样返回 1 —— 它走的是驱动 API（`nvcuda.dll`，
#   由显卡驱动装在 System32 里，一直都在）；
# * `get_supported_compute_types("cuda")` 也照样返回一串类型；
# * 直到真正推理，cuBLAS 才加载失败：`Library cublas64_12.dll is not found or cannot be loaded`。
#
# 另外，ctranslate2 4.8.2 的 Windows 轮子里**已经自带 cuDNN 9 的转发层**
# （`ctranslate2/cudnn64_9.dll`，266 KB），它会再去加载
# `cudnn_{ops,cnn,adv,graph}64_9.dll` 与 `zlibwapi.dll`；而 `ctranslate2.dll` 里
# **写死的动态加载名是 `cublas64_12.dll`**（用 tools/pe_dll_names.py 从二进制里读出来的，
# 不是查文档猜的）。所以"CUDA 12 的 cuBLAS + cuDNN 9"是硬约束，CUDA 13 的
# `cublas64_13.dll` 顶不上（本机另一个 Python 里装了 cu130 的 torch，实测只有 13）。
#
# 让 Windows 找到这些 DLL 的办法，各放在**独立子进程**里实测
# （DLL 搜索路径是进程级且不可逆，同进程里试完再试下一种会得到假阳性）。
#
# ⚠️ 这里有一个**非常容易得出错误结论**的陷阱，第一版就踩了：
# 如果探测脚本在推理**之前**用 `ctypes.WinDLL("cublas64_12.dll")` 探一次，那次调用
# 会把 DLL **先加载进进程**；之后 ctranslate2 再按名字加载时直接命中已加载的模块，
# 于是"注册根本没生效"也会显示成推理成功。`ctypes` 走的是
# `LoadLibraryExW(..., LOAD_WITH_ALTERED_SEARCH_PATH)`，和 ctranslate2 用的
# `kernel32!LoadLibraryA` **不是同一条搜索链**，两者结果可以完全相反。
#
# 用与 ctranslate2 完全相同的 `LoadLibraryA`、且**不做任何预探测、不加载模型**做真值测量
# （`python tools/gpu_path_probe.py loadlib --all`），得到的结论是：
#
# | 办法 | `LoadLibraryA("cublas64_12.dll")` 能不能成功 |
# |---|---|
# | 什么都不做 | ❌ `ERROR_MOD_NOT_FOUND (126)` |
# | `os.add_dll_directory(dir)` | ❌ **仍然 126** —— 对 LoadLibraryA 无效 |
# | 目录前插到 `PATH` | ✅ 成功（且 `import av` 之后依然成功） |
# | 把 DLL 硬链接进 `ctranslate2/` | ❌ 进程层探针同样 126（见下） |
#
# 最后一行有个细节：硬链接方案在**真推理**里成功过，因为从 `ctranslate2.dll` 内部发起的加载
# 会以它自己的目录为基准；而从 Python 层发起不会。也就是说"能不能用"取决于**谁发起这次加载**，
# 这条路的判据天生不可靠，还要动 `site-packages` —— 所以不采用，只留 PATH 一条主干。
#
# 所以**真正生效的那一步是把目录前插到 `PATH`**；`os.add_dll_directory` 保留下来只是
# 给"扩展模块的依赖解析"和 ctypes 这类走 LOAD_LIBRARY_SEARCH_* 的调用兜底，两者一起做最稳。
# 这份结论也解释了修复前的现象：只调 `os.add_dll_directory` 时，
# `asr status` 会如实显示"已注册 3 个目录"，但真推理照样退回 CPU。
#
# 全程只用标准库，符合本模块「模块级零三方依赖」的约定；没装 nvidia 轮子时它就是空操作。

# `os.add_dll_directory` 返回的句柄**必须持有**：对象被 GC 回收时目录注册就失效
_DLL_DIR_HANDLES: list[Any] = []
_REGISTERED_DLL_DIRS: list[str] = []
_CUDA_DLL_DIRS: list[Path] | None = None


def _site_package_roots() -> list[Path]:
    """当前解释器可能放 site-packages 的几个位置（去重、保序、只留存在的）。"""
    import sysconfig

    candidates: list[Path] = []
    for key in ("purelib", "platlib"):
        try:
            candidates.append(Path(sysconfig.get_paths()[key]))
        except Exception:  # noqa: BLE001 - 探测阶段不该抛
            pass
    for entry in sys.path:
        if entry and entry.replace("\\", "/").rstrip("/").endswith(
            ("site-packages", "dist-packages")
        ):
            candidates.append(Path(entry))

    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen and path.is_dir():
            seen.add(key)
            out.append(path)
    return out


def cuda_runtime_dirs() -> list[Path]:
    """pip 轮子（`nvidia-*-cu12`）把 CUDA 运行时动态库放在哪些目录。

    找不到就是空列表 —— 这既可能因为"没装轮子"，也可能因为"用的是系统 CUDA Toolkit"
    （那种情况下 DLL 本来就在 PATH 里，不需要我们插手）。结果缓存，避免重复遍历。
    """
    global _CUDA_DLL_DIRS
    if _CUDA_DLL_DIRS is not None:
        return _CUDA_DLL_DIRS

    pattern = "*.dll" if os.name == "nt" else "*.so*"
    subdirs = ("bin", "lib") if os.name == "nt" else ("lib", "bin")
    found: list[Path] = []
    for root in _site_package_roots():
        nvidia = root / "nvidia"
        if not nvidia.is_dir():
            continue
        for subdir in subdirs:
            for directory in sorted(nvidia.glob(f"*/{subdir}")):
                if directory.is_dir() and any(directory.glob(pattern)):
                    if directory not in found:
                        found.append(directory)
    _CUDA_DLL_DIRS = found
    return found


def ensure_cuda_dll_dirs() -> list[str]:
    """把 `site-packages/nvidia/*/bin` 弄进当前进程的 DLL 搜索路径。幂等。

    两步都做，但只有第一步是**实测有效**的那一步：

    1. **前插 `PATH`** —— ctranslate2 内部是 `kernel32!LoadLibraryA("cublas64_12.dll")`，
       只有 `PATH` 对它有效（实测 `os.add_dll_directory` 对它无效，见上方对照表）；
    2. `os.add_dll_directory` —— 给走 `LOAD_LIBRARY_SEARCH_*` 的调用（扩展模块依赖解析、
       ctypes）兜底，属于补充而非主力。

    返回**已处理**的目录列表（不是"发现的"）—— 处理失败时如实少列出来，
    好过让用户以为已经生效（修复前就吃过这个亏：status 显示已注册，推理照样退回 CPU）。
    """
    dirs = cuda_runtime_dirs()
    if not dirs:
        return []
    keys = [str(d) for d in dirs]

    # 1) PATH 前插：LoadLibraryA 唯一认的那条路
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    missing = [k for k in keys if k not in parts]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing + parts) if parts else os.pathsep.join(missing)

    # 2) add_dll_directory：Windows 专有，失败不影响第 1 步的效果
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for key in keys:
            if key in _REGISTERED_DLL_DIRS:
                continue
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(key))
            except OSError:
                continue

    for key in keys:
        if key not in _REGISTERED_DLL_DIRS:
            _REGISTERED_DLL_DIRS.append(key)
    return list(_REGISTERED_DLL_DIRS)


CUDA_INSTALL_HINT = (
    'pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"   '
    "# 约 1.3 GB；国内建议配镜像源"
)


# ---------------------------------------------------------------- 设备与精度


def resolve_device(requested: str, capability: Capability | None = None) -> str:
    """把 `auto` 解析成具体设备。显式指定的设备原样返回（失败会在加载时兜底）。"""
    if requested and requested != "auto":
        return requested
    report = capability or probe()
    return report.default_device if report.available else "cpu"


def resolve_compute_type(requested: str, device: str) -> str:
    if requested and requested != "auto":
        return requested
    return CUDA_COMPUTE_TYPE if device.startswith("cuda") else CPU_COMPUTE_TYPE


def build_model(
    settings: AsrSettings,
    capability: Capability | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[Any, str, str, list[str], ModelRef]:
    """加载模型。返回 `(model, device, compute_type, warnings, 权重来源)`。

    权重先经 `resolve_model_ref` 定位（默认 ModelScope，见文件顶部说明）；
    GPU 加载失败（最常见的是缺 cuDNN / cuBLAS 的 DLL）时**自动退回 CPU**，
    并把原因写进 warnings —— 宁可慢，也不要因为环境问题直接失败。
    """
    warnings: list[str] = []
    module = _import_backend()
    ref = resolve_model_ref(settings.model, source=settings.source, completion=progress)
    device = resolve_device(settings.device, capability)
    compute_type = resolve_compute_type(settings.compute_type, device)

    if device.startswith("cuda"):
        # 这一步不会有异常，也**不能**用来判断 GPU 可用性（缺 DLL 时它照样是空操作，
        # 失败要等到推理那一刻）；它只是把"装了 pip 轮子但没人告诉 Windows 去哪找"这一环补上。
        ensure_cuda_dll_dirs()

    kwargs: dict[str, Any] = {"device": device, "compute_type": compute_type}
    if settings.cpu_threads:
        kwargs["cpu_threads"] = settings.cpu_threads

    try:
        return (
            module.WhisperModel(ref.path, **kwargs),
            device,
            compute_type,
            warnings,
            ref,
        )
    except Exception as exc:  # noqa: BLE001 - 上游库的异常类型不稳定
        if not device.startswith("cuda"):
            raise UpstreamChanged(
                f"加载 ASR 模型 {settings.model} 失败：{type(exc).__name__}: {str(exc)[:200]}"
            ) from None
        warnings.append(
            f"GPU 加载失败（{type(exc).__name__}: {str(exc)[:200]}），已退回 CPU 的 int8 推理。"
            f"若要用 GPU，需要 CUDA 12 运行时 + cuDNN 9 的 DLL：{CUDA_INSTALL_HINT}"
        )

    cpu_kwargs = dict(kwargs, device="cpu", compute_type=CPU_COMPUTE_TYPE)
    try:
        return (
            module.WhisperModel(ref.path, **cpu_kwargs),
            "cpu",
            CPU_COMPUTE_TYPE,
            warnings,
            ref,
        )
    except Exception as exc:  # noqa: BLE001
        raise UpstreamChanged(
            f"加载 ASR 模型 {settings.model} 失败：{type(exc).__name__}: {str(exc)[:200]}"
        ) from None


# ---------------------------------------------------------------- 转写


def looks_like_cuda_library_error(message: str) -> bool:
    """这个报错像不像"缺 CUDA/cuDNN 的动态库"。

    实测（2026-10-07，RTX 3070 Ti / 驱动 591.86）：`get_cuda_device_count()` 返回 1、
    `get_supported_compute_types("cuda")` 也照常返回一串类型，但真正推理时才报
    `Library cublas64_12.dll is not found or cannot be loaded` ——
    也就是说**加载前没有可靠的办法判断**，只能在失败点兜住。
    （把 pip 轮子的目录注册进 DLL 搜索路径能修好这一类里最常见的一种，
    但"驱动太老""显卡不支持该精度"等仍然只会在推理时暴露，所以兜底不能撤。）
    """
    text = (message or "").lower()
    return any(key in text for key in ("cublas", "cudnn", "cudart", "nvcuda", "cuda"))


def transcribe(
    audio_path: Path,
    settings: AsrSettings,
    *,
    capability: Capability | None = None,
    progress: Callable[[float, float], None] | None = None,
    model_progress: Callable[[int, int], None] | None = None,
) -> AsrResult:
    """把一个音频文件转写成语义等价的字幕条目（`Cue` 列表）。

    进度回调：`progress(已处理秒数, 总秒数)`；`model_progress(已下载字节, 总字节)`
    用于首次下载权重时的进度。

    GPU 失败（缺 cuBLAS / cuDNN 的 DLL）时**自动用 CPU 重跑一遍**：
    这类错误在 Windows 上往往到推理那一刻才暴露（见 `looks_like_cuda_library_error`），
    所以兜底必须在这里，而不是只在加载模型时。
    """
    from .content import Cue  # 惰性导入：避免与 content.py 形成模块级循环依赖

    model, device, compute_type, warnings, ref = build_model(
        settings, capability, progress=model_progress
    )
    result = AsrResult(
        info={
            **settings.to_dict(),
            "resolved_device": device,
            "resolved_compute_type": compute_type,
            "model_ref": ref.to_dict(),
        },
        warnings=list(warnings),
    )

    language = None if settings.language in {"", "auto"} else settings.language
    total = 0.0
    logprobs: list[float] = []

    def collect(active_model) -> tuple[list[Cue], Any]:
        """跑一遍解码 + 转写。失败时把异常交给调用方决定是否换设备重跑。"""
        nonlocal total, logprobs
        cues: list[Cue] = []
        logprobs = []
        segments, info = active_model.transcribe(
            str(audio_path),
            language=language,
            beam_size=settings.beam_size,
            vad_filter=settings.vad,
            # VAD 参数：B 站视频常有大段无人声的 BGM，不切掉会显著增加幻觉
            vad_parameters={"min_silence_duration_ms": 500},
        )
        total = float(getattr(info, "duration", 0.0) or 0.0)
        for segment in segments:
            text = (getattr(segment, "text", "") or "").strip()
            # 置信度：小模型在噪声/方言上会明显偏低，这个信号比"看起来流畅"可靠
            score = getattr(segment, "avg_logprob", None)
            if isinstance(score, (int, float)):
                logprobs.append(float(score))
            if not text:
                continue
            cues.append(
                Cue(
                    start=float(getattr(segment, "start", 0.0) or 0.0),
                    end=float(getattr(segment, "end", 0.0) or 0.0),
                    text=text,
                )
            )
            if progress is not None and total:
                progress(cues[-1].end, total)
        return cues, info

    started = time.monotonic()
    try:
        cues, info = collect(model)
    except Exception as exc:  # noqa: BLE001 - 三方库异常类型不稳定
        if not device.startswith("cuda") or not looks_like_cuda_library_error(str(exc)):
            raise
        result.warnings.append(
            f"GPU 推理失败（{type(exc).__name__}: {str(exc)[:200]}），已改用 CPU 的 int8 重跑。"
            f"要在本机用上 GPU，需要 CUDA 12 运行时（cuBLAS）+ cuDNN 9 的 DLL：{CUDA_INSTALL_HINT}"
        )
        cpu_settings = replace(settings, device="cpu", compute_type=CPU_COMPUTE_TYPE)
        model, device, compute_type, more, ref = build_model(cpu_settings, capability)
        result.warnings.extend(more)
        result.info.update(
            resolved_device=device,
            resolved_compute_type=compute_type,
            model_ref=ref.to_dict(),
        )
        cues, info = collect(model)

    result.cues = cues
    result.elapsed_seconds = time.monotonic() - started
    result.audio_seconds = total
    result.info["elapsed_seconds"] = round(result.elapsed_seconds, 1)
    result.info["audio_seconds"] = round(total, 1)
    result.info["detected_language"] = str(getattr(info, "language", "") or "")
    result.info["language_probability"] = round(
        float(getattr(info, "language_probability", 0.0) or 0.0), 3
    )
    if total and result.cues:
        result.info["speed_x"] = round(total / max(result.elapsed_seconds, 0.001), 1)

    if logprobs:
        mean_score = sum(logprobs) / len(logprobs)
        result.info["avg_logprob"] = round(mean_score, 3)
        if mean_score < LOW_CONFIDENCE_LOGPROB:
            result.warnings.append(
                f"转写置信度偏低（各段平均 logprob {mean_score:.2f}）：模型对这段音频不太确定，"
                "同音字与断句更容易出错。用于正式总结前建议换更大的模型"
                "（`--asr-model small` 或 `medium`）。"
            )
    return result
