"""B 站 API 客户端（aiohttp）。

只覆盖"刷视频 / 看懂 / 分享"需要的最小集合：
- 登录态：``x/web-interface/nav``
- 搜索：``x/web-interface/wbi/search/type``（WBI 签名）
- 视频信息：``x/web-interface/view``
- 官方字幕：``x/player/wbi/v2``（拿带 auth_key 的 URL），失败再试 ``x/v2/subtitle/web/view``
- 热评：``x/v2/reply``
- 弹幕：``comment.bilibili.com/<cid>.xml``
- 发评论：``x/v2/reply/add``（WBI + csrf）

不实现私信、动态、收藏、直播、三连、批量操作 —— 能力越窄越安全。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Mapping
from urllib.parse import quote

from .errors import BiliAuthError, BiliError, BiliRisk, BudgetError
from .models import Comment, DanmakuHotspot, Subtitle, Video
from .traffic import TrafficGuard
from .util import clean_text, parse_duration, strip_html

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
API = "https://api.bilibili.com"
REFERER = "https://www.bilibili.com/"

# WBI 混淆表（B 站前端写死的 64 项表）。
MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

RISK_CODES = {-412, -352, -799, -509, -10403, -429, 412, 352, 799, 509, 10403, 429}
_NOT_LOGGED_IN = {-101, -400, -403}


def uploader_key(url: str) -> str:
    """从 ``.../7cd084941338484aae1ad9425b84077c.png`` 里取文件名。"""
    name = str(url or "").rstrip("/").rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def mixin_key(img_url: str, sub_url: str) -> str:
    raw = uploader_key(img_url) + uploader_key(sub_url)
    # img 文件名 32 位 + sub 文件名 32 位 = 64；少一位说明 nav 没给全。
    if len(raw) != 64:
        raise BiliError("WBI 签名密钥缺失（nav 没返回完整的 wbi_img），请稍后重试或检查登录状态")
    return "".join(raw[i] for i in MIXIN_TABLE)[:32]


def _encode(value: Any) -> str:
    # 对应前端的 encodeURIComponent(String(v).replace(/[!'()*]/g, ''))
    cleaned = re.sub(r"[!'()*]", "", str(value))
    return quote(cleaned, safe="-_.~")


def sign_wbi(params: Mapping[str, Any], key: str, ts: int | None = None) -> str:
    """返回 ``k=v&...&wts=...&w_rid=...``。"""
    values = {str(k): v for k, v in params.items() if k != "w_rid"}
    values["wts"] = int(ts if ts is not None else time.time())
    query = "&".join(f"{_encode(k)}={_encode(values[k])}" for k in sorted(values))
    digest = hashlib.md5((query + key).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={digest}"


class BiliAPI:
    def __init__(
        self,
        cookie: Mapping[str, str] | None,
        guard: TrafficGuard,
        logger: Any = None,
        user_agent: str = UA,
    ):
        self.cookie = {k: str(v or "").strip() for k, v in (cookie or {}).items()}
        self.guard = guard
        self.logger = logger
        self.user_agent = user_agent or UA
        self._session: Any = None
        self._nav: tuple[float, dict[str, Any]] = (0.0, {})
        self._view_cache: dict[str, tuple[float, Video]] = {}
        self._wbi_key: str = ""
        self._wbi_at: float = 0.0
        self._json_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ── 生命周期 ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._session is not None:
            return
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - AstrBot 自带 aiohttp
            raise BiliError("缺少 aiohttp：请在插件目录执行 pip install -r requirements.txt") from exc
        timeout = aiohttp.ClientTimeout(total=25, connect=10)
        self._session = aiohttp.ClientSession(timeout=timeout, headers=self._base_headers())

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie.get("sessdata"))

    @property
    def has_write_cookie(self) -> bool:
        return bool(self.cookie.get("sessdata") and self.cookie.get("bili_jct"))

    def _base_headers(self, referer: str = REFERER) -> dict[str, str]:
        headers = {
            "user-agent": self.user_agent,
            "referer": referer,
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        cookie = self._cookie_header()
        if cookie:
            headers["cookie"] = cookie
        return headers

    def _cookie_header(self) -> str:
        bits = []
        for key in ("SESSDATA", "bili_jct", "buvid3"):
            value = self.cookie.get(key.lower()) or self.cookie.get(key) or ""
            if value:
                bits.append(f"{key}={value}")
        return "; ".join(bits)

    def _ensure(self) -> None:
        if self._session is None:
            raise BiliError("网络会话还没启动（插件可能正在重载）")

    # ── 底层请求 ─────────────────────────────────────────────────────────
    async def _get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        wbi: bool = False,
        referer: str = REFERER,
        allow_codes: set[int] | tuple[int, ...] = (),
        cache_key: str = "",
        cache_ttl: float = 0.0,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self._ensure()
        now = time.monotonic()
        if cache_key and cache_ttl and cache_key in self._json_cache:
            at, payload = self._json_cache[cache_key]
            if now - at < cache_ttl:
                return payload
        await self.guard.before_read()
        params = dict(params or {})
        url = f"{API}{path}"
        if wbi:
            key = await self.wbi_key()
            url = f"{url}?{sign_wbi(params, key)}"
            params = {}
        try:
            async with self._session.get(
                url,
                params=params or None,
                headers=self._base_headers(referer),
                timeout=timeout,
            ) as resp:
                status = resp.status
                text = await resp.text(errors="replace")
        except BiliRisk:
            raise
        except Exception as exc:  # aiohttp 的超时/连接错误
            raise BiliError(f"请求失败（网络异常）：{type(exc).__name__}") from exc
        if status in (412, 429):
            minutes = self.guard.hit_risk(status, f"HTTP {status}（平台限流）")
            raise BiliRisk(f"撞上 B 站限流（HTTP {status}），已暂停约 {minutes} 分钟", status, minutes)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise BiliError(f"接口返回的不是 JSON（HTTP {status}）") from exc
        if not isinstance(payload, dict):
            raise BiliError("接口返回结构异常")
        code = payload.get("code")
        if code in RISK_CODES:
            minutes = self.guard.hit_risk(code, str(payload.get("message") or ""))
            raise BiliRisk(
                f"B 站返回风控码 {code}（{payload.get('message') or '限流'}），已暂停约 {minutes} 分钟",
                code,
                minutes,
            )
        if code not in (0, None) and code not in allow_codes:
            message = str(payload.get("message") or "接口失败")
            if code in _NOT_LOGGED_IN:
                raise BiliAuthError(f"登录态无效（code={code}：{message}），请重新填 cookie", code)
            raise BiliError(f"{message}（code={code}）", code)
        if cache_key and cache_ttl:
            self._json_cache[cache_key] = (now, payload)
        return payload

    async def _get_text(self, url: str, referer: str = REFERER) -> str:
        self._ensure()
        await self.guard.before_read()
        try:
            async with self._session.get(url, headers=self._base_headers(referer)) as resp:
                status = resp.status
                text = await resp.text(errors="replace")
        except Exception as exc:
            raise BiliError(f"下载失败：{type(exc).__name__}") from exc
        if status in (412, 429):
            minutes = self.guard.hit_risk(status, f"HTTP {status}（下载被限流）")
            raise BiliRisk(f"下载被限流（HTTP {status}），已暂停约 {minutes} 分钟", status, minutes)
        if status >= 400:
            raise BiliError(f"下载失败（HTTP {status}）")
        return text

    # ── 登录 / 签名 ──────────────────────────────────────────────────────
    async def nav(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._nav[1] and now - self._nav[0] < 600:
            return self._nav[1]
        payload = await self._get_json("/x/web-interface/nav", allow_codes=set(_NOT_LOGGED_IN))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        self._nav = (now, data)
        img = ((data.get("wbi_img") or {}).get("img_url")) or ""
        sub = ((data.get("wbi_img") or {}).get("sub_url")) or ""
        if img and sub:
            self._wbi_key = mixin_key(img, sub)
            self._wbi_at = now
        if data.get("isLogin"):
            self.guard.note_success()
        return data

    async def wbi_key(self, force: bool = False) -> str:
        if not force and self._wbi_key and time.monotonic() - self._wbi_at < 600:
            return self._wbi_key
        await self.nav(force=True)
        if not self._wbi_key:
            raise BiliError("WBI 签名不可用：nav 没返回 wbi_img（可能是未登录或接口被限流）")
        return self._wbi_key

    async def login_info(self) -> dict[str, Any]:
        data = await self.nav(force=True)
        return {
            "is_login": bool(data.get("isLogin")),
            "uname": str(data.get("uname") or ""),
            "mid": int(data.get("mid") or 0),
            "money": data.get("money"),
            "level": data.get("level_info", {}).get("current_level") if isinstance(data.get("level_info"), dict) else None,
        }

    # ── 搜索 ─────────────────────────────────────────────────────────────
    async def search(self, keyword: str, page: int = 1, page_size: int = 30) -> list[Video]:
        keyword = clean_text(keyword, limit=60)
        if not keyword:
            return []
        payload = await self._get_json(
            "/x/web-interface/wbi/search/type",
            {
                "search_type": "video",
                "keyword": keyword,
                "page": max(1, int(page)),
                "page_size": max(1, min(50, int(page_size))),
                "order": "pubdate",
            },
            wbi=True,
            referer=f"https://search.bilibili.com/all?keyword={quote(keyword)}",
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = data.get("result") or data.get("results") or []
        videos: list[Video] = []
        if not isinstance(rows, list):
            return videos
        for row in rows:
            if not isinstance(row, dict):
                continue
            bvid = str(row.get("bvid") or "").strip()
            if not bvid.startswith("BV"):
                continue
            video = Video.from_search(row)
            video.title = clean_text(strip_html(video.title), limit=200)
            video.desc = clean_text(strip_html(video.desc), limit=300)
            video.author = clean_text(strip_html(video.author), limit=60)
            if not video.duration:
                video.duration = parse_duration(row.get("duration"))
            videos.append(video)
        return videos

    # ── 视频信息 ─────────────────────────────────────────────────────────
    async def video_info(self, bvid: str, force: bool = False) -> Video:
        bvid = clean_text(bvid, limit=20)
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            raise BiliError(f"不是有效的 BV 号：{bvid}")
        now = time.monotonic()
        cached = self._view_cache.get(bvid)
        if cached and not force and now - cached[0] < 6 * 3600:
            return cached[1]
        try:
            payload = await self._get_json(
                "/x/web-interface/view",
                {"bvid": bvid},
                referer=f"https://www.bilibili.com/video/{bvid}/",
                cache_key=f"view:{bvid}",
                cache_ttl=6 * 3600,
            )
        except BiliAuthError:
            raise
        except (BiliRisk, BudgetError):
            raise
        except BiliError:
            # 少数 IP/账号会被要求走 WBI 签名版；签名版也失败就把原错误抛给上层。
            payload = await self._get_json(
                "/x/web-interface/wbi/view",
                {"bvid": bvid},
                wbi=True,
                referer=f"https://www.bilibili.com/video/{bvid}/",
                cache_key=f"view:{bvid}",
                cache_ttl=6 * 3600,
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BiliError("视频信息为空（可能已删除或仅 UP 可见）")
        video = Video.from_view(data)
        video.title = clean_text(video.title, limit=200)
        video.desc = clean_text(video.desc, limit=1000)
        video.author = clean_text(video.author, limit=60)
        self._view_cache[bvid] = (now, video)
        return video

    # ── 字幕 ─────────────────────────────────────────────────────────────
    async def subtitles(self, video: Video, preferred_lang: str = "ai-zh") -> Subtitle | None:
        if not video.cid:
            try:
                video = await self.video_info(video.bvid)
            except (BiliRisk, BudgetError):
                raise
            except BiliError:
                return None
        tracks: list[dict[str, Any]] = []
        try:
            payload = await self._get_json(
                "/x/player/wbi/v2",
                {"bvid": video.bvid, "cid": video.cid},
                wbi=True,
                referer=video.link,
            )
            tracks = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        except BiliRisk:
            raise
        except BudgetError:
            raise
        except BiliError:
            tracks = []
        if not isinstance(tracks, list) or not tracks:
            # 兜底：播放器实际调用的端点。仍拿不到就按"没有字幕"处理。
            try:
                ext = json.dumps({"video_type": 1}, separators=(",", ":"))
                payload = await self._get_json(
                    "/x/v2/subtitle/web/view",
                    {
                        "oid": video.aid,
                        "pid": video.aid,
                        "context_ext": ext,
                        "type": 1,
                        "cur_production_type": 0,
                        "preferred_language": preferred_lang or "ai-zh",
                        "playlist_switch": 0,
                    },
                    referer=video.link,
                )
                tracks = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            except BiliRisk:
                raise
            except BudgetError:
                raise
            except BiliError:
                tracks = []
        if not isinstance(tracks, list) or not tracks:
            return None
        track = _pick_track(tracks, preferred_lang)
        if not track:
            return None
        url = str(track.get("subtitle_url") or "").strip()
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        try:
            raw = await self._get_text(url, referer=video.link)
            parsed = json.loads(raw)
            body = parsed.get("body") or [] if isinstance(parsed, dict) else []
        except BiliRisk:
            raise
        except BudgetError:
            raise
        except (BiliError, ValueError, AttributeError, TypeError):
            return None
        lines: list[str] = []
        chars = 0
        for row in body:
            if not isinstance(row, dict):
                continue
            content = clean_text(row.get("content"), limit=300)
            if not content:
                continue
            try:
                start = float(row.get("from") or 0)
            except (TypeError, ValueError):
                start = 0.0
            lines.append(f"[{int(start) // 60:02d}:{int(start) % 60:02d}] {content}")
            chars += len(content)
            if len(lines) >= 1200 or chars > 24_000:
                break
        if not lines:
            return None
        return Subtitle(lang=str(track.get("lan") or "unknown"), lines=lines)

    # ── 评论 ─────────────────────────────────────────────────────────────
    async def hot_comments(self, video: Video, limit: int = 10) -> list[Comment]:
        aid = video.aid
        if not aid:
            try:
                aid = (await self.video_info(video.bvid)).aid
            except (BiliRisk, BudgetError):
                raise
            except BiliError:
                return []
        if not aid:
            return []
        try:
            payload = await self._get_json(
                "/x/v2/reply",
                {"type": 1, "oid": aid, "sort": 2, "ps": max(1, min(30, limit + 5)), "pn": 1},
                referer=video.link,
            )
        except BiliRisk:
            raise
        except BudgetError:
            raise
        except BiliError:
            return []
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows: list[dict[str, Any]] = []
        top = data.get("top")
        if isinstance(top, list):
            rows.extend([r for r in top if isinstance(r, dict)])
        upper = data.get("upper")
        if isinstance(upper, dict) and isinstance(upper.get("top"), dict):
            rows.append(upper["top"])
        replies = data.get("replies")
        if isinstance(replies, list):
            rows.extend([r for r in replies if isinstance(r, dict)])
        out: list[Comment] = []
        seen: set[str] = set()
        for row in rows:
            rpid = str(row.get("rpid_str") or row.get("rpid") or "")
            if rpid in seen:
                continue
            seen.add(rpid)
            member = row.get("member") if isinstance(row.get("member"), dict) else {}
            content = row.get("content") if isinstance(row.get("content"), dict) else {}
            text = clean_text(content.get("message"), limit=220)
            if not text or text in {"此用户已删除", "deleted"}:
                continue
            try:
                like = int(row.get("like") or 0)
            except (TypeError, ValueError):
                like = 0
            out.append(
                Comment(
                    user=clean_text(member.get("uname") or "匿名", limit=40),
                    text=text,
                    like=like,
                    rpid=rpid,
                    is_top=bool(row.get("rpid") in [r.get("rpid") for r in (top or []) if isinstance(r, dict)]),
                )
            )
            if len(out) >= limit:
                break
        return out

    # ── 弹幕 ─────────────────────────────────────────────────────────────
    async def danmaku_hotspots(self, cid: int, duration: int = 0, buckets: int = 8) -> list[DanmakuHotspot]:
        if not cid:
            return []
        url = f"https://comment.bilibili.com/{cid}.xml"
        try:
            raw = await self._get_text(url)
        except BiliRisk:
            raise
        except BudgetError:
            raise
        except BiliError:
            return []
        try:
            root = ET.fromstring(raw.encode("utf-8"))
        except ET.ParseError:
            return []
        per_minute: dict[int, list[str]] = {}
        counts: dict[int, int] = {}
        total = 0
        for node in root.iter("d"):
            text = clean_text(node.text, limit=80)
            p = str(node.get("p") or "")
            if not text or not p:
                continue
            try:
                seconds = float(p.split(",", 1)[0])
            except (TypeError, ValueError):
                continue
            minute = max(0, int(seconds // 60))
            counts[minute] = counts.get(minute, 0) + 1
            samples = per_minute.setdefault(minute, [])
            if len(samples) < 6 and text not in samples and len(text) >= 2:
                samples.append(text)
            total += 1
            if total > 12_000:
                break
        if not counts:
            return []
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[: max(1, buckets)]
        out = [
            DanmakuHotspot(minute=minute, count=count, samples=per_minute.get(minute, [])[:4])
            for minute, count in sorted(top, key=lambda kv: kv[0])
        ]
        return out

    # ── 写：发评论 ───────────────────────────────────────────────────────
    async def post_comment(self, aid: int, message: str) -> str:
        self._ensure()
        if not self.has_write_cookie:
            raise BiliAuthError("缺少 SESSDATA 或 bili_jct，无法发评论")
        message = clean_text(message, limit=1000, keep_newlines=False)
        if not message:
            raise BiliError("评论内容为空")
        params = {
            "type": "1",
            "oid": str(aid),
            "message": message,
            "plat": "1",
            "csrf": self.cookie.get("bili_jct", ""),
        }
        signed = ""
        try:
            signed = sign_wbi(params, await self.wbi_key())
        except (BiliRisk, BudgetError):
            raise
        except BiliError:
            signed = ""  # 没签名也允许尝试，接口自己会判
        await self.guard.before_write()
        url = f"{API}/x/v2/reply/add"
        if signed:
            # 与 B 站前端/whale 一致：URL 上只带 w_rid + wts，业务参数放 body。
            signed_parts = dict(part.split("=", 1) for part in signed.split("&") if "=" in part)
            url = (
                f"{url}?w_rid={signed_parts.get('w_rid', '')}"
                f"&wts={signed_parts.get('wts', '')}"
            )
        body = "&".join(f"{quote(k)}={quote(str(v))}" for k, v in params.items())
        try:
            async with self._session.post(
                url,
                data=body.encode("utf-8"),
                headers={
                    **self._base_headers("https://www.bilibili.com/"),
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": "https://www.bilibili.com",
                },
            ) as resp:
                status = resp.status
                text = await resp.text(errors="replace")
        except Exception as exc:
            raise BiliError(f"发评论失败：{type(exc).__name__}") from exc
        if status in (412, 429):
            minutes = self.guard.hit_risk(status, "发评论被限流")
            raise BiliRisk(f"发评论被限流（HTTP {status}），已暂停约 {minutes} 分钟", status, minutes)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise BiliError(f"发评论返回非 JSON（HTTP {status}）") from exc
        code = payload.get("code")
        if code in RISK_CODES:
            minutes = self.guard.hit_risk(code, str(payload.get("message") or ""))
            raise BiliRisk(f"发评论触发风控 code={code}，已暂停约 {minutes} 分钟", code, minutes)
        if code != 0:
            message = str(payload.get("message") or "未知原因")
            if code in _NOT_LOGGED_IN:
                raise BiliAuthError(f"发评论失败：登录态无效（code={code}：{message}）", code)
            raise BiliError(f"发评论失败：{message}（code={code}）", code)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return str(data.get("rpid_str") or data.get("rpid") or "")


def _pick_track(tracks: list[dict[str, Any]], preferred: str) -> dict[str, Any] | None:
    def lang(item: dict[str, Any]) -> str:
        return str(item.get("lan") or "")

    for want in (preferred, "ai-zh", "zh-CN", "zh-Hans"):
        if not want:
            continue
        for track in tracks:
            if lang(track) == want:
                return track
    for track in tracks:
        if lang(track).startswith("zh"):
            return track
    return tracks[0] if tracks else None
