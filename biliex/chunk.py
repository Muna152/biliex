"""转录分块：把长字幕切成带时间戳、块间有重叠的片段。

为什么要重叠：*Lost in the Middle*（TACL 2024）证明长上下文模型对中段信息
利用显著衰减，硬切会让边界处的内容丢失。
"""

from __future__ import annotations

from dataclasses import dataclass

from .content import Cue

DEFAULT_MAX_CHARS = 6000
DEFAULT_OVERLAP_CHARS = 400


def fmt_ts(seconds: float) -> str:
    """把秒格式化成 `MM:SS` 或 `H:MM:SS`。"""
    total = int(max(seconds, 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@dataclass
class Chunk:
    index: int
    start: float
    end: float
    text: str
    cue_count: int

    @property
    def time_range(self) -> str:
        return f"{fmt_ts(self.start)} - {fmt_ts(self.end)}"


def format_transcript(cues: list[Cue], *, with_timestamp: bool = True) -> str:
    """完整转录文本，每行带 `[MM:SS]` 前缀。"""
    lines = []
    for cue in cues:
        if with_timestamp:
            lines.append(f"[{fmt_ts(cue.start)}] {cue.text}")
        else:
            lines.append(cue.text)
    return "\n".join(lines)


def chunk_cues(
    cues: list[Cue],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """按字符预算切块，块间保留约 `overlap_chars` 的重叠。"""
    if not cues:
        return []

    chunks: list[Chunk] = []
    index = 0
    position = 0
    total = len(cues)

    while position < total:
        start_position = position
        char_count = 0
        lines: list[str] = []

        while position < total:
            line = f"[{fmt_ts(cues[position].start)}] {cues[position].text}"
            # 至少要有一条，避免单条超长时死循环
            if lines and char_count + len(line) > max_chars:
                break
            lines.append(line)
            char_count += len(line) + 1
            position += 1

        index += 1
        chunks.append(
            Chunk(
                index=index,
                start=cues[start_position].start,
                end=cues[position - 1].end,
                text="\n".join(lines),
                cue_count=position - start_position,
            )
        )

        if position >= total:
            break

        # 回退若干条作为下一块的重叠上下文
        overlap_count = 0
        overlap_total = 0
        back = position - 1
        while back > start_position and overlap_total < overlap_chars:
            overlap_total += len(cues[back].text) + 12
            overlap_count += 1
            back -= 1
        position = max(start_position + 1, position - overlap_count)

    return chunks
