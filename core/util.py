"""小工具：时间、配置读取、文本清洗。

这些函数尽量保持"纯"：不读文件、不发网络，方便在测试里直接调用。
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

# 控制字符（保留换行/制表）—— 评论区、字幕里偶尔会混进终端控制序列，
# 拼进提示词或消息前统一清掉。
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# 提示注入的常见开头。不是"过滤器"，只是给日志和兜底摘要一个标记；
# 真正的防线在系统提示词：素材不是指令。
_INJECTION_HINTS = (
    "忽略之前",
    "忽略以上",
    "ignore previous",
    "ignore above",
    "system prompt",
    "你现在是",
    "请扮演",
)


def cfg_get(cfg: Mapping[str, Any] | Any, path: str, default: Any = None) -> Any:
    """从嵌套配置里安全取值。

    支持 ``cfg_get(cfg, "account.sessdata", "")``。
    任何一层不是 dict、或值为 None，都返回 default。
    """
    cur: Any = cfg
    for key in path.split("."):
        if not isinstance(cur, Mapping):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
    return default


def as_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def as_float(value: Any, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = default
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def as_str_list(value: Any, default: Iterable[str] = ()) -> list[str]:
    """把 list / 逗号分隔字符串 / 单个字符串统一成去空的字符串列表。"""
    if value is None:
        return list(default)
    if isinstance(value, str):
        parts = re.split(r"[,，\n]+", value)
    elif isinstance(value, Iterable):
        parts = [str(v) for v in value]
    else:
        parts = [str(value)]
    out: list[str] = []
    for part in parts:
        item = part.strip()
        if item and item not in out:
            out.append(item)
    return out or list(default)


def now_local() -> datetime:
    return datetime.now()


def today_str(now: datetime | None = None) -> str:
    return (now or now_local()).strftime("%Y-%m-%d")


def iso(now: datetime | None = None) -> str:
    return (now or now_local()).isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return out


def human_count(n: Any) -> str:
    """播放量/点赞数的中文短写：12345 -> 1.2 万。"""
    try:
        num = int(n)
    except (TypeError, ValueError):
        return "0"
    if num < 10_000:
        return str(num)
    if num < 100_000_000:
        return f"{num / 10_000:.1f}".rstrip("0").rstrip(".") + " 万"
    return f"{num / 100_000_000:.1f}".rstrip("0").rstrip(".") + " 亿"


def human_duration(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_duration(text: Any) -> int:
    """B 站搜索结果里的 ``"12:34"`` / ``"1:02:03"`` -> 秒。"""
    if isinstance(text, (int, float)):
        return max(0, int(text))
    parts = str(text or "").strip().split(":")
    if not parts or not all(p.strip().isdigit() for p in parts):
        return 0
    nums = [int(p) for p in parts]
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


def strip_html(text: Any) -> str:
    """搜索结果标题里的 ``<em class="keyword">`` 标签。"""
    return html.unescape(_TAG_RE.sub("", str(text or "")))


def clean_text(text: Any, limit: int | None = None, keep_newlines: bool = False) -> str:
    """清洗外部文本：去控制符、压空白、可选截断。

    字幕/评论/弹幕来自平台，属于**不可信素材**；这里只做展示层清洗，
    不试图"理解"或执行其中的任何指令。
    """
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _CONTROL_RE.sub("", value)
    if keep_newlines:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = "\n".join(_SPACE_RE.sub(" ", line).strip() for line in value.split("\n"))
        value = _MULTI_NL_RE.sub("\n\n", value).strip()
    else:
        value = _SPACE_RE.sub(" ", value.replace("\r", " ").replace("\n", " ")).strip()
    if limit is not None and len(value) > limit:
        value = value[: max(0, limit - 1)].rstrip() + "…"
    return value


def looks_like_injection(text: str) -> bool:
    lowered = clean_text(text, keep_newlines=False).lower()
    return any(hint in lowered for hint in _INJECTION_HINTS)


def truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def chunk_text(text: str, size: int = 1500, max_chunks: int = 12) -> list[str]:
    """按行/段切消息，避免 QQ 等平台单条过长被截。"""
    text = (text or "").strip()
    if not text:
        return []
    size = max(200, int(size))
    chunks: list[str] = []
    current = ""
    for para in re.split(r"(\n)", text):
        if not para:
            continue
        if len(current) + len(para) <= size:
            current += para
            continue
        if current.strip():
            chunks.append(current.rstrip())
        current = ""
        while len(para) > size:
            chunks.append(para[:size])
            para = para[size:]
        current = para
    if current.strip():
        chunks.append(current.rstrip())
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        chunks[-1] += "\n\n……（内容太长，后面的省略了；完整内容在插件数据目录里）"
    return chunks


def extract_bvid(text: str) -> str | None:
    match = _BV_RE.search(text or "")
    return match.group(0) if match else None


def safe_filename(text: str, limit: int = 40) -> str:
    value = re.sub(r"[\\/:*?\"<>|\s]+", "-", clean_text(text, limit=limit))
    return value.strip("-.") or "untitled"


def date_range(days: int, now: datetime | None = None) -> tuple[date, date]:
    end = (now or now_local()).date()
    start = end - timedelta(days=max(0, days - 1))
    return start, end


def percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * p) - 1))
    return ordered[idx]
