"""解析用户输入的视频标识。

支持：完整 URL、b23.tv 短链、裸 BV 号、av 号、带分 P 参数的 URL。
短链的展开需要网络，因此这里只负责**识别**，展开交给调用方。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_B23_RE = re.compile(r"https?://b23\.tv/([0-9A-Za-z]+)")
_PAGE_RE = re.compile(r"[?&]p=(\d+)")


@dataclass
class Target:
    bvid: str | None = None
    aid: int | None = None
    page: int = 1
    short_code: str | None = None
    raw: str = ""

    @property
    def is_short_link(self) -> bool:
        return self.short_code is not None

    def describe(self) -> str:
        if self.bvid:
            return self.bvid
        if self.aid:
            return f"av{self.aid}"
        return self.short_code or self.raw


def parse(text: str) -> Target:
    """从任意输入中解析出视频标识。解析不出任何标识时抛 ValueError。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("输入为空")

    page_match = _PAGE_RE.search(raw)
    page = int(page_match.group(1)) if page_match else 1

    if match := _BV_RE.search(raw):
        return Target(bvid=match.group(1), page=page, raw=raw)

    if match := _AV_RE.search(raw):
        return Target(aid=int(match.group(1)), page=page, raw=raw)

    if match := _B23_RE.search(raw):
        return Target(short_code=match.group(1), page=page, raw=raw)

    raise ValueError(
        f"无法从输入中识别视频标识：{raw!r}。"
        "请提供 BV 号、av 号或 bilibili.com / b23.tv 链接。"
    )
