"""B 站扫码登录（passport 网页二维码接口）。

纯标准库；不依赖 aiohttp。调用方用 asyncio.to_thread 包一层，避免阻塞事件循环。
参考 astrbot_plugin_bili_whale（MIT）的登录流程，简化为「生成 → 轮询 → 取 cookie」。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from .qr import to_png

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

STATE_OK = 0
STATE_WAITING = 86101
STATE_SCANNED = 86090
STATE_EXPIRED = 86038

_KEEP = ("sessdata", "bili_jct", "buvid3")
_ALIASES = {
    "sessdata": ("SESSDATA", "sessdata"),
    "bili_jct": ("bili_jct", "biliJct", "BILI_JCT"),
    "buvid3": ("buvid3", "Buvid3"),
}


def _open(url: str, timeout: float = 15.0):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        cookies = list(resp.headers.get_all("Set-Cookie") or [])
        final = resp.geturl() or url
    return body, cookies, final


def _json(url: str, timeout: float = 15.0):
    body, cookies, final = _open(url, timeout)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise RuntimeError(f"B 站返回的不是 JSON：{body[:80]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"B 站返回的不是 JSON 对象：{body[:80]}")
    return data, cookies, final


def generate(timeout: float = 15.0) -> tuple[str, str]:
    """申请二维码，返回 (二维码内容 URL, qrcode_key)。"""
    data, _cookies, _final = _json(GENERATE_URL, timeout)
    if int(data.get("code", -1)) != 0:
        raise RuntimeError(f"申请二维码失败：code={data.get('code')} {data.get('message')}")
    payload = data.get("data") or {}
    url = str(payload.get("url") or "")
    key = str(payload.get("qrcode_key") or "")
    if not url or not key:
        raise RuntimeError("申请二维码失败：B 站没给 url / qrcode_key")
    return url, key


def poll(qrcode_key: str, timeout: float = 15.0) -> dict[str, Any]:
    query = urllib.parse.urlencode({"qrcode_key": qrcode_key, "source": "main-fe-header"})
    data, cookies, final = _json(f"{POLL_URL}?{query}", timeout)
    payload = data.get("data")
    out: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
    out.setdefault("code", data.get("code", -1))
    out.setdefault("message", data.get("message", ""))
    jar = _parse_cookies(cookies)
    out["cookies"] = jar
    out["cookie_names"] = sorted(jar)
    out["final_url"] = final
    return out


def _parse_cookies(headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in headers or []:
        head = str(line).split(";", 1)[0].strip()
        name, sep, value = head.partition("=")
        if sep and name.strip():
            out[name.strip()] = value.strip().strip('"')
    return out


def _parse_url(url: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (url or "").split("?", 1)[-1].split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        if key:
            out[key] = value
    return out


def _norm_sess(value: str) -> str:
    value = (value or "").strip().strip('"')
    if not value:
        return ""
    if "%2C" in value.upper():
        return value.replace("%2c", "%2C")
    return value.replace(",", "%2C")


def _pick(bag: dict[str, Any], field: str) -> str:
    lower = {str(k).lower(): v for k, v in (bag or {}).items()}
    for name in _ALIASES[field]:
        if name in (bag or {}):
            return str(bag[name])
        if name.lower() in lower:
            return str(lower[name.lower()])
    return ""


def _from_bag(bag: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for field in _KEEP:
        raw = _pick(bag, field)
        if raw:
            out[field] = _norm_sess(raw) if field == "sessdata" else raw
    return out


def credentials(payload: Any) -> dict[str, str]:
    """从轮询响应里取 cookie；三路兜底（Set-Cookie / 回调 URL / 顶层）。"""
    src = payload if isinstance(payload, dict) else {"url": str(payload or "")}
    out = _from_bag(src.get("cookies") or {})
    for key, value in _from_bag(_parse_url(str(src.get("url") or ""))).items():
        out.setdefault(key, value)
    for key, value in _from_bag(src).items():
        out.setdefault(key, value)
    return out


def fetch_callback(url: str, timeout: float = 10.0) -> dict[str, str]:
    """GET 回调地址，把这一步才 Set-Cookie 的票捞出来。"""
    body, cookies, final = _open(url, timeout)
    out = _from_bag(_parse_cookies(cookies))
    for key, value in _from_bag(_parse_url(final)).items():
        out.setdefault(key, value)
    if not out.get("sessdata"):
        for key, value in _from_bag(_parse_url(body[:4000])).items():
            out.setdefault(key, value)
    return out


def refresh_jct(sessdata: str, timeout: float = 10.0) -> str:
    """拿着 SESSDATA 敲一次首页，补 bili_jct；补不到只影响写操作。"""
    sess = _norm_sess(sessdata)
    if not sess:
        return ""
    req = urllib.request.Request(
        "https://www.bilibili.com/",
        headers={
            "User-Agent": UA,
            "Cookie": f"SESSDATA={sess}",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            jar = _parse_cookies(resp.headers.get_all("Set-Cookie") or [])
        return str(jar.get("bili_jct") or "")
    except Exception:
        return ""


def render(url: str, path: str) -> str:
    return to_png(url, path, "M", 8)


def diagnose(payload: Any) -> str:
    """登录成功却没 cookie 时，给一行不含值的现场信息。"""
    if not isinstance(payload, dict):
        return f"响应类型={type(payload).__name__}"
    names = payload.get("cookie_names") or sorted(payload.get("cookies") or {})
    url = str(payload.get("url") or "")
    host = urllib.parse.urlsplit(url).netloc if url.startswith("http") else "（空）"
    keys = sorted(_parse_url(url))[:8]
    return "；".join(
        [
            f"cookie 名={','.join(names) or '（一个没有）'}",
            f"回调域名={host}",
            f"回调参数={','.join(keys) or '（空）'}",
        ]
    )
