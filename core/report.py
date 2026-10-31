"""日报排版。

日报格式与 dsh-bili-scroll 保持兼容：每条视频单独一段，第一行固定是
``### BV号`` 顶格 —— 这样以后想接"刷过的视频"列表、统计条数都方便。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .models import DeepResult, Material, RunResult
from .util import human_count, human_duration, truncate

VERDICT_ORDER = {"值得看": 0, "可以看": 1, "不值当": 2}


def build_report(
    day: str,
    results: Iterable[DeepResult],
    scanned: int,
    deep_count: int,
    detail: str = "full",
    closing: str = "",
    skipped_no_subtitle: int = 0,
) -> str:
    results = list(results)
    detail = (detail or "full").strip().lower()
    if detail == "none" or not results:
        return ""
    picked = len(results)
    lines = [
        f"# {day} 刷到的 B 站",
        "",
        f"> 今天刷了 {scanned} 条，深读 {deep_count} 条，挑中 {picked} 条。",
    ]
    if skipped_no_subtitle:
        lines.append(f"> 另有 {skipped_no_subtitle} 条因没有字幕/读不到内容跳过。")
    lines.append("")
    for item in results:
        result = item.normalized()
        video = result.video
        lines.append(f"### {video.bvid}")
        if detail == "thin":
            lines.append(f"- 档位：{result.verdict} ｜ [{video.title}]({video.link})")
            if result.why:
                lines.append(f"- 为什么：{result.why}")
        else:
            lines.extend(
                [
                    f"- 标题：{video.title}",
                    f"- UP：{video.author or '未知'}",
                    f"- 链接：{video.link}",
                    f"- 播放：{human_count(video.play)} · 时长：{human_duration(video.duration)}",
                    f"- 档位：{result.verdict}",
                    f"- 为什么值得：{result.why or '（没写出理由，按"可以看"处理）'}",
                    f"- 一句话：{result.one_liner or '——'}",
                ]
            )
            if result.evidence:
                lines.append(f"- 读到的：{'、'.join(result.evidence)}")
        lines.append("")
    if closing:
        lines.extend(["## 今天的一句", "", closing.strip(), ""])
    return "\n".join(lines).strip() + "\n"


def append_report(path: str | Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n---\n\n")
            fh.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def build_summary(result: RunResult, results: Iterable[DeepResult]) -> str:
    results = list(results)
    lines: list[str] = []
    if result.skip:
        lines.append("🐟 今天不想看 B 站，但日记还是写了。")
    else:
        lines.append(f"🐟 今天刷了 {result.scanned} 条，深读 {result.deep} 条，挑中 {result.picked} 条。")
    worth = [r for r in results if r.normalized().verdict == "值得看"]
    okay = [r for r in results if r.normalized().verdict == "可以看"]
    for title, group in (("值得看", worth), ("可以看", okay)):
        if not group:
            continue
        lines.append(f"{title}：")
        for item in group[:4]:
            video = item.video
            lines.append(f"· {truncate(video.title, 46)}（{video.bvid}）")
    if result.shared:
        lines.append(f"已在评论区 @ 你 {result.shared} 条。")
    if result.report_path:
        lines.append(f"完整日报：{result.report_path}")
    return "\n".join(lines)


def build_dm_text(result: RunResult, max_chars: int = 300) -> str:
    """B 站私信推送的短文本。

    私信风控比评论还敏感，控制在一屏内：只发摘要/标题，不带本机路径、
    不附完整日报；完整内容留在 AstrBot 会话和日报文件里。
    """
    lines = []
    for line in (result.summary or "").splitlines():
        if line.startswith("完整日报：") or line.startswith("（评论分享") or line.startswith("（分享模式"):
            continue
        if line.startswith("另有 "):
            continue
        lines.append(line)
    if not lines:
        lines.append(f"🐟 今天刷了 {result.scanned} 条，挑中 {result.picked} 条。")
    lines.append("（完整日报在 AstrBot 里）")
    return truncate("\n".join(lines), max(80, int(max_chars)))


def build_skip_note(day: str, reason: str) -> str:
    return f"# {day} 今天不想看\n\n{reason or '就是不想看，没有理由。'}\n"


def build_digest_text(material: Material, result: DeepResult, limit: int = 1600) -> str:
    video = material.video
    result = result.normalized()
    parts = [
        f"【{video.title}】",
        f"{video.author or '未知 UP'} · {human_count(video.play)} 播放 · {human_duration(video.duration)}",
        f"{video.link}",
        "",
        f"评分 {result.score}/20 · {result.verdict}",
        f"为什么：{result.why or '——'}",
        f"一句话：{result.one_liner or '——'}",
    ]
    if material.has_subtitle:
        excerpt = " / ".join(material.subtitle.lines[:3])
        parts.extend(["", "字幕开头：" + truncate(excerpt, 220)])
    if material.comments:
        parts.extend(["", "热评：", material.comments_text(limit=3, per_comment=90)])
    return truncate("\n".join(parts), limit)


def list_reports(report_dir: str | Path, limit: int = 10) -> list[str]:
    directory = Path(report_dir)
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files[:limit]]
