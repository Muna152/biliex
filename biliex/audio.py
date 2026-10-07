"""音频获取层（L3 本地 ASR 的第一半）：取播放地址 + 下载音频流。

**这一层只用标准库** —— 它属于可选 ASR 功能，但本身**不需要任何第三方依赖**。
只有真正做转写的 `biliex/asr.py` 才需要 `faster-whisper`。
因此"要不要装 ASR"这件事，只影响 `asr.py` 与那一个 pip extra。

安全约定（与 `http.py` 的整体约定一致，但这里有三条刻意的差异）：

* 取 `playurl` 时**带** Cookie —— 它是 `api.bilibili.com` 的接口，登录态能拿到更好的音轨；
* 下载音频流时**不带** Cookie —— CDN 域名（`*.bilivideo.cn` / `*.bilivideo.com`）不属于
  `bilibili.com`，URL 自带短期签名（`upsig`/`deadline`），不需要也不应该带上账号凭据；
* 落盘/上报的音轨元数据**剔除**签名 URL —— 它们是几分钟就过期的临时令牌，
  留档没有价值，留在磁盘上只是多一份可能被误用的链接。

实测（2026-10-07，未登录、免 WBI 签名）：`/x/player/playurl` 返回 `dash.audio` 三条音轨
（30216 / 30232 / 30280），带 `Range` 请求返回 `206`，且 `Content-Range` 给出**文件总长度** ——
这个总长度正好可以用来判断"音频到底下全了没有"，是 L3 版的完整性判据。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlparse

from . import config
from .errors import BiliexError, NetworkError

# dash 音频流的 id 优先级（数值越大越好）。30280 = 192K，30232 = 132K，30216 = 64K。
_AUDIO_ID_RANK = {30280: 30, 30232: 20, 30216: 10}

# 下载分块大小（256 KiB）
_CHUNK = 256 * 1024


class StaleAudioUrl(BiliexError):
    """音频 URL 已过期（CDN 签名有时效）。

    调用方应当**重新取一次 playurl** 再下载，而不是重试同一个 URL。
    """

    code = "stale_audio_url"


class AudioIncomplete(BiliexError):
    """音频没下完。

    宁可失败也不要拿半截音频去转写 —— 那会产出一份"看起来正常、实际缺了后半段"的转录，
    恰好是最危险的那种错误结果。
    """

    code = "audio_incomplete"


@dataclass
class AudioStream:
    """一条可下载的音轨。"""

    url: str
    backups: list[str] = field(default_factory=list)
    stream_id: int = 0
    bandwidth: int = 0
    mime: str = ""
    codecs: str = ""
    source: str = "dash"
    segment_count: int = 1

    @property
    def extension(self) -> str:
        """下载文件用的扩展名（只影响可读性，解码靠容器自动探测）。"""
        return ".m4s" if self.source == "dash" else ".mp4"

    def urls(self) -> list[str]:
        """候选 URL：主 URL 在前，备用 CDN 在后。"""
        seen: list[str] = []
        for url in [self.url, *self.backups]:
            if url and url not in seen:
                seen.append(url)
        return seen

    def to_dict(self) -> dict:
        """落档用：**不含签名 URL**，只保留可核对的元数据。"""
        return {
            "stream_id": self.stream_id,
            "bandwidth": self.bandwidth,
            "mime": self.mime,
            "codecs": self.codecs,
            "source": self.source,
            "segment_count": self.segment_count,
            "host": urlparse(self.url).netloc,
            "backup_count": len(self.backups),
        }


@dataclass
class AudioAsset:
    """下载完成的音频文件。"""

    stream: AudioStream
    path: Path
    bytes_written: int
    expected_bytes: int = 0

    @property
    def size_mb(self) -> float:
        return round(self.bytes_written / 1024 / 1024, 2)

    def to_dict(self) -> dict:
        return {
            "stream": self.stream.to_dict(),
            "file": self.path.name,
            "bytes": self.bytes_written,
            "expected_bytes": self.expected_bytes,
            "size_mb": self.size_mb,
        }


@dataclass
class DownloadResult:
    path: Path
    bytes_written: int
    expected_bytes: int = 0
    complete: bool = False


# ---------------------------------------------------------------- 选择音轨


def _id_rank(stream: dict) -> int:
    try:
        return _AUDIO_ID_RANK.get(int(stream.get("id") or 0), 0)
    except (TypeError, ValueError):
        return 0


def pick_stream(data: dict) -> AudioStream | None:
    """从 playurl 的 `data` 里挑一条最合适的音轨。

    选择规则：**带宽最高者优先，同带宽按音质 id 排序**。
    刻意不选 FLAC / 杜比：转写用不到那点音质，体积与解码成本却高得多。
    """
    dash = data.get("dash") or {}
    candidates = [
        item
        for item in (dash.get("audio") or [])
        if isinstance(item, dict) and (item.get("baseUrl") or item.get("base_url"))
    ]
    if candidates:
        best = max(
            candidates,
            key=lambda item: (int(item.get("bandwidth") or 0), _id_rank(item)),
        )
        backups = [b for b in (best.get("backupUrl") or best.get("backup_url") or []) if b]
        return AudioStream(
            url=best.get("baseUrl") or best.get("base_url") or "",
            backups=backups,
            stream_id=int(best.get("id") or 0),
            bandwidth=int(best.get("bandwidth") or 0),
            mime=str(best.get("mimeType") or best.get("mime_type") or ""),
            codecs=str(best.get("codecs") or ""),
            source="dash",
        )

    # durl 是老的整段 flv/mp4 路径，仅在 dash 缺失时兜底。
    # 注意：长视频的 durl 会被切成多段（order=1,2,3...），需要拼接，
    # 本工具当前不处理多段 —— 如实报错，而不是拼出一份缺内容的结果。
    durl = data.get("durl") or []
    if durl:
        ordered = sorted(durl, key=lambda item: item.get("order") or 0)
        first = ordered[0]
        url = first.get("url") or ""
        if url:
            return AudioStream(
                url=url,
                backups=[b for b in (first.get("backup_url") or []) if b],
                bandwidth=int(first.get("size") or 0),
                source="durl",
                segment_count=len(ordered),
            )
    return None


def streams_metadata(data: dict) -> dict:
    """落档用的音轨概览（**剔除签名 URL**）。"""
    dash = data.get("dash") or {}
    return {
        "accept_quality": data.get("accept_quality"),
        "timelength_ms": data.get("timelength"),
        "dash_audio_ids": [int(item.get("id") or 0) for item in (dash.get("audio") or [])],
        "has_flac": bool((dash.get("flac") or {}).get("audio")),
        "has_dolby": bool((dash.get("dolby") or {}).get("audio")),
        "durl_segments": len(data.get("durl") or []),
    }


# ---------------------------------------------------------------- 下载


def download_headers(*, offset: int = 0) -> dict[str, str]:
    """下载音频流用的请求头。

    **刻意不带 Cookie**：CDN 不属于 `bilibili.com`，URL 自带签名。
    这条是安全约定，有单元测试守卫。
    """
    headers = {
        "User-Agent": config.USER_AGENT,
        "Referer": config.REFERER,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "close",
    }
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
    return headers


def total_from_headers(headers) -> int:
    """从 `Content-Range` 或 `Content-Length` 推出文件总长度（推不出就返回 0）。"""
    content_range = headers.get("Content-Range") or ""
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    length = headers.get("Content-Length")
    if length and str(length).isdigit():
        return int(length)
    return 0


def _iter_chunks(response) -> Iterator[bytes]:
    while True:
        chunk = response.read(_CHUNK)
        if not chunk:
            return
        yield chunk


def download(
    stream: AudioStream,
    dest: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    attempts: int = 3,
) -> DownloadResult:
    """把音轨下载到 `dest`。

    支持**断点续传**：中途失败后带 `Range: bytes=N-` 从断点继续。
    一条 1 小时的音轨可能有几十 MB，失败就从零再来代价太大。

    URL 过期（403/410/451）抛 `StaleAudioUrl` —— 那是**换 URL** 才能解决的事，
    重试同一个 URL 只会一直失败。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = stream.urls()
    expected = 0
    last_error: BiliexError | None = None

    for attempt in range(1, attempts + 1):
        url = urls[min(attempt - 1, len(urls) - 1)]
        offset = dest.stat().st_size if dest.exists() else 0
        try:
            request = urllib.request.Request(url, headers=download_headers(offset=offset))
            with urllib.request.urlopen(request, timeout=60) as response:
                expected = total_from_headers(response.headers)
                if offset and response.status != 206:
                    offset = 0  # 服务端忽略了 Range，只能从头写
                written = offset
                with open(dest, "ab" if offset else "wb") as handle:
                    for chunk in _iter_chunks(response):
                        handle.write(chunk)
                        written += len(chunk)
                        if progress is not None:
                            progress(written, expected or written)
            last_error = None
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 410, 451):
                raise StaleAudioUrl(
                    f"音频 URL 已失效（HTTP {exc.code}），需要重新取播放地址"
                ) from None
            if exc.code == 416:
                # 416 = Range 起点超出文件长度。两种情况，都要处理：
                #  a) 本地这份**已经完整**（多半是上次 `--keep-audio` 留下的）→ 直接用；
                #  b) 本地残留与服务端文件**不一致** → 删掉重下。
                # 判据必须是 `written == total` 而不是 `>=`：比服务端还大说明本地这份
                # 根本不是这个 URL 的文件（例如换了音轨），拿它去转写就是错的结果。
                total = total_from_headers(exc.headers)
                written = dest.stat().st_size if dest.exists() else 0
                if total and written == total:
                    return DownloadResult(dest, written, total, complete=True)
                dest.unlink(missing_ok=True)
                last_error = NetworkError("断点续传失败（HTTP 416），已丢弃残留文件重试")
                continue
            last_error = NetworkError(f"下载音频失败（HTTP {exc.code}）")
        except urllib.error.URLError as exc:
            last_error = NetworkError(f"下载音频时网络中断：{exc.reason}")
        except OSError as exc:
            last_error = NetworkError(f"写入音频文件失败：{exc}")

        written = dest.stat().st_size if dest.exists() else 0
        if written and expected and written >= expected:
            return DownloadResult(dest, written, expected, complete=True)
        if written and not expected and last_error is None:
            # 服务端没给长度，只能以"这一次读完了"为准
            return DownloadResult(dest, written, 0, complete=True)

    if last_error is not None:
        raise last_error

    written = dest.stat().st_size if dest.exists() else 0
    return DownloadResult(dest, written, expected, complete=False)


# ---------------------------------------------------------------- 组合


def fetch_audio(
    client,
    bvid: str,
    cid: int,
    *,
    dest_dir: Path,
    prefix: str = "audio",
    progress: Callable[[int, int], None] | None = None,
    raw_sink: dict | None = None,
) -> AudioAsset:
    """取播放地址并下载音频，供本地 ASR 使用。

    地址过期时**自动重取一次** —— CDN 签名有时效，这是实测会遇到的情况。
    """
    from .errors import ContentUnavailable

    def _prepare(sink_key: str) -> AudioStream:
        data = client.playurl(bvid, cid)
        stream = pick_stream(data)
        if stream is None:
            raise ContentUnavailable("playurl 未返回任何音轨（该视频可能没有音频流）")
        if stream.segment_count > 1:
            raise ContentUnavailable(
                f"该视频的音轨是 {stream.segment_count} 段 durl 格式，本工具当前只支持 dash 音轨，"
                "无法完整获取"
            )
        if raw_sink is not None:
            raw_sink[sink_key] = streams_metadata(data)
        return stream

    stream = _prepare("playurl")
    dest = dest_dir / f"{prefix}{stream.extension}"
    try:
        result = download(stream, dest, progress=progress)
    except StaleAudioUrl:
        stream = _prepare("playurl_retry")
        dest = dest_dir / f"{prefix}{stream.extension}"
        result = download(stream, dest, progress=progress)

    if not result.complete:
        raise AudioIncomplete(
            f"音频未下载完整（{result.bytes_written} / "
            f"{result.expected_bytes or '未知'} 字节）"
        )

    return AudioAsset(
        stream=stream,
        path=result.path,
        bytes_written=result.bytes_written,
        expected_bytes=result.expected_bytes,
    )


def cleanup(path: Path) -> bool:
    """删除临时的音频文件。返回是否真的删掉了。"""
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        return False
    return False


__all__ = [
    "AudioAsset",
    "AudioIncomplete",
    "AudioStream",
    "DownloadResult",
    "StaleAudioUrl",
    "cleanup",
    "download",
    "download_headers",
    "fetch_audio",
    "pick_stream",
    "streams_metadata",
    "total_from_headers",
]
