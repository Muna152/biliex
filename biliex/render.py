"""产出层：把内容包写成 agent 可直接消费的文件。

产出结构（每个视频一个目录）：

    out/<bvid>/
      index.md             入口：元数据 + 命中级别 + 官方摘要 + 分块清单 + 给 agent 的总结要求
      meta.json            结构化元数据与命中情况
      raw/                 上游原始 JSON（证据留档，便于事后核对）
      p<N>-transcript.md   分 P 的完整转录（带时间戳）
      p<N>-chunks/C##.md   分块文件
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .chunk import Chunk, format_transcript, fmt_ts
from .content import (
    LEVEL_ASR,
    LEVEL_NONE,
    LEVEL_OFFICIAL,
    LEVEL_SUBTITLE,
    PageContent,
)

LEVEL_LABEL = {
    LEVEL_OFFICIAL: "L1 · B 站官方 AI 总结（摘要 + 提纲 + AI 字幕均由 B 站生成）",
    LEVEL_SUBTITLE: "L2 · 平台字幕（人工 CC 或 AI 字幕，由本工具下载）",
    LEVEL_ASR: "L3 · 本地 ASR 转写（faster-whisper 在本机推理，内容由本工具生成）",
    LEVEL_NONE: "L4 · 仅元数据（该视频没有可用的总结或字幕）",
}

# 嵌进 index.md 的总结要求：参考 youtube-summary 的结构化输出，
# 并强制每段携带时间戳，避免归并时把时间信息洗掉。
SUMMARY_BRIEF = """\
## 给 agent 的总结要求

请基于上面提供的内容（优先使用 B 站官方摘要与提纲；若只有转录，则基于转录），产出结构化总结：

**硬性约束**
1. **只依据给定文本**，不得引入外部知识。不确定的内容明确写「原文未说明」，不要补全。
2. **每条要点必须携带时间戳**（用 `[MM:SS]` 格式），便于回看原视频。
3. **不要写「视频中提到」「作者认为」这类归因前缀**，直接陈述内容。
4. 每条要点至少 2-3 句，避免只有短语。
5. 某一节在原文中确实没有内容时**直接省略该节**，不要为了凑结构而编造。

**输出结构**
- **一句话概括**：整段视频的核心结论
- **总体摘要**：3-5 句
- **话题章节**：按 `[时间戳] 小节标题` + 要点列表组织
- **关键引用**：原文中值得留存的原话（带时间戳）
- **新颖观点 / 反直觉观点**：与常识不同之处（带时间戳）
- **方法论**：视频给出的可操作步骤（带时间戳）
- **关键数据**：出现的数字、结论、指标（带时间戳）

**长文处理**：若转录很长，请对每个分块先做块级摘要，再归并；归并时只做合并与排序，
不要重新自由发挥，以免丢失章节结构与时间戳。
"""


@dataclass
class Bundle:
    """一次 fetch 的全部产物。"""

    bvid: str
    root: Path
    meta: dict
    pages: list[PageContent]


def _frontmatter(bundle: Bundle) -> str:
    meta = bundle.meta
    lines = [
        "---",
        f"bvid: {bundle.bvid}",
        f"title: {_yaml_scalar(meta.get('title', ''))}",
        f"up: {_yaml_scalar(meta.get('owner_name', ''))}",
        f"duration_seconds: {meta.get('duration', 0)}",
        f"source_level: {meta.get('level', LEVEL_NONE)}",
    ]
    models = sorted(
        {
            page.asr.get("info", {}).get("model")
            for page in bundle.pages
            if page.asr.get("info", {}).get("model")
        }
    )
    if models:
        lines.append(f"asr_model: {_yaml_scalar(', '.join(models))}")
    lines += [
        f"subtitle_is_ai: {str(meta.get('subtitle_is_ai', False)).lower()}",
        f"generated_at: {meta.get('generated_at', '')}",
        f"source_url: https://www.bilibili.com/video/{bundle.bvid}",
        "---",
    ]
    return "\n".join(lines)


def source_note(levels: set[str]) -> str:
    """数据来源说明。**纯函数**，因为有单元测试守着它。

    这句话直接决定 agent 会不会把"本机转写的内容"说成"B 站官方总结"，
    所以它必须随命中级别精确变化，不能笼统。
    """
    if levels == {LEVEL_OFFICIAL}:
        return (
            "本视频命中了 B 站官方 AI 总结接口，下面的摘要与提纲**由 B 站生成**，"
            "时间戳可直接用于回看。"
        )
    if LEVEL_OFFICIAL in levels:
        return (
            "部分分 P 命中 B 站官方 AI 总结（内容由 B 站生成）；"
            "其余分 P 的素材来自平台字幕或本机 ASR 转写，见各分 P 说明。"
        )
    if levels == {LEVEL_ASR}:
        return (
            "未命中官方 AI 总结，平台也没有可用字幕 —— 内容由**本机 faster-whisper 转写**，"
            "是本工具生成的内容，**不是 B 站提供的字幕**。"
        )
    if LEVEL_ASR in levels and LEVEL_SUBTITLE in levels:
        return (
            "未命中官方 AI 总结；部分分 P 用平台字幕，部分分 P 因平台字幕未通过可信度校验"
            "改用了**本机 ASR 转写**（见各分 P 说明）。"
        )
    if LEVEL_SUBTITLE in levels and LEVEL_NONE in levels:
        return "未命中官方 AI 总结；部分分 P 只有平台字幕，另有分 P 连字幕都没有（只有元数据）。"
    if LEVEL_SUBTITLE in levels:
        return "未命中官方 AI 总结，内容来自平台字幕转录。请基于转录自行总结。"
    return "该视频没有可用的 AI 总结或字幕，只有元数据。"


def _asr_lines(asr: dict) -> list[str]:
    """`index.md` 里的 ASR 元数据行 —— 让"这份转写是怎么来的"可核查。"""
    info = asr.get("info") or {}
    audio = asr.get("audio") or {}
    lines: list[str] = []
    if info.get("model"):
        ref = info.get("model_ref") or {}
        source = ref.get("source")
        source_note = {
            "modelscope": "ModelScope 镜像",
            "huggingface": "HuggingFace",
            "local": "本地目录",
        }.get(source, "")
        if source_note:
            source_note = f"　权重来源 `{source_note}`"
            if ref.get("cached"):
                source_note += "（缓存）"
        lines.append(
            f"- ASR：模型 `{info['model']}`　设备 `{info.get('resolved_device', '?')}`　"
            f"精度 `{info.get('resolved_compute_type', '?')}`{source_note}"
        )
    if info.get("elapsed_seconds") is not None:
        speed = info.get("speed_x")
        speed_note = f"，约 {speed}× 实时速度" if speed else ""
        lines.append(
            f"- ASR 耗时：{float(info['elapsed_seconds']):.0f} 秒"
            f"（音频 {float(info.get('audio_seconds') or 0):.0f} 秒{speed_note}）"
        )
    if audio.get("size_mb"):
        stream = audio.get("stream") or {}
        lines.append(
            f"- 音频：{audio['size_mb']} MB（音轨 {stream.get('stream_id') or '?'}，"
            f"{stream.get('source', 'dash')}）"
        )
    return lines


def _yaml_scalar(value: str) -> str:
    text = str(value).replace('"', "'").replace("\n", " ")
    return f'"{text}"'


def _official_section(page: PageContent) -> str:
    if page.level != LEVEL_OFFICIAL:
        return ""
    parts = ["## B 站官方 AI 总结", ""]
    if page.summary:
        parts += [f"> {page.summary}", ""]
    if page.outline:
        parts.append("### 官方分段提纲")
        parts.append("")
        for section in page.outline:
            parts.append(f"**[{fmt_ts(section.timestamp)}] {section.title}**")
            parts.append("")
            for point in section.points:
                parts.append(f"- `[{fmt_ts(point.timestamp)}]` {point.content}")
            parts.append("")
    return "\n".join(parts)


def _page_section(page: PageContent, chunks: list[Chunk], prefix: str) -> str:
    parts = [
        f"### 分 P{page.page}：{page.part or '(未命名)'}",
        "",
        f"- cid: `{page.cid}`",
        f"- 时长：{fmt_ts(page.duration)}",
        f"- 命中级别：{LEVEL_LABEL.get(page.level, page.level)}",
        f"- 字幕条数：{len(page.cues)}",
    ]
    if page.subtitle_lan:
        if page.level == LEVEL_ASR:
            parts.append("- 来源：**本机 ASR 转写**（不是平台字幕，也不是 B 站生成的摘要）")
        else:
            ai_note = "（AI 生成）" if page.subtitle_is_ai else "（人工上传）"
            parts.append(f"- 字幕语言：`{page.subtitle_lan}` {ai_note}")
    if page.asr:
        parts.extend(_asr_lines(page.asr))
    if page.level != LEVEL_OFFICIAL and page.coverage:
        parts.append(f"- 时长覆盖率：{page.coverage:.1%}")
    parts.append("")

    if page.unstable:
        parts += [
            "> ⛔ **该分 P 的字幕不可信**：两次独立读取结果不一致，或时长覆盖率远低于阈值。",
            "> 上游对同一 cid 可能返回互不相同的内容（已实测）。**不要把它当作完整内容来总结**；",
            "> 若仍要使用，请在结论中明确标注「内容可能不完整或与视频不符」。",
            "",
        ]

    if page.fallback_reasons:
        parts.append("**降级原因**")
        parts.append("")
        for reason in page.fallback_reasons:
            parts.append(f"- {reason}")
        parts.append("")

    if page.warnings:
        parts.append("**告警**")
        parts.append("")
        for warning in page.warnings:
            parts.append(f"- ⚠️ {warning}")
        parts.append("")

    if chunks:
        parts.append("**分块清单**")
        parts.append("")
        for chunk in chunks:
            parts.append(
                f"- `{prefix}-chunks/C{chunk.index:02d}.md` "
                f"（{chunk.time_range}，{chunk.cue_count} 条）"
            )
        parts.append("")
    return "\n".join(parts)


def write_bundle(
    bundle: Bundle,
    *,
    chunks_by_cid: dict[int, list[Chunk]],
) -> Path:
    """把内容包落盘，返回目录路径。"""
    root = bundle.root
    root.mkdir(parents=True, exist_ok=True)
    prefix_map = {page.cid: f"p{page.page}" for page in bundle.pages}

    sections = [_frontmatter(bundle), "", f"# {bundle.meta.get('title', '')}", ""]
    if bundle.meta.get("owner_name"):
        sections.append(
            f"UP 主：{bundle.meta['owner_name']}　|　"
            f"总时长：{fmt_ts(bundle.meta.get('duration', 0))}　|　"
            f"分 P 数：{len(bundle.pages)}"
        )
        sections.append("")

    # 明确告知数据来自哪一级 —— 用户必须能分辨"B 站自己总结的"和"本机转写的"
    levels = {page.level for page in bundle.pages}
    sections += [f"> **数据来源**：{source_note(levels)}", ""]

    replaced = [p for p in bundle.pages if p.asr_replaced_subtitle]
    if replaced:
        pages_text = "、".join(f"分 P{p.page}" for p in replaced)
        sections += [
            f"> ⚠️ **{pages_text} 的平台字幕未通过可信度校验**（覆盖率不足、两次读取不一致、"
            "或与标题零关键词重合），已改用**本机 ASR 转写**作为素材。",
            "> 两者的来源不同：转写内容由本工具在本机生成，与平台字幕可能存在用词差异。",
            "",
        ]

    asr_pages = [p for p in bundle.pages if p.level == LEVEL_ASR]
    if asr_pages:
        pages_text = "、".join(f"分 P{p.page}" for p in asr_pages)
        sections += [
            f"> 🎙️ **{pages_text} 的内容是本机 ASR 转写**（faster-whisper 本地推理）。"
            "转写可能有同音字与断句误差；引用原文时请注明这是转写文本。",
            "",
        ]

    mismatched = [p for p in bundle.pages if p.low_relevance]
    if mismatched:
        pages_text = "、".join(f"分 P{p.page}" for p in mismatched)
        sections += [
            f"> ⛔⛔ **{pages_text} 的字幕与标题没有任何关键词重合，极可能不是本视频的内容**"
            "（实测：上游 `player/v2` 的 AI 字幕接口会返回别的视频的字幕）。",
            "> **不要用这些内容生成总结。** 请改用同分 P 的官方 AI 总结（若有），"
            "或如实告知用户「无法获取该视频的可靠内容」。",
            "",
        ]

    unstable_pages = [p for p in bundle.pages if p.unstable]
    if unstable_pages:
        pages_text = "、".join(f"分 P{p.page}" for p in unstable_pages)
        sections += [
            f"> ⛔ **注意：{pages_text} 的字幕不可信**（两次独立读取不一致或严重不完整）。",
            "> 生成总结时不要把这些内容当作完整素材，必须向用户说明这一限制。",
            "",
        ]

    for page in bundle.pages:
        official = _official_section(page)
        if official:
            sections.append(official)
        sections.append(
            _page_section(page, chunks_by_cid.get(page.cid, []), prefix_map[page.cid])
        )

    if any(page.cues for page in bundle.pages):
        sections += [SUMMARY_BRIEF, ""]

    (root / "index.md").write_text("\n".join(sections), encoding="utf-8")
    (root / "meta.json").write_text(
        json.dumps(bundle.meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for page in bundle.pages:
        prefix = prefix_map[page.cid]
        if page.cues:
            (root / f"{prefix}-transcript.md").write_text(
                format_transcript(page.cues), encoding="utf-8"
            )
        chunks = chunks_by_cid.get(page.cid, [])
        if chunks:
            chunk_dir = root / f"{prefix}-chunks"
            chunk_dir.mkdir(exist_ok=True)
            for chunk in chunks:
                header = (
                    f"<!-- 分 P{page.page} 第 {chunk.index}/{len(chunks)} 块　"
                    f"{chunk.time_range} -->\n\n"
                )
                (chunk_dir / f"C{chunk.index:02d}.md").write_text(
                    header + chunk.text + "\n", encoding="utf-8"
                )

    return root


def save_raw(bundle: Bundle, name: str, payload: object) -> None:
    """留档上游原始 JSON —— 出现争议时这是唯一可信证据。"""
    raw_dir = bundle.root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
