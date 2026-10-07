"""L3（本地 ASR，可选组件）的离线单元测试。

这个文件刻意**与核心测试分开**：本地 ASR 是可选能力，
它的测试不应该要求装上 `faster-whisper`，也不应该让核心测试依赖它。

覆盖四类：
1. 音轨选择与下载头的**安全不变量**（下载音频绝不带 Cookie、落档绝不带签名 URL）；
2. 能力探测在"装了"与"没装"两种情况下都要给出**可执行**的结论；
3. L3 与 L1/L2/L4 的编排关系（何时启用、何时替换、失败如何降级）；
4. 渲染层对"数据来源"的表述 —— 不能把本机转写说成 B 站官方内容。
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest import mock

from biliex import asr
from biliex import audio as audio_mod
from biliex.content import (
    COVERAGE_THRESHOLD,
    LEVEL_ASR,
    LEVEL_NONE,
    LEVEL_OFFICIAL,
    LEVEL_SUBTITLE,
    Cue,
    PageContent,
    SubtitleOutcome,
    fetch_page,
    run_local_asr,
)
from biliex.render import source_note

# 真实的 playurl 响应片段（2026-10-07 实测抓到的结构，URL 已替换为占位符）
DASH_PAYLOAD = {
    "accept_quality": [120, 112, 80, 64, 32, 16],
    "timelength": 1144000,
    "dash": {
        "duration": 1144,
        "audio": [
            {
                "id": 30216,
                "baseUrl": "https://cdn.example/a64.m4s?sig=AAA",
                "backupUrl": ["https://backup.example/a64.m4s?sig=BBB"],
                "bandwidth": 65683,
                "mimeType": "audio/mp4",
                "codecs": "mp4a.40.2",
            },
            {
                "id": 30232,
                "baseUrl": "https://cdn.example/a132.m4s?sig=CCC",
                "bandwidth": 99580,
                "mimeType": "audio/mp4",
                "codecs": "mp4a.40.2",
            },
            {
                "id": 30280,
                "baseUrl": "https://cdn.example/a192.m4s?sig=DDD",
                "bandwidth": 99580,
                "mimeType": "audio/mp4",
                "codecs": "mp4a.40.2",
            },
        ],
    },
}


# 测试用的临时目录**放在仓库内**（tests/.tmp），不用系统 temp：
# 一是不受沙箱对系统临时目录的限制影响，二是失败时残留的文件就在眼皮底下。
_TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


def _temp_dir() -> Path:
    """建一个测试用临时目录。

    ⚠️ 刻意**不用** `tempfile.mkdtemp()`（也不是因为系统临时目录被禁止）：

    实测（2026-10-07，本机）根因是 **mode 0o700** —— CPython 在 Windows 上对
    `os.mkdir(path, 0o700)` 会构造一套**受保护的、不继承父目录**的目录权限，
    而 DSH 沙箱的授权项是靠**从工作区根继承**生效的，于是这个目录建出来就没人能进
    （连创建它的进程自己写不进去、列不出来、也删不掉）。对照实验：

    | 创建方式 | 结果 |
    |---|---|
    | `os.mkdir(p)`（默认） | 可用 |
    | `os.mkdir(p, 0o750)` / `0o755` / `0o777` | 可用 |
    | `os.mkdir(p, 0o700)` | ❌ 建出来即成"黑洞" |
    | `tempfile.mkdtemp()`（内部就是 `mkdir(0o700)`） | ❌ 同上，**与目录位置无关** |

    所以这里用 `mkdir` + 随机名。任何用 `mkdtemp` 的工具（例如 `uv build`）在
    受限会话下都会因此失败，而**在不受限的普通终端里一切正常**（已实测）。
    """
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / f"l3-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return path


class TestAudioSelection(unittest.TestCase):
    def test_picks_highest_bandwidth_then_id_rank(self):
        stream = audio_mod.pick_stream(DASH_PAYLOAD)
        self.assertIsNotNone(stream)
        # 30232 与 30280 带宽相同，按音质 id 应选 30280
        self.assertEqual(stream.stream_id, 30280)
        self.assertEqual(stream.bandwidth, 99580)
        self.assertEqual(stream.source, "dash")
        self.assertEqual(stream.extension, ".m4s")

    def test_picks_only_available_stream(self):
        payload = {"dash": {"audio": [DASH_PAYLOAD["dash"]["audio"][0]]}}
        stream = audio_mod.pick_stream(payload)
        self.assertEqual(stream.stream_id, 30216)

    def test_stream_without_url_is_ignored(self):
        payload = {"dash": {"audio": [{"id": 30280, "bandwidth": 99999}]}}
        self.assertIsNone(audio_mod.pick_stream(payload))

    def test_durl_fallback(self):
        payload = {"durl": [{"order": 1, "url": "https://cdn.example/x.flv", "size": 123}]}
        stream = audio_mod.pick_stream(payload)
        self.assertEqual(stream.source, "durl")
        self.assertEqual(stream.extension, ".mp4")
        self.assertEqual(stream.segment_count, 1)

    def test_multi_segment_durl_is_flagged_not_silently_truncated(self):
        payload = {
            "durl": [
                {"order": 1, "url": "https://cdn.example/1.flv"},
                {"order": 2, "url": "https://cdn.example/2.flv"},
            ]
        }
        stream = audio_mod.pick_stream(payload)
        # 多段必须被标出来 —— 只下第一段会产出一份"看起来正常"的残缺转录
        self.assertEqual(stream.segment_count, 2)

    def test_no_audio_at_all(self):
        self.assertIsNone(audio_mod.pick_stream({}))
        self.assertIsNone(audio_mod.pick_stream({"dash": {}}))


class TestAudioSecurityInvariants(unittest.TestCase):
    """音频这条路有两处刻意的差异，都要有测试守着。"""

    def test_download_headers_never_carry_cookie(self):
        headers = audio_mod.download_headers()
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("cookie", {k.lower() for k in headers})
        # 断点续传时也不能冒出来
        resumed = audio_mod.download_headers(offset=1024)
        self.assertNotIn("Cookie", resumed)
        self.assertEqual(resumed["Range"], "bytes=1024-")

    def test_metadata_never_contains_signed_urls(self):
        """签名 URL 是短期令牌，不该被写进 raw/ 留档。"""
        stream = audio_mod.pick_stream(DASH_PAYLOAD)
        dumped = json.dumps(stream.to_dict())
        self.assertNotIn("sig=", dumped)
        self.assertNotIn("http", dumped)
        self.assertEqual(stream.to_dict()["host"], "cdn.example")

    def test_streams_metadata_has_no_urls(self):
        dumped = json.dumps(audio_mod.streams_metadata(DASH_PAYLOAD))
        self.assertNotIn("http", dumped)
        self.assertIn("30280", dumped)

    def test_total_from_headers(self):
        self.assertEqual(
            audio_mod.total_from_headers({"Content-Range": "bytes 0-1023/9390556"}), 9390556
        )
        self.assertEqual(audio_mod.total_from_headers({"Content-Length": "12345"}), 12345)
        self.assertEqual(audio_mod.total_from_headers({}), 0)


class TestCapability(unittest.TestCase):
    # 这些测试会碰 HF_* / BILIEX_ASR_* 环境变量，而 probe() 会**真的写** os.environ，
    # 所以每个用例前后都要清干净，否则用例之间会互相污染（实测踩过）。
    _ENV_PREFIXES = ("HF_", "BILIEX_ASR_")

    def setUp(self):
        self._saved = {
            key: value
            for key, value in os.environ.items()
            if key.startswith(self._ENV_PREFIXES)
        }
        for key in list(self._saved):
            os.environ.pop(key, None)

    def tearDown(self):
        for key in [k for k in os.environ if k.startswith(self._ENV_PREFIXES)]:
            os.environ.pop(key, None)
        os.environ.update(self._saved)

    def test_module_imports_without_the_optional_dependency(self):
        """没装 faster-whisper 时，导入 asr 模块本身必须安然无恙。"""
        self.assertTrue(hasattr(asr, "transcribe"))
        self.assertNotIn("faster_whisper", str(asr.__dict__.keys()))

    def test_missing_component_reports_actionable_hint(self):
        with mock.patch.object(asr, "_import_backend", side_effect=ImportError("no module")):
            report = asr.probe(refresh=True)
        self.assertFalse(report.available)
        self.assertIn("faster-whisper", report.reason)
        self.assertIn("pip install", report.hint)
        self.assertIn("不可用", report.summary())
        self.assertFalse(report.summary().startswith("本地 ASR 可用"))

    def test_available_with_backend(self):
        fake = mock.Mock(__version__="9.9.9")
        # 显式把 PyAV 兼容性检查钉成"没问题"，否则这条用例会取决于本机装了哪个 av
        with mock.patch.object(asr, "_av_incompatibility", return_value=None):
            report = asr.probe(backend=fake)
        self.assertTrue(report.available)
        self.assertEqual(report.version, "9.9.9")
        self.assertIn("cpu", report.devices)
        self.assertTrue(report.summary().startswith("本地 ASR 可用"))

    def test_probe_is_cached(self):
        first = asr.probe()
        self.assertIs(asr.probe(), first)

    def test_hf_endpoint_defaults_to_mirror_but_respects_user(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HF_ENDPOINT", None)
            os.environ.pop(asr.NO_MIRROR_ENV, None)
            endpoint, applied, _ = asr.ensure_hf_endpoint()
            self.assertEqual(endpoint, asr.MIRROR_ENDPOINT)
            self.assertTrue(applied)
            self.assertEqual(os.environ["HF_ENDPOINT"], asr.MIRROR_ENDPOINT)

        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://my.endpoint"}, clear=False):
            endpoint, applied, _ = asr.ensure_hf_endpoint()
            self.assertEqual(endpoint, "https://my.endpoint")
            self.assertFalse(applied)

        env = {"HF_ENDPOINT": "", asr.NO_MIRROR_ENV: "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("HF_ENDPOINT", None)
            endpoint, applied, _ = asr.ensure_hf_endpoint()
            self.assertEqual(endpoint, "")
            self.assertFalse(applied)

    def test_hub_timeouts_are_relaxed_but_never_override_user_values(self):
        """实测：镜像的 TLS 握手要几十秒，hub 默认 10 秒超时会直接判模型加载失败。"""
        _, mirrored, _ = asr.ensure_hf_endpoint()
        self.assertTrue(mirrored)
        self.assertEqual(os.environ["HF_HUB_DOWNLOAD_TIMEOUT"], "60")
        # 走镜像时同时关掉 xet（它连的是镜像不代理的专用端点）
        self.assertEqual(os.environ.get("HF_HUB_DISABLE_XET"), "1")

        # 用户自己设过就不能覆盖
        with mock.patch.dict(os.environ, {"HF_HUB_ETAG_TIMEOUT": "5"}, clear=False):
            asr.apply_hub_transport_defaults(mirrored=False)
            self.assertEqual(os.environ["HF_HUB_ETAG_TIMEOUT"], "5")

    def test_device_and_compute_type_resolution(self):
        cuda = asr.Capability(available=True, devices=["cuda", "cpu"], default_device="cuda")
        cpu = asr.Capability(available=True, devices=["cpu"], default_device="cpu")
        self.assertEqual(asr.resolve_device("auto", cuda), "cuda")
        self.assertEqual(asr.resolve_device("auto", cpu), "cpu")
        self.assertEqual(asr.resolve_device("cpu", cuda), "cpu")
        self.assertEqual(asr.resolve_compute_type("auto", "cuda"), "float16")
        self.assertEqual(asr.resolve_compute_type("auto", "cpu"), "int8")
        self.assertEqual(asr.resolve_compute_type("int8_float16", "cpu"), "int8_float16")

    def test_settings_env_defaults_and_overrides(self):
        with mock.patch.dict(os.environ, {"BILIEX_ASR_MODEL": "small"}, clear=False):
            settings = asr.AsrSettings.from_env()
            self.assertEqual(settings.model, "small")
            # 显式参数覆盖环境变量；None 不覆盖
            self.assertEqual(asr.AsrSettings.from_env(model="tiny").model, "tiny")
            self.assertEqual(asr.AsrSettings.from_env(model=None).model, "small")


class _FakeSegment:
    def __init__(self, start: float, end: float, text: str, logprob: float | None = None) -> None:
        self.start = start
        self.end = end
        self.text = text
        if logprob is not None:
            self.avg_logprob = logprob


class _FakeInfo:
    duration = 60.0
    language = "zh"
    language_probability = 0.99


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, path, **kwargs):
        self.calls.append({"path": path, **kwargs})
        return iter([_FakeSegment(0.0, 2.0, " 第一句 "), _FakeSegment(2.0, 60.0, "第二句")]), _FakeInfo()


class TestTranscribeMapping(unittest.TestCase):
    def test_maps_segments_to_cues_and_reports_speed(self):
        model = _FakeModel()
        settings = asr.AsrSettings(model="tiny", device="cpu", compute_type="int8")
        with mock.patch.object(
            asr,
            "build_model",
            return_value=(model, "cpu", "int8", [], asr.ModelRef("x", "modelscope")),
        ):
            progress: list[tuple[float, float]] = []
            result = asr.transcribe(
                Path("fake.m4s"), settings, progress=lambda d, t: progress.append((d, t))
            )

        self.assertEqual([c.text for c in result.cues], ["第一句", "第二句"])  # 已 strip
        self.assertEqual(result.audio_seconds, 60.0)
        self.assertAlmostEqual(result.coverage, 1.0)
        self.assertEqual(result.info["resolved_device"], "cpu")
        self.assertEqual(result.info["detected_language"], "zh")
        self.assertIn("speed_x", result.info)
        self.assertEqual(progress, [(2.0, 60.0), (60.0, 60.0)])
        # VAD 必须开着：B 站视频常有大段纯 BGM，不切掉会显著增加幻觉
        self.assertTrue(model.calls[0]["vad_filter"])
        self.assertEqual(model.calls[0]["language"], "zh")
        # 耗时/音频时长要进 info —— 人类可读输出与 meta.json 都读这里
        self.assertIn("elapsed_seconds", result.info)
        self.assertEqual(result.info["audio_seconds"], 60.0)

    def test_low_confidence_is_flagged_with_an_actionable_hint(self):
        """小模型在噪声/方言上会明显不确定；这个信号要能传到产物里。"""

        class _UnsureModel:
            def transcribe(self, path, **kwargs):
                return (
                    iter(
                        [
                            _FakeSegment(0, 10, "听不太清的一句", logprob=-1.8),
                            _FakeSegment(10, 60, "另一句", logprob=-1.4),
                        ]
                    ),
                    _FakeInfo(),
                )

        settings = asr.AsrSettings(model="tiny", device="cpu")
        with mock.patch.object(
            asr,
            "build_model",
            return_value=(_UnsureModel(), "cpu", "int8", [], asr.ModelRef("x", "modelscope")),
        ):
            result = asr.transcribe(Path("fake.m4s"), settings)

        self.assertAlmostEqual(result.info["avg_logprob"], -1.6, places=2)
        self.assertTrue(any("置信度偏低" in w and "--asr-model small" in w for w in result.warnings))

    def test_confident_output_is_not_flagged(self):
        class _SureModel:
            def transcribe(self, path, **kwargs):
                return (
                    iter([_FakeSegment(0, 60, "很清楚的一句", logprob=-0.2)]),
                    _FakeInfo(),
                )

        settings = asr.AsrSettings(model="small", device="cpu")
        with mock.patch.object(
            asr,
            "build_model",
            return_value=(_SureModel(), "cpu", "int8", [], asr.ModelRef("x", "modelscope")),
        ):
            result = asr.transcribe(Path("fake.m4s"), settings)
        self.assertFalse(any("置信度" in w for w in result.warnings))

    def test_language_auto_passes_none(self):
        model = _FakeModel()
        settings = asr.AsrSettings(model="tiny", language="auto")
        with mock.patch.object(
            asr,
            "build_model",
            return_value=(model, "cpu", "int8", [], asr.ModelRef("x", "modelscope")),
        ):
            asr.transcribe(Path("fake.m4s"), settings)
        self.assertIsNone(model.calls[0]["language"])

    def test_gpu_load_failure_falls_back_to_cpu(self):
        """缺 cuDNN 的 DLL 时，CTranslate2 有时是在**加载模型**这一步就报错的。"""
        calls: list[dict] = []

        class _Module:
            __version__ = "fake"

            @staticmethod
            def WhisperModel(name, **kwargs):
                calls.append(kwargs)
                if kwargs["device"] == "cuda":
                    raise RuntimeError("cudnn_ops_infer64_8.dll not found")
                return "cpu-model"

        with mock.patch.object(asr, "_import_backend", return_value=_Module), mock.patch.object(
            # 单元测试绝不能真的去下模型：把"权重从哪来"这一步钉死
            asr,
            "resolve_model_ref",
            side_effect=lambda model, **kwargs: asr.ModelRef(path=model, source="local"),
        ):
            capability = asr.Capability(available=True, devices=["cuda", "cpu"], default_device="cuda")
            settings = asr.AsrSettings(model="tiny")
            model, device, compute_type, warnings, ref = asr.build_model(settings, capability)

        self.assertEqual(model, "cpu-model")
        self.assertEqual(device, "cpu")
        self.assertEqual(compute_type, "int8")
        self.assertEqual(ref.source, "local")
        self.assertTrue(any("退回 CPU" in w for w in warnings))
        self.assertEqual([c["device"] for c in calls], ["cuda", "cpu"])


# ---------------------------------------------------------------- CUDA 运行时发现


class TestCudaRuntimeDiscovery(unittest.TestCase):
    """GPU 真正跑通的关键一环：pip 轮子里的 DLL 目录必须被注册进搜索路径。

    实测（2026-10-07，RTX 3070 Ti）：装上 `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`
    **并不够**。它们把 DLL 放在 `site-packages/nvidia/*/bin`，而那里不在 Windows 的
    DLL 搜索路径上，于是 `get_cuda_device_count()` 照样返回 1、
    `get_supported_compute_types("cuda")` 照样有值，直到推理才报
    `Library cublas64_12.dll is not found or cannot be loaded`。
    四种办法的对照（`tools/gpu_path_probe.py`）里，`os.add_dll_directory` 与
    `PATH` 前插能让推理成功，"什么都不做"失败。

    这些用例把「发现 → 注册 → 如实上报」钉死，且**都不需要真的装 CUDA**。
    """

    def setUp(self):
        self.tmp = _temp_dir()
        # 这三个是模块级缓存/句柄，用例之间必须复位，否则互相污染
        self._saved = (
            asr._CUDA_DLL_DIRS,
            list(asr._REGISTERED_DLL_DIRS),
            list(asr._DLL_DIR_HANDLES),
        )
        asr._CUDA_DLL_DIRS = None
        asr._REGISTERED_DLL_DIRS.clear()
        asr._DLL_DIR_HANDLES.clear()

    def tearDown(self):
        asr._CUDA_DLL_DIRS, registered, handles = self._saved
        asr._REGISTERED_DLL_DIRS[:] = registered
        asr._DLL_DIR_HANDLES[:] = handles
        if self.tmp.exists():
            _cleanup_tree(self.tmp)

    def _fake_site_packages(self) -> Path:
        root = self.tmp / "site-packages"
        for lib, dll in (("cublas", "cublas64_12.dll"), ("cudnn", "cudnn_ops64_9.dll")):
            directory = root / "nvidia" / lib / "bin"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / dll).write_bytes(b"MZ")
        return root

    def test_discovers_the_bin_dirs_of_pip_wheels(self):
        root = self._fake_site_packages()
        with mock.patch.object(asr, "_site_package_roots", return_value=[root]):
            dirs = asr.cuda_runtime_dirs()
        self.assertEqual(
            [d.name for d in dirs], ["bin", "bin"], "两个轮子的 bin 目录都要被发现"
        )
        self.assertEqual({d.parent.name for d in dirs}, {"cublas", "cudnn"})

    def test_empty_when_no_wheels_installed(self):
        """没装 nvidia 轮子时必须安静地返回空 —— 用系统 CUDA Toolkit 的用户走这条。"""
        root = self.tmp / "site-packages"
        (root / "faster_whisper").mkdir(parents=True)
        with mock.patch.object(asr, "_site_package_roots", return_value=[root]):
            self.assertEqual(asr.cuda_runtime_dirs(), [])
            self.assertEqual(asr.ensure_cuda_dll_dirs(), [])

    @unittest.skipUnless(os.name == "nt", "os.add_dll_directory 是 Windows 专有接口")
    def test_path_prepend_is_the_step_that_actually_works(self):
        """⚠️ 这条守的是**实测踩过的坑**：只有 PATH 对 ctranslate2 的 LoadLibraryA 有效。

        `os.add_dll_directory` 单独用是**没用**的 —— 曾经因为它"看起来注册成功了"
        （`asr status` 显示已注册 3 个目录）而误判为已修好，实际推理照样退回 CPU。
        所以这里必须断言 PATH 真的被改了。
        """
        root = self._fake_site_packages()
        with mock.patch.object(asr, "_site_package_roots", return_value=[root]), mock.patch.dict(
            os.environ, {"PATH": r"C:\Windows\System32"}, clear=False
        ):
            registered = asr.ensure_cuda_dll_dirs()
            parts = os.environ["PATH"].split(os.pathsep)

        self.assertEqual(len(registered), 2)
        for directory in registered:
            self.assertIn(directory, parts, "PATH 里必须真的有这个目录，否则 GPU 起不来")
        self.assertIn(r"C:\Windows\System32", parts, "原有 PATH 不能被丢掉")
        self.assertEqual(parts[-1], r"C:\Windows\System32", "应当是**前插**，不是追加")

    @unittest.skipUnless(os.name == "nt", "os.add_dll_directory 是 Windows 专有接口")
    def test_registers_dirs_once_and_keeps_handles_alive(self):
        """注册必须幂等，且句柄要一直被持有 —— 句柄被 GC 掉，目录就失效了。"""
        root = self._fake_site_packages()
        handles: list[object] = []

        def fake_add(path: str):
            handle = object()
            handles.append(handle)
            return handle

        with mock.patch.object(asr, "_site_package_roots", return_value=[root]), mock.patch.object(
            asr.os, "add_dll_directory", side_effect=fake_add
        ) as add, mock.patch.dict(os.environ, {"PATH": r"C:\Windows\System32"}, clear=False):
            first = asr.ensure_cuda_dll_dirs()
            path_after_first = os.environ["PATH"]
            second = asr.ensure_cuda_dll_dirs()
            path_after_second = os.environ["PATH"]

        self.assertEqual(len(first), 2)
        self.assertEqual(first, second)
        self.assertEqual(add.call_count, 2, "第二次调用不能再注册一遍")
        self.assertEqual(path_after_second, path_after_first, "第二次调用不该重复前插 PATH")
        self.assertEqual(len(asr._DLL_DIR_HANDLES), 2, "句柄必须被模块持有，不能只留在局部变量里")

    @unittest.skipUnless(os.name == "nt", "os.add_dll_directory 是 Windows 专有接口")
    def test_add_dll_directory_failure_does_not_break_the_path_route(self):
        """`os.add_dll_directory` 只是补充；它失败不该把已经生效的 PATH 方案一起拖死。"""
        root = self._fake_site_packages()
        with mock.patch.object(asr, "_site_package_roots", return_value=[root]), mock.patch.object(
            asr.os, "add_dll_directory", side_effect=OSError("nope")
        ), mock.patch.dict(os.environ, {"PATH": r"C:\Windows\System32"}, clear=False):
            registered = asr.ensure_cuda_dll_dirs()
            parts = os.environ["PATH"].split(os.pathsep)

        self.assertEqual(len(registered), 2)
        for directory in registered:
            self.assertIn(directory, parts)

    def test_capability_carries_cuda_runtime_into_dict(self):
        report = asr.Capability(available=True, cuda_runtime=[r"C:\x\nvidia\cublas\bin"])
        self.assertEqual(report.to_dict()["cuda_runtime"], [r"C:\x\nvidia\cublas\bin"])

    def test_probe_reports_what_was_actually_registered(self):
        fake = mock.Mock(__version__="9.9.9")
        with mock.patch.object(asr, "_av_incompatibility", return_value=None), mock.patch.object(
            asr, "ensure_cuda_dll_dirs", return_value=[r"C:\x\nvidia\cudnn\bin"]
        ):
            report = asr.probe(backend=fake)
        self.assertEqual(report.cuda_runtime, [r"C:\x\nvidia\cudnn\bin"])
        self.assertEqual(report.to_dict()["cuda_runtime"], [r"C:\x\nvidia\cudnn\bin"])

    def test_install_hint_names_both_wheels(self):
        """提示要能直接复制粘贴，且必须同时给出 cuBLAS 与 cuDNN —— 少一个都跑不起来。"""
        self.assertIn("nvidia-cublas-cu12", asr.CUDA_INSTALL_HINT)
        self.assertIn("nvidia-cudnn-cu12", asr.CUDA_INSTALL_HINT)

    def test_build_model_registers_dll_dirs_before_using_cuda(self):
        """用 GPU 之前必须先把目录注册好；用 CPU 时不该多此一举。"""
        calls: list[dict] = []

        class _Module:
            @staticmethod
            def WhisperModel(path, **kwargs):
                calls.append(kwargs)
                return "model"

        capability = asr.Capability(available=True, devices=["cuda", "cpu"], default_device="cuda")
        settings = asr.AsrSettings(model="tiny")
        with mock.patch.object(asr, "_import_backend", return_value=_Module), mock.patch.object(
            asr, "resolve_model_ref", side_effect=lambda model, **kw: asr.ModelRef(path=model, source="local")
        ), mock.patch.object(asr, "ensure_cuda_dll_dirs") as ensure:
            asr.build_model(settings, capability)
            self.assertEqual(ensure.call_count, 1)
            self.assertEqual([c["device"] for c in calls], ["cuda"])

        calls.clear()
        cpu_capability = asr.Capability(available=True, devices=["cpu"], default_device="cpu")
        with mock.patch.object(asr, "_import_backend", return_value=_Module), mock.patch.object(
            asr, "resolve_model_ref", side_effect=lambda model, **kw: asr.ModelRef(path=model, source="local")
        ), mock.patch.object(asr, "ensure_cuda_dll_dirs") as ensure:
            asr.build_model(settings, cpu_capability)
            self.assertEqual(ensure.call_count, 0, "纯 CPU 路径不该去碰 DLL 搜索路径")


# ---------------------------------------------------------------- L3 编排


def _capability() -> "asr.Capability":
    return asr.Capability(available=True, devices=["cpu"], default_device="cpu", version="fake")


def _asr_result(cues: list[Cue], *, audio_seconds: float = 60.0) -> "asr.AsrResult":
    result = asr.AsrResult(cues=cues, audio_seconds=audio_seconds)
    result.info = {"model": "tiny", "resolved_device": "cpu", "audio_seconds": audio_seconds}
    return result


class _StubClient:
    pass


def _audio_asset(directory: Path) -> "audio_mod.AudioAsset":
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "p1.m4s"
    path.write_bytes(b"audio-bytes")
    return audio_mod.AudioAsset(
        stream=audio_mod.pick_stream(DASH_PAYLOAD), path=path, bytes_written=11, expected_bytes=11
    )


class TestL3Chain(unittest.TestCase):
    TITLE = "新疆的农业与沙漠"

    def setUp(self):
        self.tmp = _temp_dir()
        # 音频落在**子目录**里，这样"转写完是否清理干净"可以被精确断言
        self.dest = self.tmp / "asr"
        self.settings = asr.AsrSettings(model="tiny", device="cpu", compute_type="int8")

    def tearDown(self):
        if self.tmp.exists():
            _cleanup_tree(self.tmp)

    def _patch(self, *, subtitle, asr_cues=None, available=True, transcribe_error=None,
               audio_error=None):
        """把 L2 与 L3 的**底层**换成桩，`run_local_asr` 的真实逻辑仍然执行。

        桩都记在 `self` 上，测试直接断言这些 mock 有没有被调用 ——
        不要在外层再 patch 同一个目标，那会把这里的桩覆盖掉。
        """
        self.probe_mock = mock.Mock(
            return_value=_capability()
            if available
            else asr.Capability(
                available=False,
                reason="未安装 faster-whisper",
                hint="pip install faster-whisper",
            )
        )
        self.official_mock = mock.Mock(return_value=(False, None, "L1 未命中"))
        self.subtitle_mock = mock.Mock(return_value=subtitle)
        self.audio_mock = mock.Mock(
            side_effect=audio_error
            if audio_error is not None
            else (lambda *a, **k: _audio_asset(self.dest))
        )
        self.transcribe_mock = mock.Mock(
            side_effect=transcribe_error
            if transcribe_error is not None
            else None,
            return_value=None
            if transcribe_error is not None
            else _asr_result(asr_cues or [], audio_seconds=60.0),
        )
        return [
            mock.patch("biliex.content.try_official", self.official_mock),
            mock.patch("biliex.content.fetch_subtitle", self.subtitle_mock),
            mock.patch("biliex.asr.probe", self.probe_mock),
            mock.patch("biliex.audio.fetch_audio", self.audio_mock),
            mock.patch("biliex.asr.transcribe", self.transcribe_mock),
        ]

    def _run(self, patches, **kwargs):
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return fetch_page(
            _StubClient(),
            "BV1",
            1,
            page=1,
            part="P1",
            duration=60.0,
            up_mid=1,
            title=self.TITLE,
            asr_dest_dir=self.dest,
            **kwargs,
        )

    def test_l3_picks_up_when_platform_has_nothing(self):
        patches = self._patch(
            subtitle=SubtitleOutcome(warnings=["player/v2 未返回字幕轨"]),
            asr_cues=[Cue(0, 30, "新疆的沙漠农业"), Cue(30, 60, "课本里的新疆")],
        )
        page = self._run(patches, asr_settings=self.settings)

        self.assertEqual(page.level, LEVEL_ASR)
        self.assertEqual(len(page.cues), 2)
        self.assertAlmostEqual(page.coverage, 1.0)
        self.assertFalse(page.unstable)
        self.assertEqual(page.asr["info"]["model"], "tiny")
        self.assertGreaterEqual(page.asr["audio"]["size_mb"], 0)
        self.assertFalse(page.asr_replaced_subtitle)
        self.assertTrue(any("L3" in r for r in page.fallback_reasons))
        # 音频默认不留（转写完即删，连空目录也一起清掉）
        self.assertFalse((self.dest / "p1.m4s").exists())
        self.assertFalse(self.dest.exists())

    def test_l3_disabled_by_default(self):
        patches = self._patch(subtitle=SubtitleOutcome(), asr_cues=[Cue(0, 60, "不该被用到")])
        page = self._run(patches)

        self.assertEqual(page.level, LEVEL_NONE)
        self.assertEqual(page.asr, {})
        self.audio_mock.assert_not_called()
        self.transcribe_mock.assert_not_called()

    def test_l3_replaces_untrustworthy_platform_subtitle(self):
        """L2 不可信 + L3 可用 → 用本地转写替换，并如实说明替换了。"""
        subtitle = SubtitleOutcome(
            cues=[Cue(0, 6, "只有开头一点点")], lan="ai-zh", is_ai=True, coverage=0.1, stable=False
        )
        patches = self._patch(
            subtitle=subtitle,
            asr_cues=[Cue(0, 30, "新疆的沙漠农业"), Cue(30, 60, "课本里的新疆")],
        )
        page = self._run(patches, asr_settings=self.settings)

        self.assertEqual(page.level, LEVEL_ASR)
        self.assertTrue(page.asr_replaced_subtitle)
        self.assertTrue(any("已被替换" in w for w in page.warnings))
        self.assertTrue(any("未通过三重校验" in r for r in page.fallback_reasons))

    def test_l3_not_triggered_when_subtitle_is_trustworthy(self):
        subtitle = SubtitleOutcome(
            cues=[Cue(0, 30, "新疆的沙漠农业"), Cue(30, 60, "课本里的新疆")],
            lan="zh-Hans",
            coverage=1.0,
            stable=True,
        )
        patches = self._patch(subtitle=subtitle, asr_cues=[Cue(0, 60, "多余")])
        page = self._run(patches, asr_settings=self.settings)

        self.assertEqual(page.level, LEVEL_SUBTITLE)
        self.assertEqual(page.asr, {})
        self.audio_mock.assert_not_called()
        self.transcribe_mock.assert_not_called()

    def test_l3_force_skips_official_and_platform(self):
        patches = self._patch(
            subtitle=SubtitleOutcome(), asr_cues=[Cue(0, 30, "新疆沙漠"), Cue(30, 60, "课本新疆")]
        )
        page = self._run(patches, asr_settings=self.settings, asr_force=True)

        self.official_mock.assert_not_called()
        self.subtitle_mock.assert_not_called()
        self.assertEqual(page.level, LEVEL_ASR)
        self.assertTrue(any("--asr-force" in r for r in page.fallback_reasons))

    def test_audio_failure_degrades_without_raising(self):
        patches = self._patch(
            subtitle=SubtitleOutcome(warnings=["没有字幕轨"]),
            asr_cues=[Cue(0, 60, "不该走到这里")],
            audio_error=audio_mod.AudioIncomplete("音频未下载完整（1 / 2 字节）"),
        )
        page = self._run(patches, asr_settings=self.settings)

        self.assertEqual(page.level, LEVEL_NONE)  # 退回 L2 的结果（这里本来就没有）
        self.assertTrue(any("取音频失败" in w for w in page.warnings))
        self.assertTrue(any("L3" in r for r in page.fallback_reasons))
        self.transcribe_mock.assert_not_called()

    def test_transcribe_crash_degrades_without_raising(self):
        patches = self._patch(
            subtitle=SubtitleOutcome(),
            transcribe_error=RuntimeError("CUDA out of memory"),
        )
        page = self._run(patches, asr_settings=self.settings)

        self.assertEqual(page.level, LEVEL_NONE)
        self.assertTrue(
            any("本地转写失败" in w and "CUDA out of memory" in w for w in page.warnings)
        )

    def test_component_missing_is_reported_with_install_hint(self):
        patches = self._patch(subtitle=SubtitleOutcome(), available=False)
        page = self._run(patches, asr_settings=self.settings)

        self.assertEqual(page.level, LEVEL_NONE)
        self.assertTrue(any("pip install faster-whisper" in w for w in page.warnings))
        self.audio_mock.assert_not_called()

    def test_audio_downloaded_when_keep_audio_is_set(self):
        settings = asr.AsrSettings(model="tiny", keep_audio=True)
        patches = self._patch(subtitle=SubtitleOutcome(), asr_cues=[Cue(0, 60, "新疆沙漠农业")])
        self._run(patches, asr_settings=settings)
        self.assertTrue((self.dest / "p1.m4s").exists())

    def test_short_audio_is_flagged_unstable(self):
        result = _asr_result([Cue(0, 10, "新疆沙漠")], audio_seconds=10.0)
        with mock.patch("biliex.asr.probe", return_value=_capability()), mock.patch(
            "biliex.audio.fetch_audio", return_value=_audio_asset(self.dest)
        ), mock.patch("biliex.asr.transcribe", return_value=result):
            outcome = run_local_asr(
                _StubClient(), "BV1", 1, page=1, duration=60.0,
                dest_dir=self.dest, settings=self.settings,
            )
        self.assertFalse(outcome.stable)
        self.assertLess(outcome.coverage, COVERAGE_THRESHOLD)
        self.assertTrue(any("明显短于视频时长" in w for w in outcome.warnings))
        self.assertTrue(any("只覆盖了" in w for w in outcome.warnings))

    def test_audio_file_cleanup_respects_keep_flag(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                settings = asr.AsrSettings(model="tiny", keep_audio=keep)
                asset = _audio_asset(self.dest)
                with mock.patch("biliex.asr.probe", return_value=_capability()), mock.patch(
                    "biliex.audio.fetch_audio", return_value=asset
                ), mock.patch(
                    "biliex.asr.transcribe",
                    return_value=_asr_result([Cue(0, 60, "新疆沙漠")]),
                ):
                    run_local_asr(
                        _StubClient(), "BV1", 1, page=1, duration=60.0,
                        dest_dir=self.dest, settings=settings,
                    )
                self.assertEqual(asset.path.exists(), keep)
                # 不留音频时，空目录也要收走
                self.assertEqual(self.dest.exists(), keep)


class TestAudioResume(unittest.TestCase):
    """断点续传的两种边界 —— 都是实跑踩出来的（HTTP 416）。"""

    def setUp(self):
        self.tmp = _temp_dir()
        self.stream = audio_mod.pick_stream(DASH_PAYLOAD)
        self.dest = self.tmp / "p1.m4s"

    def tearDown(self):
        _cleanup_tree(self.tmp)

    def test_416_with_complete_local_file_counts_as_done(self):
        """上一轮 `--keep-audio` 留下的完整文件：不该因为 416 而失败。"""
        self.dest.write_bytes(b"x" * 11)

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                getattr(request, "full_url", "u"), 416, "Range Not Satisfiable",
                {"Content-Range": "bytes */11"}, None,
            )

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = audio_mod.download(self.stream, self.dest)
        self.assertTrue(result.complete)
        self.assertEqual(result.bytes_written, 11)
        self.assertEqual(self.dest.read_bytes(), b"x" * 11)

    def test_416_with_stale_bigger_file_redownloads(self):
        """本地残留比服务端文件还大（换了音轨）→ 丢弃残留，从头下。"""
        self.dest.write_bytes(b"y" * 40)
        calls: list[str] = []

        class _Response:
            status = 200
            headers = {"Content-Length": "11"}

            def read(self, *_args):
                if getattr(self, "_sent", False):
                    return b""
                self._sent = True
                return b"z" * 11

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            calls.append(request.get_header("Range") or "")
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    getattr(request, "full_url", "u"), 416, "Range Not Satisfiable",
                    {"Content-Range": "bytes */11"}, None,
                )
            return _Response()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = audio_mod.download(self.stream, self.dest)

        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith("bytes=40-"))  # 第一次尝试续传
        self.assertEqual(calls[1], "")  # 第二次不带 Range，全量重下
        self.assertTrue(result.complete)
        self.assertEqual(self.dest.read_bytes(), b"z" * 11)


class TestCompatibilityGuards(unittest.TestCase):
    """两个实测踩到的版本/环境坑，都要在**下载音频之前**或**失败的那一刻**兜住。"""

    def test_av_19_is_rejected(self):
        problem = asr.check_av_compatibility("19.0.1")
        self.assertIsNotNone(problem)
        reason, hint = problem
        self.assertIn("PyAV", reason)
        self.assertIn("av<19", hint)

    def test_supported_av_versions_pass(self):
        for version in ("18.1.0", "17.1.0", "12.0.0", "15.0.0"):
            self.assertIsNone(asr.check_av_compatibility(version), version)

    def test_unparsable_version_is_not_a_false_alarm(self):
        for version in ("", "   ", "unknown", "v19"):
            self.assertIsNone(asr.check_av_compatibility(version), version)

    def test_probe_reports_av_conflict_as_unavailable(self):
        fake = mock.Mock(__version__="1.2.1")
        with mock.patch.object(
            asr,
            "_av_incompatibility",
            return_value=("已安装的 PyAV 19.0.1 与 faster-whisper 不兼容", asr.AV_FIX_HINT),
        ):
            report = asr.probe(backend=fake)
        self.assertFalse(report.available)
        self.assertIn("PyAV", report.reason)
        self.assertIn("av<19", report.hint)

    def test_cuda_library_error_detection(self):
        self.assertTrue(
            asr.looks_like_cuda_library_error(
                "Library cublas64_12.dll is not found or cannot be loaded"
            )
        )
        self.assertTrue(asr.looks_like_cuda_library_error("CUDA out of memory"))
        self.assertTrue(asr.looks_like_cuda_library_error("libcudnn.so.9: cannot open"))
        self.assertFalse(asr.looks_like_cuda_library_error("Invalid audio format"))
        self.assertFalse(asr.looks_like_cuda_library_error(""))

    def test_gpu_inference_failure_reruns_on_cpu(self):
        """实测：`get_cuda_device_count()` 与 `get_supported_compute_types('cuda')` 都正常，
        但真正推理时才报缺 `cublas64_12.dll` —— 所以兜底必须在推理这一步。"""

        class _CudaBroken:
            def transcribe(self, path, **kwargs):
                raise RuntimeError(
                    "Library cublas64_12.dll is not found or cannot be loaded"
                )

        good = _FakeModel()
        settings = asr.AsrSettings(model="tiny", device="cuda", compute_type="float16")
        ref = asr.ModelRef("x", "modelscope")
        builds = [
            (_CudaBroken(), "cuda", "float16", [], ref),
            (good, "cpu", "int8", [], ref),
        ]
        with mock.patch.object(asr, "build_model", side_effect=builds) as build:
            result = asr.transcribe(Path("fake.m4s"), settings)

        self.assertEqual([c.text for c in result.cues], ["第一句", "第二句"])
        self.assertEqual(result.info["resolved_device"], "cpu")
        self.assertEqual(result.info["resolved_compute_type"], "int8")
        self.assertTrue(any("已改用 CPU" in w for w in result.warnings))
        # 第二次是用 CPU 参数重建的
        second_settings = build.call_args_list[1].args[0]
        self.assertEqual(second_settings.device, "cpu")
        self.assertEqual(second_settings.compute_type, "int8")

    def test_non_cuda_failure_is_not_swallowed(self):
        class _Broken:
            def transcribe(self, path, **kwargs):
                raise RuntimeError("Invalid audio format")

        settings = asr.AsrSettings(model="tiny", device="cpu")
        with mock.patch.object(
            asr,
            "build_model",
            return_value=(_Broken(), "cpu", "int8", [], asr.ModelRef("x", "modelscope")),
        ):
            with self.assertRaises(RuntimeError):
                asr.transcribe(Path("fake.m4s"), settings)


class TestModelSource(unittest.TestCase):
    """权重从哪来 —— 实测 huggingface.co 不可达、hf-mirror 不稳，ModelScope 稳。"""

    def test_repo_ids_for_known_shortcuts(self):
        ids = asr.repo_ids("large-v3-turbo")
        self.assertEqual(ids["hf"], "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
        self.assertEqual(ids["modelscope"], "pengzhendong/faster-whisper-large-v3-turbo")
        self.assertEqual(asr.repo_ids("turbo")["hf"], ids["hf"])
        self.assertEqual(asr.repo_ids("large")["modelscope"], "Systran/faster-whisper-large-v3")

    def test_repo_ids_passthrough_for_full_names(self):
        ids = asr.repo_ids("someone/custom-ct2")
        self.assertEqual(ids["hf"], "someone/custom-ct2")
        self.assertEqual(ids["modelscope"], "someone/custom-ct2")

    def test_check_modelscope_does_not_depend_on_config_schema(self):
        """实测 tiny 的 config.json 只有 4 个键；按猜字段判定会把这条路静默判死。"""
        real_config = {"alignment_heads": [[2, 0]], "lang_ids": {}, "suppress_ids": []}
        with mock.patch.object(asr, "_http_get_json", return_value=real_config):
            self.assertTrue(asr.check_modelscope("Systran/faster-whisper-tiny"))

        with mock.patch.object(asr, "_http_get_json", return_value={"Code": 404, "Message": "no"}):
            self.assertFalse(asr.check_modelscope("x/y"))
        with mock.patch.object(asr, "_http_get_json", return_value={}):
            self.assertFalse(asr.check_modelscope("x/y"))
        with mock.patch.object(asr, "_http_get_json", side_effect=OSError("boom")):
            self.assertFalse(asr.check_modelscope("x/y"))

    def test_cache_dir_env_override(self):
        with mock.patch.dict(os.environ, {asr.CACHE_ENV: str(_TMP_ROOT)}, clear=False):
            self.assertEqual(asr.asr_cache_dir(), _TMP_ROOT)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(asr.CACHE_ENV, None)
            self.assertEqual(asr.asr_cache_dir().name, "models")

    def test_local_directory_is_used_as_is(self):
        directory = _temp_dir()
        self.addCleanup(directory.rmdir)
        ref = asr.resolve_model_ref(str(directory))
        self.assertEqual(ref.path, str(directory))
        self.assertEqual(ref.source, "local")

    def test_existing_weights_are_reused_without_network(self):
        cache = _temp_dir()
        self.addCleanup(lambda: _cleanup_tree(cache))
        weights = cache / asr.repo_ids("tiny")["modelscope"].replace("/", "__")
        weights.mkdir()
        for name in asr._REQUIRED_FILES:
            (weights / name).write_bytes(b"x")

        with mock.patch.dict(os.environ, {asr.CACHE_ENV: str(cache)}, clear=False), mock.patch.object(
            asr, "check_modelscope", side_effect=AssertionError("不该联网")
        ):
            ref = asr.resolve_model_ref("tiny")
        self.assertEqual(ref.path, str(weights))
        self.assertEqual(ref.source, "modelscope")
        self.assertTrue(ref.cached)

    def test_auto_falls_back_to_huggingface_when_modelscope_unreachable(self):
        cache = _temp_dir()
        self.addCleanup(lambda: _cleanup_tree(cache))
        with mock.patch.dict(os.environ, {asr.CACHE_ENV: str(cache)}, clear=False), mock.patch.object(
            asr, "check_modelscope", return_value=False
        ):
            ref = asr.resolve_model_ref("tiny")
        self.assertEqual(ref.path, "tiny")
        self.assertEqual(ref.source, "huggingface")
        self.assertEqual(ref.repo, "Systran/faster-whisper-tiny")

    def test_explicit_modelscope_source_fails_loudly(self):
        cache = _temp_dir()
        self.addCleanup(lambda: _cleanup_tree(cache))
        with mock.patch.dict(os.environ, {asr.CACHE_ENV: str(cache)}, clear=False), mock.patch.object(
            asr, "check_modelscope", return_value=False
        ):
            with self.assertRaises(asr.UpstreamChanged):
                asr.resolve_model_ref("tiny", source="modelscope")

    def test_huggingface_source_never_touches_modelscope(self):
        with mock.patch.object(
            asr, "check_modelscope", side_effect=AssertionError("不该探测 ModelScope")
        ):
            ref = asr.resolve_model_ref("small", source="huggingface")
        self.assertEqual(ref.path, "small")
        self.assertEqual(ref.source, "huggingface")

    def test_download_skips_already_complete_files(self):
        dest = _temp_dir()
        self.addCleanup(lambda: _cleanup_tree(dest))
        (dest / "config.json").write_bytes(b"already-here")
        listing = [
            {"Path": "config.json", "Size": 12},  # 已完整 → 不重下
            {"Path": "README.md", "Size": 5},  # 说明文件 → 跳过
            {"Path": "tokenizer.json", "Size": 7},  # 缺 → 必须下
        ]
        seen: list[str] = []

        def fake_urlopen(request, timeout=None):
            seen.append(getattr(request, "full_url", str(request)))

            class _Response:
                def read(self, *_args):
                    if getattr(self, "_done", False):
                        return b""
                    self._done = True
                    return b"payload"

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return _Response()

        with mock.patch.object(asr, "modelscope_files", return_value=listing), mock.patch(
            "urllib.request.urlopen", fake_urlopen
        ):
            asr.download_from_modelscope("x/y", dest)

        self.assertEqual((dest / "config.json").read_bytes(), b"already-here")  # 没有重下
        self.assertEqual(len(seen), 1)
        self.assertIn("tokenizer.json", seen[0])
        self.assertFalse((dest / "README.md").exists())

    def test_has_weights_requires_core_files(self):
        directory = _temp_dir()
        self.addCleanup(lambda: _cleanup_tree(directory))
        self.assertFalse(asr.has_weights(directory))
        for name in asr._REQUIRED_FILES:
            (directory / name).write_bytes(b"x")
        self.assertTrue(asr.has_weights(directory))


def _cleanup_tree(directory: Path) -> None:
    """递归删掉一个临时目录（递归里会删掉子目录本身，外层不要再删一次）。"""
    for item in directory.glob("*"):
        if item.is_dir():
            _cleanup_tree(item)
        else:
            item.unlink(missing_ok=True)
    directory.rmdir()


class TestSourceNote(unittest.TestCase):
    """index.md 里那句"数据来源"必须随级别精确变化。"""

    def test_official_is_attributed_to_bilibili(self):
        note = source_note({LEVEL_OFFICIAL})
        self.assertIn("由 B 站生成", note)

    def test_local_asr_is_never_attributed_to_bilibili(self):
        note = source_note({LEVEL_ASR})
        self.assertIn("本机", note)
        self.assertIn("不是 B 站提供的字幕", note)

    def test_mixed_levels_mention_both_sources(self):
        note = source_note({LEVEL_ASR, LEVEL_SUBTITLE})
        self.assertIn("平台字幕", note)
        self.assertIn("本机 ASR", note)

    def test_metadata_only(self):
        self.assertIn("只有元数据", source_note({LEVEL_NONE}))

    def test_subtitle_and_metadata_only(self):
        note = source_note({LEVEL_SUBTITLE, LEVEL_NONE})
        self.assertIn("只有元数据", note)

    def test_page_dict_carries_asr_provenance(self):
        page = PageContent(cid=1, page=1, part="P1", duration=10.0, level=LEVEL_ASR)
        page.asr = {"info": {"model": "tiny"}}
        payload = page.to_dict()
        self.assertEqual(payload["asr"]["info"]["model"], "tiny")
        self.assertFalse(payload["asr_replaced_subtitle"])


if __name__ == "__main__":
    unittest.main()
