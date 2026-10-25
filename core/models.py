"""领域模型：视频、字幕、评论、弹幕、深读结果。

字段尽量与 B 站接口原名对应，方便排查；展示层再转成中文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .util import human_count, human_duration, parse_duration


@dataclass
class Video:
    bvid: str
    title: str = ""
    author: str = ""
    mid: int = 0
    aid: int = 0
    cid: int = 0
    play: int = 0
    pubdate: int = 0
    duration: int = 0
    desc: str = ""
    pic: str = ""
    tname: str = ""

    @property
    def link(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}/"

    @property
    def short(self) -> str:
        bits = [self.title or self.bvid]
        if self.author:
            bits.append(f"UP {self.author}")
        if self.duration:
            bits.append(human_duration(self.duration))
        if self.play:
            bits.append(f"{human_count(self.play)} 播放")
        return " · ".join(bits)

    @classmethod
    def from_search(cls, row: dict[str, Any]) -> "Video":
        return cls(
            bvid=str(row.get("bvid") or ""),
            title=str(row.get("title") or ""),
            author=str(row.get("author") or ""),
            mid=int(row.get("mid") or 0),
            aid=int(row.get("aid") or 0),
            play=int(row.get("play") or 0),
            pubdate=int(row.get("pubdate") or 0),
            duration=parse_duration(row.get("duration")),
            desc=str(row.get("description") or ""),
            pic=str(row.get("pic") or ""),
            tname=str(row.get("typename") or ""),
        )

    @classmethod
    def from_view(cls, data: dict[str, Any]) -> "Video":
        pages = data.get("pages") or []
        cid = int(data.get("cid") or (pages[0].get("cid") if pages else 0) or 0)
        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        return cls(
            bvid=str(data.get("bvid") or ""),
            title=str(data.get("title") or ""),
            author=str(owner.get("name") or ""),
            mid=int(owner.get("mid") or 0),
            aid=int(data.get("aid") or 0),
            cid=cid,
            play=int(stat.get("view") or 0),
            pubdate=int(data.get("pubdate") or 0),
            duration=int(data.get("duration") or 0),
            desc=str(data.get("desc") or ""),
            pic=str(data.get("pic") or ""),
            tname=str(data.get("tname") or ""),
        )


@dataclass
class Subtitle:
    lang: str
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def char_count(self) -> int:
        return sum(len(line) for line in self.lines)

    @property
    def meaningful(self) -> bool:
        """只有"字幕 by XX"这类署名、或干脆空白的，按无字幕处理。"""
        body = "\n".join(re.sub(r"^\[[\d:]+\]\s*", "", line) for line in self.lines)
        body = re.sub(r"(本?字幕\s*by\s*\S+|本字幕由.*?提供)", "", body, flags=re.IGNORECASE)
        letters = [ch for ch in body if ch.isalnum()]
        # 既要够长，又要有足够多不同的字，避免"字幕byXX"复制十遍冒充正文。
        return len(letters) >= 20 and len(set(letters)) >= 8


@dataclass
class Comment:
    user: str
    text: str
    like: int = 0
    rpid: str = ""
    is_top: bool = False

    @property
    def line(self) -> str:
        prefix = "置顶 · " if self.is_top else ""
        return f"- {prefix}{self.user}（{human_count(self.like)} 赞）：{self.text}"


@dataclass
class DanmakuHotspot:
    minute: int
    count: int
    samples: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.minute // 60:02d}:{self.minute % 60:02d}"


@dataclass
class Material:
    """一条视频"能读到的所有文字"。"""

    video: Video
    subtitle: Subtitle | None = None
    comments: list[Comment] = field(default_factory=list)
    danmaku: list[DanmakuHotspot] = field(default_factory=list)
    subtitle_error: str = ""

    @property
    def has_subtitle(self) -> bool:
        return bool(self.subtitle and self.subtitle.meaningful)

    @property
    def evidence(self) -> list[str]:
        out = []
        if self.has_subtitle:
            out.append(f"官方字幕 {self.subtitle.char_count} 字")
        if self.danmaku:
            out.append(f"弹幕热区 {len(self.danmaku)} 段")
        if self.comments:
            out.append(f"热评 {len(self.comments)} 条")
        return out

    def danmaku_text(self, limit: int = 1200) -> str:
        rows = []
        for spot in self.danmaku:
            samples = " / ".join(spot.samples[:3])
            rows.append(f"[{spot.label}]（{spot.count} 条）{samples}")
        return "\n".join(rows)[:limit]

    def comments_text(self, limit: int = 10, per_comment: int = 120) -> str:
        rows = [c.line for c in self.comments[:limit]]
        return "\n".join(rows)[: per_comment * max(1, limit)] if rows else ""


@dataclass
class DeepResult:
    video: Video
    score: int = 0
    verdict: str = "可以看"
    why: str = ""
    one_liner: str = ""
    evidence: list[str] = field(default_factory=list)

    VERDICTS = ("值得看", "可以看", "不值当")

    def normalized(self) -> "DeepResult":
        score = max(0, min(20, int(self.score or 0)))
        verdict = self.verdict if self.verdict in self.VERDICTS else _verdict_of(score)
        return DeepResult(
            video=self.video,
            score=score,
            verdict=verdict,
            why=self.why.strip(),
            one_liner=self.one_liner.strip(),
            evidence=list(self.evidence),
        )


def _verdict_of(score: int) -> str:
    if score >= 13:
        return "值得看"
    if score >= 8:
        return "可以看"
    return "不值当"


@dataclass
class RunResult:
    scanned: int = 0
    deep: int = 0
    picked: int = 0
    shared: int = 0
    report_path: str = ""
    summary: str = ""
    error: str = ""
    skip: bool = False
