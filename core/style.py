"""评论语感闸门：在发出去之前拦「八股 / 重复 / 撞车」。

借鉴 dsh-bili-scroll 2.2.0 的做法：
- 开头套话黑名单；
- 和最近自己发过的评论做 bigram 相似度；
- 和这条视频现有热评做相似度。

纯本地、零依赖；读不到热评时跳过第二道检查，不因读不到就不让发。
"""

from __future__ import annotations

import re
from typing import Iterable

# 开头八股。宁可稍微严一点：撞了就重写一次，仍撞就不发。
LEAD_RE = re.compile(
    r"^\s*(?:"
    r"这条(?:我认|我记下了|值得)|我记下了|值得一看|确实值得|不得不说|"
    r"好家伙|这个(?:必须|真的要)|强烈推荐|感谢分享|学到了|受教了|"
    r"作为一个|总之|综上|首先|其次"
    r")"
)

_MENTION_RE = re.compile(r"^@[^\s]+\s*")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def normalize_mention(text: str) -> str:
    """B 站只认紧贴的 @昵称；把模型常写的「@ 昵称」压成「@昵称」。"""
    return re.sub(r"@\s+", "@", str(text or ""))


def _clean(text: str) -> str:
    body = " ".join(str(text or "").split())
    body = _MENTION_RE.sub("", body)
    return body.strip()


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", "".join(ch for ch in str(text or "") if not ch.isspace()))
    if len(t) < 2:
        return set(t)
    return {t[i : i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    """bigram Jaccard；够用、够快、不引依赖。"""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / max(1, len(x | y))


def style_reason(
    text: str,
    history: Iterable[str] = (),
    hot_comments: Iterable[str] = (),
    *,
    history_threshold: float = 0.55,
    hot_threshold: float = 0.50,
) -> str:
    """返回不能发的原因；空串表示通过。"""
    body = _clean(text)
    if not body:
        return "评论是空的。"
    match = LEAD_RE.match(body)
    if match:
        return (
            f"开头「{match.group(0).strip()}」是套话。直接说这条视频里的具体东西："
            "「讲了什么 + 一个只有看了才说得出的细节」，开头别用套话。"
        )
    if has_emoji_or_tag(body):
        return "评论里不要放 emoji 或话题标签；像人随口说一句。"
    for old in history:
        old_body = _clean(old)
        if len(old_body) < 4:
            continue
        score = similarity(body, old_body)
        if score >= history_threshold:
            return (
                f"跟你以前发过的「{old_body[:18]}…」太像了（相似度 {score:.2f}）。"
                "换开头、换关注点，别套同一个句式。"
            )
    for other in hot_comments:
        other_body = _clean(other)
        if len(other_body) < 4:
            continue
        score = similarity(body, other_body)
        if score >= hot_threshold:
            return (
                f"这条视频底下已经有一句很像的（相似度 {score:.2f}）：{other_body[:24]}… "
                "撞车了 —— 换个角度写，或者这一轮不发。"
            )
    return ""


def has_emoji_or_tag(text: str) -> bool:
    return bool(_EMOJI_RE.search(text or "") or re.search(r"#\S+", text or ""))
