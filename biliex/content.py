"""内容获取层：四级降级链。

    L1  view/conclusion/get   官方 AI 总结（摘要 + 分段提纲 + AI 字幕，均带时间戳）
    L2  player/v2 → 字幕 JSON  人工 CC 字幕或 AI 字幕
    L3  本地 ASR              下载音频 → faster-whisper 转写（**可选组件**，需额外安装）
    L4  仅元数据               任何情况下都能给出输出

设计要点：
* 每一级的失败原因都要**记录下来**并在最终输出里体现，便于判断为什么降级；
* L2 必须做「假残缺」防护：批量/高频请求时 B 站会返回**内容被截断**的字幕
  （实测有 6 条 vs 单独重请求 373 条的案例），靠时长覆盖率 + 重试来兜。
* L3 是**可选**的：`biliex/audio.py`（取音频，零第三方依赖）与 `biliex/asr.py`
  （转写，需要 faster-whisper）在这里被**惰性导入**，没装也不影响 L1/L2/L4 的任何一行。
* L3 还与 L2 有一处关键差异：L2 遇到不可信字幕时**只能如实标注**，
  而 L3 一旦可用，就有能力**用本地转写替换掉那份可疑的平台字幕**。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .api import Client
from .errors import BiliexError

if TYPE_CHECKING:  # 仅在类型检查时引入，运行时保持可选组件"可缺席"
    from .asr import AsrSettings

# 命中哪一级
LEVEL_OFFICIAL = "official_ai_summary"
LEVEL_SUBTITLE = "subtitle"
LEVEL_ASR = "local_asr"
LEVEL_NONE = "none"

# 字幕「假残缺」判据：最后一条字幕的结束时间 / 视频时长。
# 低于这个比例就认为内容被截断，需要重取。
COVERAGE_THRESHOLD = 0.90

# L2 最多重取几轮
SUBTITLE_ATTEMPTS = 4


@dataclass
class Cue:
    """一条字幕。"""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "text": self.text}


@dataclass
class OutlinePoint:
    timestamp: float
    content: str


@dataclass
class OutlineSection:
    title: str
    timestamp: float
    points: list[OutlinePoint] = field(default_factory=list)


@dataclass
class PageContent:
    """单个分 P 的内容。"""

    cid: int
    page: int
    part: str
    duration: float
    level: str = LEVEL_NONE
    summary: str = ""
    outline: list[OutlineSection] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    subtitle_lan: str = ""
    subtitle_is_ai: bool = False
    coverage: float = 0.0
    # 两次读取不一致或覆盖率不达标时为 True —— 表示内容不可信，不能当完整内容用。
    unstable: bool = False
    # 字幕与标题的关键词相关性（仅对 L2 有意义；L1 的关联由 B 站保证正确）
    relevance_matched: int = 0
    relevance_total: int = 0
    missing_keywords: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fallback_reasons: list[str] = field(default_factory=list)
    # L3 本地转写的元数据（模型/设备/耗时/音频大小等）。空 dict = 没走 L3。
    asr: dict = field(default_factory=dict)
    # 本次内容是否由 L3 **替换**掉了不可信的 L2 字幕
    asr_replaced_subtitle: bool = False
    # 上游原始载荷留档（作为证据，出现争议时可回查）。
    # 刻意**不**存完整字幕 JSON —— 它与 transcript 文件重复，且体积大。
    raw: dict = field(default_factory=dict)

    @property
    def low_relevance(self) -> bool:
        """标题关键词一个都没命中 —— 强烈提示这份字幕不是本视频的。"""
        return self.relevance_total > 0 and self.relevance_matched == 0

    def to_dict(self) -> dict:
        return {
            "cid": self.cid,
            "page": self.page,
            "part": self.part,
            "duration": self.duration,
            "level": self.level,
            "summary": self.summary,
            "outline": [
                {
                    "title": s.title,
                    "timestamp": s.timestamp,
                    "points": [
                        {"timestamp": p.timestamp, "content": p.content}
                        for p in s.points
                    ],
                }
                for s in self.outline
            ],
            "cue_count": len(self.cues),
            "subtitle_lan": self.subtitle_lan,
            "subtitle_is_ai": self.subtitle_is_ai,
            "coverage": round(self.coverage, 4),
            "unstable": self.unstable,
            "relevance": {
                "matched": self.relevance_matched,
                "total": self.relevance_total,
                "missing_keywords": self.missing_keywords,
                "low_relevance": self.low_relevance,
            },
            "warnings": self.warnings,
            "fallback_reasons": self.fallback_reasons,
            "asr": self.asr,
            "asr_replaced_subtitle": self.asr_replaced_subtitle,
        }


# ---------------------------------------------------------------- L1


def _parse_official(data: dict) -> tuple[str, list[OutlineSection], list[Cue]]:
    """解析 `conclusion/get` 的 model_result。"""
    model = data.get("model_result") or {}
    summary = model.get("summary") or ""

    outline: list[OutlineSection] = []
    for section in model.get("outline") or []:
        points = [
            OutlinePoint(
                timestamp=float(p.get("timestamp") or 0),
                content=(p.get("content") or "").strip(),
            )
            for p in (section.get("part_outline") or [])
        ]
        outline.append(
            OutlineSection(
                title=(section.get("title") or "").strip(),
                timestamp=float(section.get("timestamp") or 0),
                points=points,
            )
        )

    cues: list[Cue] = []
    for track in model.get("subtitle") or []:
        for item in track.get("part_subtitle") or []:
            text = (item.get("content") or "").strip()
            if not text:
                continue
            cues.append(
                Cue(
                    start=float(item.get("start_timestamp") or 0),
                    end=float(item.get("end_timestamp") or 0),
                    text=text,
                )
            )

    cues.sort(key=lambda c: c.start)
    return summary, outline, cues


def try_official(client: Client, bvid: str, cid: int, up_mid: int | None) -> tuple[bool, dict | None, str]:
    """尝试 L1。返回 (是否命中, 数据, 未命中原因)。"""
    try:
        data = client.conclusion_get(bvid, cid, up_mid)
    except BiliexError as exc:
        return False, None, f"L1 调用失败({exc.code}): {exc.message}"

    status = data.get("code")
    if status == 0:
        return True, data, ""
    if status == 1:
        return False, None, "L1 无摘要（未识别到语音）"
    if status == -1:
        return False, None, "L1 不支持 AI 摘要（敏感内容等）"
    return False, None, f"L1 返回未知状态码 {status}"


# ---------------------------------------------------------------- L2


def _score_track(track: dict) -> tuple[int, bool]:
    """给字幕轨打分。返回 (分数, 是否为 AI 字幕)。

    优先级：人工中文 CC 字幕 > AI 中文字幕 > 其它中文 > 任意。
    人工字幕质量通常高于 AI 自动生成，因此优先。
    """
    lan = str(track.get("lan") or "").lower()
    is_ai = lan.startswith("ai-") or "自动生成" in str(track.get("lan_doc") or "")
    if not track.get("subtitle_url"):
        return -1, is_ai

    score = 0
    if lan in {"zh-hans", "zh-cn"}:
        score = 100
    elif lan in {"zh-hant", "zh-tw"}:
        score = 90
    elif lan.startswith("zh") or lan == "ai-zh":
        score = 80
    elif lan.startswith("ai-"):
        score = 60
    elif lan.startswith("en"):
        score = 20
    else:
        score = 10

    if is_ai:
        score -= 15  # 同等语言下，人工字幕优先
    return score, is_ai


def _pick_track(tracks: list[dict]) -> tuple[dict | None, bool]:
    candidates = []
    for track in tracks:
        score, is_ai = _score_track(track)
        if score >= 0:
            candidates.append((score, is_ai, track))
    if not candidates:
        return None, False
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, is_ai, track = candidates[0]
    return track, is_ai


def _parse_subtitle_body(data: dict) -> list[Cue]:
    cues = []
    for item in data.get("body") or []:
        text = (item.get("content") or "").strip()
        if not text:
            continue
        cues.append(
            Cue(
                start=float(item.get("from") or 0),
                end=float(item.get("to") or 0),
                text=text,
            )
        )
    cues.sort(key=lambda c: c.start)
    return cues


def _coverage(cues: list[Cue], duration: float) -> float:
    if not cues or duration <= 0:
        return 0.0
    return min(cues[-1].end / duration, 1.0)


# 标题里的通用词/常见搭配，做相关性判断时剔除。
# 为什么连 2-gram 也要滤：像「里的」「不是」「这样」在任意文本里都常见，
# 留着会让"零命中"这个判据失效（错误内容也会被误判成命中）。
_TITLE_STOPWORDS = {
    # 2-gram 常见搭配
    "里的", "的新", "的一", "了的", "是在", "在的", "有的", "不是", "这样", "那样",
    "什么", "怎么", "为什", "这个", "那个", "我们", "你们", "他们", "自己", "可以",
    "没有", "就是", "一直", "已经", "还是", "但是", "因为", "所以", "如果", "然后",
    "时候", "东西", "事情", "地方", "大家", "朋友", "记得", "知道", "觉得", "真的",
    "非常", "特别", "一个", "一下", "开始", "如何", "以及", "还有", "只是", "不过",
    # 3-gram / 整词
    "我记得", "记得课", "得课本", "课本里", "本里的",
    # 通用内容词
    "视频", "系列", "全集", "合集", "第一", "第二", "第三", "今天", "明天", "昨天",
    "抢先", "预告", "解说", "完整", "高清", "官方", "中文", "字幕",
}

# 虚词字符。含这些字的 n-gram 一律不算"内容词"。
#
# 为什么必须滤：实测中错误字幕的"命中"全部是 `是这样` / `这样的` / `是这` 这类虚词组合 ——
# 它们在任意长文本里都会出现，会让"零命中"这个判据彻底失效。
# 滤掉之后，只有 `新疆`、`课本` 这类真内容词参与判定，错误内容就露出来了。
_FUNCTION_CHARS = set(
    "的了是在有和与就都也我你他她它们这那不没把被让给对从到而并但还只很太更最"
    "一二三上下里外个之其所以及或如若则于"
)

# 内容词少于这个数量就不做相关性判定 —— 样本太少容易误伤
_MIN_KEYWORDS_FOR_RELEVANCE = 2


def _title_keywords(text: str, *, limit: int = 12) -> list[str]:
    """从标题里抽出**内容词**，用于相关性判断。

    没有中文分词器，用 n-gram 近似：先取 3-gram（更有区分度），再取 2-gram，
    最后加英文/数字词；含虚词的组合一律丢弃。

    ⚠️ 证据基础：这个判据目前只在**一个真实错配案例**上验证过
    （标题讲新疆农业，`player/v2` 返回的却是讲"顶层设计"的另一个视频）。
    它只用于**提醒**，绝不自动丢弃内容 —— 误报的代价是让 agent 多看一眼，
    漏报的代价是总结建立在错误素材上，两者不对称，所以宁可提醒。
    """
    text = text or ""
    out: list[str] = []

    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        for n in (3, 2):
            for i in range(len(run) - n + 1):
                token = run[i : i + n]
                if token in _TITLE_STOPWORDS or token in out:
                    continue
                if any(ch in _FUNCTION_CHARS for ch in token):
                    continue
                out.append(token)

    for token in re.findall(r"[A-Za-z0-9]{3,}", text):
        if token not in out:
            out.append(token)

    return out[:limit]


def _relevance(text: str, cues: list[Cue]) -> tuple[int, int, list[str]]:
    """字幕内容与标题的相关性。

    为什么需要这个判据：实测发现 `player/v2` 的 AI 字幕接口会返回**完全属于另一个视频**
    的内容（标题讲新疆农业，字幕却在讲"顶层设计"），而且每次请求还不一样。
    双读一致性只能挡住"每次不同"，挡不住"每次都错但错得一致"。

    返回 (命中数, 内容词总数, 未命中的内容词)。内容词不足时返回 (0, 0, [])，表示无法判定。
    """
    keywords = _title_keywords(text)
    if len(keywords) < _MIN_KEYWORDS_FOR_RELEVANCE or not cues:
        return 0, 0, []
    joined = "".join(cue.text for cue in cues)
    missing = [kw for kw in keywords if kw not in joined]
    return len(keywords) - len(missing), len(keywords), missing


@dataclass
class SubtitleOutcome:
    """一次字幕获取的结果。"""

    cues: list[Cue] = field(default_factory=list)
    lan: str = ""
    is_ai: bool = False
    coverage: float = 0.0
    # 两次独立读取是否一致。False 说明上游对同一 cid 返回了不同内容。
    stable: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.cues) and self.coverage >= COVERAGE_THRESHOLD and self.stable


def _track_signature(cues: list[Cue]) -> tuple:
    """能代表"这份字幕是哪一份"的指纹。

    用于判断两次独立读取拿到的**是不是同一份字幕**。
    真实的（稳定的）字幕是 CDN 上的同一个文件，两次读取应当完全一致。
    """
    if not cues:
        return (0, 0, ())
    return (
        len(cues),
        round(cues[-1].end),
        tuple(cue.text[:12] for cue in cues[:3]),
    )


def _same_content(a: list[Cue], b: list[Cue]) -> bool:
    return _track_signature(a) == _track_signature(b)


def _read_subtitle_once(
    client: Client, bvid: str, cid: int, duration: float, raw_sink: dict | None
) -> tuple[SubtitleOutcome | None, bool]:
    """读取一次字幕。返回 (结果, 是否已登录)。

    已登录标志用于区分「没登录」与「该视频确实没字幕」—— 前者重试无意义。
    """
    player = client.player_v2(bvid, cid)
    logged_in = bool(player.get("login_mid"))

    if raw_sink is not None:
        raw_sink["player"] = player
        raw_sink["track_count"] = len((player.get("subtitle") or {}).get("subtitles") or [])
        raw_sink["need_login_subtitle"] = player.get("need_login_subtitle")

    tracks = (player.get("subtitle") or {}).get("subtitles") or []
    track, is_ai = _pick_track(tracks)
    if track is None:
        return None, logged_in

    raw = client.subtitle_json(track["subtitle_url"])
    cues = _parse_subtitle_body(raw)
    return (
        SubtitleOutcome(
            cues=cues,
            lan=str(track.get("lan") or ""),
            is_ai=is_ai,
            coverage=_coverage(cues, duration),
        ),
        logged_in,
    )


def fetch_subtitle(
    client: Client,
    bvid: str,
    cid: int,
    duration: float,
    *,
    attempts: int = SUBTITLE_ATTEMPTS,
    raw_sink: dict | None = None,
) -> SubtitleOutcome:
    """取字幕，带「假残缺」防护 + **双读一致性校验**。

    为什么要双读：实测发现上游 AI 字幕接口对同一 cid 可能**每次返回不同内容**
    （同一视频连拉三次分别得到 155 / 428 / 无字幕轨，内容互不相干，甚至像别的视频）。
    只靠时长覆盖率挡不住这种情况，因此每轮都独立读两次，只有两次一致才认为可信。

    每一轮都**重新请求 player/v2 换新的 subtitle_url** —— `auth_key` 有时效。
    """
    warnings: list[str] = []
    best: SubtitleOutcome | None = None

    def keep(candidate: SubtitleOutcome) -> None:
        """按覆盖率择优（覆盖率比条数更能反映"内容是否完整"）。"""
        nonlocal best
        if best is None or candidate.coverage > best.coverage:
            best = candidate

    for attempt in range(1, attempts + 1):
        first, logged_in = _read_subtitle_once(client, bvid, cid, duration, raw_sink)

        if first is None:
            if not logged_in:
                # 未登录时 subtitles 恒为空数组 —— 重试毫无意义，直接给准确原因。
                warnings.append(
                    "需要登录才能获取字幕：未登录时 subtitles 恒为空数组。"
                    "请先运行 `biliex auth set` 写入 SESSDATA。"
                )
                break
            if attempt == 1:
                warnings.append("player/v2 未返回字幕轨（已登录，该视频可能确实没有字幕）")
            else:
                warnings.append(f"第 {attempt} 轮未返回字幕轨")
            if attempt < attempts:
                time.sleep(3.0 * attempt)
            continue

        # 第二次独立读取，用于一致性校验
        second, _ = _read_subtitle_once(client, bvid, cid, duration, None)

        if second is None or not _same_content(first.cues, second.cues):
            second_count = len(second.cues) if second else 0
            warnings.append(
                f"第 {attempt} 轮两次读取不一致（{len(first.cues)} 条 vs {second_count} 条）"
                "—— 上游字幕接口对同一 cid 返回了不同内容，该轮结果不可信"
            )
            keep(first)
            if second is not None:
                keep(second)
            if attempt < attempts:
                time.sleep(6.0 + attempt * 3.0)
            continue

        # 两次一致，再看覆盖率（防"截断但两次都截断成一样"）
        if first.coverage >= COVERAGE_THRESHOLD:
            first.stable = True
            first.warnings = warnings
            if raw_sink is not None:
                raw_sink["subtitle_reads"] = 2
                raw_sink["subtitle_stable"] = True
            return first

        warnings.append(
            f"第 {attempt} 轮字幕疑似被截断：{len(first.cues)} 条，"
            f"时长覆盖率 {first.coverage:.1%}（阈值 {COVERAGE_THRESHOLD:.0%}）"
        )
        keep(first)
        if attempt < attempts:
            time.sleep(6.0 + attempt * 3.0)

    if best is None:
        return SubtitleOutcome(warnings=warnings)

    warnings.append(
        "未能获得可信字幕（两次读取不一致或覆盖率不达标），"
        "下面的内容**可能不完整或与视频不符**，向用户说明时务必标注"
    )
    best.stable = False
    best.warnings = warnings
    return best



# ---------------------------------------------------------------- L3（可选组件）


@dataclass
class AsrOutcome:
    """一次本地转写的结果。`cues` 为空即代表 L3 没有产出可用内容。"""

    cues: list[Cue] = field(default_factory=list)
    coverage: float = 0.0
    stable: bool = False
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    audio: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.cues) and self.stable


def _prune_empty_dir(path: Path) -> None:
    """音频删掉后顺手把空目录也去掉 —— 别在产物里留一个空壳目录。"""
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def run_local_asr(
    client: Client,
    bvid: str,
    cid: int,
    *,
    page: int,
    duration: float,
    dest_dir: Path,
    settings: "AsrSettings",
    progress: Callable[[str, float, float], None] | None = None,
    raw_sink: dict | None = None,
) -> AsrOutcome:
    """L3：下载音频 → 本地转写。

    这个函数**不允许抛异常给上层** —— 它是可选能力，任何失败都只应表现为"降级"，
    绝不能因为它把一次本来能成功的 L1/L2 抓取搞崩。

    `progress(stage, done, total)` 用于给命令行报进度（stage 为 `download` / `transcribe`）。
    """
    from . import asr as asr_mod
    from . import audio as audio_mod

    warnings: list[str] = []

    capability = asr_mod.probe()
    if not capability.available:
        return AsrOutcome(
            warnings=[
                f"本地 ASR 不可用：{capability.reason}。"
                f"装上之后 L3 会自动可用：{capability.hint}"
            ],
            info={"capability": capability.to_dict()},
        )

    def report_download(done: int, total: int) -> None:
        if progress is not None:
            progress("download", float(done), float(total or done))

    try:
        asset = audio_mod.fetch_audio(
            client,
            bvid,
            cid,
            dest_dir=dest_dir,
            prefix=f"p{page}",
            progress=report_download,
            raw_sink=raw_sink,
        )
    except BiliexError as exc:
        return AsrOutcome(warnings=[f"取音频失败（{exc.code}）：{exc.message}"])

    def report_transcribe(done: float, total: float) -> None:
        if progress is not None:
            progress("transcribe", done, total)

    def report_model(done: int, total: int) -> None:
        if progress is not None:
            progress("model", float(done), float(total or done))

    try:
        result = asr_mod.transcribe(
            asset.path,
            settings,
            capability=capability,
            progress=report_transcribe,
            model_progress=report_model,
        )
    except BiliexError as exc:
        return AsrOutcome(
            warnings=[f"本地转写失败（{exc.code}）：{exc.message}"],
            audio=asset.to_dict(),
        )
    except Exception as exc:  # noqa: BLE001 - 三方库的异常类型不稳定，兜住并如实上报
        return AsrOutcome(
            warnings=[f"本地转写失败（{type(exc).__name__}）：{str(exc)[:300]}"],
            audio=asset.to_dict(),
        )
    finally:
        if not settings.keep_audio:
            audio_mod.cleanup(asset.path)
            _prune_empty_dir(asset.path.parent)

    warnings.extend(result.warnings)

    # 用**视频时长**算覆盖率，才能和 L1/L2 的同一判据直接对比
    coverage = _coverage(result.cues, duration)
    if not result.cues:
        warnings.append("本地转写没有产出任何文本（音频可能全程无人声）")

    audio_seconds = float(result.info.get("audio_seconds") or result.audio_seconds or 0.0)
    if duration and audio_seconds and audio_seconds < duration * 0.95:
        warnings.append(
            f"音频时长（{audio_seconds:.0f}s）明显短于视频时长（{duration:.0f}s）"
            "—— 音频可能不完整，转录会缺内容"
        )

    stable = bool(result.cues) and coverage >= COVERAGE_THRESHOLD
    if result.cues and not stable:
        warnings.append(
            f"本地转写只覆盖了 {coverage:.1%} 的时长（阈值 {COVERAGE_THRESHOLD:.0%}），"
            "请在总结时说明内容可能不完整"
        )

    info = dict(result.info)
    # 只有真去 HuggingFace 取权重时才提这个端点，否则是噪音（默认走 ModelScope）
    if capability.mirror_applied and (info.get("model_ref") or {}).get("source") == "huggingface":
        info["hf_endpoint"] = capability.hf_endpoint
    return AsrOutcome(
        cues=result.cues,
        coverage=coverage,
        stable=stable,
        warnings=warnings,
        info=info,
        audio=asset.to_dict(),
    )


# ---------------------------------------------------------------- 编排


def _apply_subtitle(result: PageContent, outcome: SubtitleOutcome, title: str, part: str) -> None:
    """把 L2 的结果写进 `PageContent`（含相关性校验）。"""
    result.level = LEVEL_SUBTITLE
    result.cues = outcome.cues
    result.subtitle_lan = outcome.lan
    result.subtitle_is_ai = outcome.is_ai
    result.coverage = outcome.coverage
    result.unstable = not outcome.stable

    # 相关性校验：L2 的字幕可能**根本不是这个视频的**（实测有串号）。
    # 命中数 0 且存在关键词时，强烈提示内容错配。
    matched, total, missing = _relevance(title or part, outcome.cues)
    result.relevance_matched = matched
    result.relevance_total = total
    result.missing_keywords = missing
    if result.low_relevance:
        result.warnings.append(
            f"字幕内容与标题**没有任何关键词重合**（未命中：{'、'.join(missing[:6])}）"
            "—— 极可能不是本视频的内容（上游字幕接口存在串号现象）。"
            "不要据此生成总结。"
        )


def _apply_asr(result: PageContent, outcome: AsrOutcome, title: str, part: str) -> None:
    """把 L3 的结果写进 `PageContent`，并重新做一次相关性校验。

    L3 的素材是本机从播放流转写的，"串号"风险远低于平台字幕接口；
    但仍然算一次相关性 —— 万一播放地址指向了别的稿件，这个判据能兜住。
    """
    result.level = LEVEL_ASR
    result.cues = outcome.cues
    result.subtitle_lan = "local-asr"
    result.subtitle_is_ai = False
    result.coverage = outcome.coverage
    result.unstable = not outcome.stable
    result.asr = {"info": outcome.info, "audio": outcome.audio}

    matched, total, missing = _relevance(title or part, outcome.cues)
    result.relevance_matched = matched
    result.relevance_total = total
    result.missing_keywords = missing
    if result.low_relevance:
        result.warnings.append(
            f"本地转写内容与标题**没有任何关键词重合**（未命中：{'、'.join(missing[:6])}）"
            "—— 可能不是本视频的音频，请人工确认。"
        )


def fetch_page(
    client: Client,
    bvid: str,
    cid: int,
    *,
    page: int,
    part: str,
    duration: float,
    up_mid: int | None,
    title: str = "",
    prefer_official: bool = True,
    asr_settings: "AsrSettings | None" = None,
    asr_force: bool = False,
    asr_dest_dir: Path | None = None,
    asr_progress: Callable[[str, float, float], None] | None = None,
) -> PageContent:
    """对单个分 P 走完整降级链。

    `asr_settings` 非 None 即表示**启用 L3**（可选组件）。`asr_force=True` 时跳过 L1/L2，
    直接本地转写 —— 用于平台内容已知不可信、或想拿一份独立转录来交叉验证的场合。
    """
    result = PageContent(cid=cid, page=page, part=part, duration=duration)

    if asr_force:
        result.fallback_reasons.append(
            "按 --asr-force 跳过官方 AI 总结与平台字幕，直接使用本地 ASR 转写"
        )
    elif prefer_official:
        hit, data, reason = try_official(client, bvid, cid, up_mid)
        if hit and data is not None:
            summary, outline, cues = _parse_official(data)
            result.level = LEVEL_OFFICIAL
            result.summary = summary
            result.outline = outline
            result.cues = cues
            result.coverage = _coverage(cues, duration)
            result.subtitle_is_ai = True  # 官方 AI 总结里的字幕本身就是 AI 生成
            result.subtitle_lan = "ai-zh"
            result.raw["conclusion"] = data
            if not cues:
                result.warnings.append("官方 AI 总结命中，但未附带 AI 字幕")
            return result
        result.fallback_reasons.append(reason)

    if not asr_force:
        outcome = fetch_subtitle(client, bvid, cid, duration, raw_sink=result.raw)
        result.warnings.extend(outcome.warnings)
        if outcome.cues:
            _apply_subtitle(result, outcome, title, part)
            # 平台字幕不可信、且本轮启用了 L3 → 用本地转写替换它。
            # 这是 L3 相对 L2 的真正价值：不只是"多一条兜底路径"，而是能**纠正**上游错误。
            if asr_settings is not None and (not outcome.usable or result.low_relevance):
                result.fallback_reasons.append(
                    "平台字幕未通过三重校验（双读一致性 / 覆盖率 / 标题相关性），"
                    "已尝试改用本地 ASR 转写"
                )
            else:
                return result
        else:
            result.fallback_reasons.append("L2 未取到任何字幕")

    if asr_settings is None:
        return result

    outcome = run_local_asr(
        client,
        bvid,
        cid,
        page=page,
        duration=duration,
        dest_dir=asr_dest_dir or Path.cwd() / "out" / bvid / ".asr",
        settings=asr_settings,
        progress=asr_progress,
        raw_sink=result.raw,
    )
    result.warnings.extend(outcome.warnings)

    if not outcome.cues:
        result.fallback_reasons.append("L3 本地 ASR 未产出可用内容")
        if result.level == LEVEL_NONE:
            result.fallback_reasons.append("L2 未取到任何字幕")
        return result

    previous_level = result.level
    _apply_asr(result, outcome, title, part)
    result.asr_replaced_subtitle = previous_level == LEVEL_SUBTITLE
    result.fallback_reasons.append(
        f"内容由 L3 本地 ASR 转写生成（模型 {outcome.info.get('model', '?')}，"
        f"设备 {outcome.info.get('resolved_device', '?')}）"
    )
    if result.asr_replaced_subtitle:
        result.warnings.insert(
            0,
            "本次内容**不是**平台字幕，而是本机 ASR 转写（平台字幕未通过可信度校验，已被替换）",
        )
    return result
