"""小工具：时间、配置读取、文本清洗。

这些函数尽量保持"纯"：不读文件、不发网络，方便在测试里直接调用。
"""

from __future__ import annotations

import html
import logging
import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

logger = logging.getLogger("bili_whale.util")

# 控制字符（保留换行/制表）—— 评论区、字幕里偶尔会混进终端控制序列，
# 拼进提示词或消息前统一清掉。
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_BV_RE = re.compile(r"[Bb][Vv][0-9A-Za-z]{10}")
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
    """从文本里抽第一个 BV 号；前缀大小写不敏感，统一成 ``BV``。"""
    match = _BV_RE.search(text or "")
    if not match:
        return None
    value = match.group(0)
    return "BV" + value[2:]


def av2bv(aid: int) -> str | None:
    """把 av 号（数字 aid）换成 BV 号。"""
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        return None
    if aid <= 0:
        return None
    table = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
    positions = (11, 10, 3, 8, 4, 6)
    value = (aid ^ 177451812) + 8728348608
    chars = list("BV1  4 1 7  ")
    for i, pos in enumerate(positions):
        chars[pos] = table[(value // (58 ** i)) % 58]
    return "".join(chars)


_B23_RE = re.compile(r"(?:https?://)?b23\.tv/([0-9A-Za-z]+)")
_VIDEO_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)",
    re.IGNORECASE,
)
_BARE_AV_RE = re.compile(r"(?<![0-9A-Za-z])[aA][vV](\d{1,12})(?![0-9])")
# App 内分享出来的链接是 bilibili://video/170001 或 bilibili://video/BV1xx…
# B 站「@我的」接口里的 uri 就是这种形式。
_APP_URI_RE = re.compile(
    r"bilibili://video/(BV[0-9A-Za-z]{10}|\d{1,12})", re.IGNORECASE
)
# 短链 302 落到视频页时，URL 里正常就有 BV；只有落到壳页面才需要看这两处。
_CANONICAL_RE = re.compile(
    r"""<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']""", re.IGNORECASE
)
_OG_URL_RE = re.compile(
    r"""<meta[^>]+property=["']og:url["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE
)


def normalize_bvid(token: str | None) -> str | None:
    """把 ``bv1xx…`` / ``BV1xx…`` 统一成大写前缀，非法值返回 None。"""
    if not token:
        return None
    value = token.strip()
    if len(value) == 12 and value[:2].lower() == "bv":
        candidate = "BV" + value[2:]
    else:
        candidate = value
    return candidate if _BV_RE.fullmatch(candidate) else None


async def resolve_bvid(text: str, timeout: float = 10.0) -> str | None:
    """从一段话里找出视频号。

    顺序：文本里直接有 BV → 用；``bilibili://video/...`` App 链接 →
    av 号翻译或 BV 直接取；``b23.tv/xxx`` 短链 → 跟随跳转再取；
    ``bilibili.com/video/BV...`` 或 ``av...`` → 直接翻译；都不行返回 None。

    短链展开失败时只记日志、返回 None：调用方据此提示"没认出视频号"，
    而不是把网络故障当成用户输入错误。
    """
    raw = text or ""
    direct = extract_bvid(raw)
    if direct:
        return direct

    target = ""
    app = _APP_URI_RE.search(raw)
    link = _B23_RE.search(raw)
    if app:
        token = app.group(1)
        if token[:2].lower() == "bv":
            return normalize_bvid(token)
        return av2bv(token)
    if link:
        target = "https://b23.tv/" + link.group(1)
    else:
        match = _VIDEO_URL_RE.search(raw)
        if match:
            token = match.group(1)
            if token[:2].lower() == "bv":
                return normalize_bvid(token)
            return av2bv(token[2:])
        bare_av = _BARE_AV_RE.search(raw)
        if bare_av:
            return av2bv(bare_av.group(1))
        return None

    try:
        import aiohttp
    except ImportError:  # pragma: no cover - AstrBot 自带 aiohttp
        logger.warning("[bili-whale] 解析短链需要 aiohttp，但没装上：%s", target)
        return None

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        timeout_obj = aiohttp.ClientTimeout(total=max(3.0, timeout))
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(target, allow_redirects=True, headers=headers) as resp:
                found = extract_bvid(str(resp.url))
                if found:
                    return found
                if resp.status != 200:
                    logger.warning(
                        "[bili-whale] 短链展开失败：HTTP %s，%s", resp.status, target
                    )
                    return None
                ctype = str(resp.headers.get("Content-Type") or "").lower()
                if "html" not in ctype:
                    # 不是网页，继续扫全文只会撞出乱七八糟的 BV。
                    logger.warning(
                        "[bili-whale] 短链展开后不是网页（%s）：%s", ctype or "?", target
                    )
                    return None
                body = await resp.text(errors="replace")
                for pattern in (_CANONICAL_RE, _OG_URL_RE):
                    page = pattern.search(body)
                    if page:
                        found = extract_bvid(page.group(1))
                        if found:
                            return found
                logger.warning(
                    "[bili-whale] 短链没跳到视频页：%s → %s", target, str(resp.url)[:120]
                )
                return None
    except Exception as exc:  # 超时、证书、DNS 都可能，别吞掉线索
        logger.warning(
            "[bili-whale] 短链展开出错 %s：%s: %s", target, type(exc).__name__, exc
        )
        return None


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
