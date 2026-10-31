"""选择性地记住：不是每条视频都进长期记忆。

分三档，按配置决定：
1. 分数 >= strong_score：直接记（高光）；
2. 分数 >= min_score：按 chance 随机记（记不记得住看运气）；
3. 其余不记。

另外模型给的 interesting（0-1）超过阈值也算高光；
每天的条数上限由调用方配合台账控制。

纯函数 + 可注入的随机源，方便单测。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .models import DeepResult, RunResult
from .util import as_bool, as_float, as_int, cfg_get, clean_text, truncate

MEMORY_REASONS = {
    "strong": "高光",
    "interesting": "很有意思",
    "chance": "值得记住",
}


@dataclass(frozen=True)
class MemoryPolicy:
    enabled: bool = True
    auto_write: bool = True
    min_score: int = 13
    strong_score: int = 16
    chance: float = 0.35
    interesting_threshold: float = 0.8
    max_per_day: int = 3
    importance: float = 0.65
    max_chars: int = 500

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | Any) -> "MemoryPolicy":
        min_score = as_int(cfg_get(cfg, "memory.min_score", 13), 13, 0, 20)
        strong_score = as_int(cfg_get(cfg, "memory.strong_score", 16), 16, 0, 20)
        if strong_score < min_score:
            strong_score = min_score
        return cls(
            enabled=as_bool(cfg_get(cfg, "memory.enabled", True), True),
            auto_write=as_bool(cfg_get(cfg, "memory.auto_write", True), True),
            min_score=min_score,
            strong_score=strong_score,
            chance=as_float(cfg_get(cfg, "memory.chance", 0.35), 0.35, 0.0, 1.0),
            interesting_threshold=as_float(
                cfg_get(cfg, "memory.interesting_threshold", 0.8), 0.8, 0.0, 1.0
            ),
            max_per_day=as_int(cfg_get(cfg, "memory.max_per_day", 3), 3, 0, 50),
            importance=as_float(cfg_get(cfg, "memory.importance", 0.65), 0.65, 0.0, 1.0),
            max_chars=as_int(cfg_get(cfg, "memory.max_chars", 500), 500, 100, 5000),
        )


def decide(
    result: DeepResult,
    policy: MemoryPolicy,
    rng: Callable[[], float] | None = None,
) -> str:
    """返回写入理由（strong/interesting/chance），不该记就返回空串。"""
    rng = rng or random.random
    result = result.normalized()
    if result.verdict == "不值当":
        return ""
    if result.score >= policy.strong_score:
        return "strong"
    if result.interesting >= policy.interesting_threshold and result.interesting > 0:
        return "interesting"
    if result.score < policy.min_score:
        return ""
    if policy.chance <= 0:
        return ""
    if policy.chance >= 1:
        return "chance"
    return "chance" if rng() < policy.chance else ""


def pick(
    results: Iterable[DeepResult],
    policy: MemoryPolicy,
    rng: Callable[[], float] | None = None,
) -> tuple[DeepResult | None, str]:
    """挑出本轮最值得记的一条；没有就返回 (None, "")。"""
    if not policy.enabled or not policy.auto_write:
        return None, ""
    best: DeepResult | None = None
    best_reason = ""
    best_key = -1.0
    for item in results:
        result = item.normalized()
        reason = decide(result, policy, rng)
        if not reason:
            continue
        # 高光优先，其次按分数、有趣程度排序
        key = (
            (2.0 if reason == "strong" else 1.5 if reason == "interesting" else 1.0)
            + result.score / 100.0
            + result.interesting / 10.0
        )
        if key > best_key:
            best, best_reason, best_key = result, reason, key
    return best, best_reason


def build_note(result: DeepResult, reason: str, max_chars: int = 500) -> str:
    """拼成一条适合写进长期记忆的第一人称短句。"""
    video = result.video
    bits = [f"今天在 B 站看了《{video.title}》（{video.bvid}）"]
    if video.author:
        bits.append(f"UP 是 {video.author}")
    line = "，".join(bits)
    if result.why:
        line += f"。{result.why}"
    if result.one_liner:
        line += f" 一句话：{result.one_liner}"
    if result.memory_hint:
        line += f" 记住它的理由：{result.memory_hint}"
    label = MEMORY_REASONS.get(reason, "")
    if label:
        line += f"（{label}）"
    return truncate(clean_text(line, keep_newlines=False), max_chars)


def attach(result: RunResult, results: Iterable[DeepResult], policy: MemoryPolicy, rng=None) -> str:
    """给 RunResult 填上本轮要不要记；返回写入理由（空串表示不记）。"""
    best, reason = pick(results, policy, rng)
    if best is None:
        return ""
    result.memory_bvid = best.video.bvid
    result.memory_note = build_note(best, reason, policy.max_chars)
    return reason


def today_count(log: Mapping[str, Any], day: str) -> int:
    rows = log.get(day) if isinstance(log, Mapping) else None
    return len(rows) if isinstance(rows, list) else 0


def already_logged(log: Mapping[str, Any], day: str, bvid: str) -> bool:
    rows = log.get(day) if isinstance(log, Mapping) else None
    return isinstance(rows, list) and bvid in rows


def record(log: dict[str, Any], day: str, bvid: str, keep_days: int = 30) -> None:
    rows = log.get(day)
    if not isinstance(rows, list):
        rows = []
        log[day] = rows
    if bvid and bvid not in rows:
        rows.append(bvid)
    for key in sorted(log)[:-keep_days]:
        log.pop(key, None)
