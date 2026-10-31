"""SESSDATA 离线体检：不联网、不回显值，只看形态和过期时间。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


def inspect_sessdata(sessdata: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    value = str(sessdata or "")
    segments = value.split("%2C")
    report: dict[str, Any] = {
        "length": len(value),
        "segments": len(segments),
        "expiry": None,
        "expired": None,
        "problems": [],
        "warnings": [],
    }
    if not value:
        report["problems"].append("SESSDATA 是空的")
        return report
    if re.search(r"\s", value):
        report["problems"].append("里面有空白字符（换行/空格），粘贴时被断开了")
    if value != value.strip():
        report["problems"].append("首尾有多余空白")
    if value.lower().startswith("sessdata="):
        report["problems"].append("把键名 SESSDATA= 也粘进来了")
    if ";" in value:
        report["problems"].append("末尾带着分号（SESSDATA=…; 那种），会污染请求头")
    if "," in value:
        report["problems"].append("出现未编码的逗号 —— 这串被 URL 解码过，B 站不认")
    if len(value) < 100:
        report["problems"].append(f"只有 {len(value)} 个字符，正常 SESSDATA 一般 150~300（可能只复制了一段）")
    if report["segments"] != 3:
        report["warnings"].append(f"按 %2C 切成 {report['segments']} 段（正常是 3 段）")
    if len(segments) >= 2 and segments[1].isdigit() and len(segments[1]) == 10:
        try:
            expiry = datetime.fromtimestamp(int(segments[1]))
            report["expiry"] = expiry
            report["expired"] = expiry < now
            if report["expired"]:
                report["problems"].append(f"这串自带的过期时间是 {expiry:%Y-%m-%d %H:%M}，已经过期了")
        except (ValueError, OSError, OverflowError):
            report["warnings"].append("第 2 段看着像时间戳但解不出来")
    elif len(segments) >= 2:
        report["warnings"].append("第 2 段不是 10 位时间戳（正常 SESSDATA 的第 2 段是过期时间）")
    return report


def describe_sessdata(report: dict[str, Any]) -> str:
    bits: list[str] = []
    expiry = report.get("expiry")
    if expiry:
        bits.append(f"有效期到 {expiry:%Y-%m-%d %H:%M}")
    problems = list(report.get("problems") or [])
    warnings = list(report.get("warnings") or [])
    if problems:
        bits.append("；".join(problems[:3]))
    elif warnings:
        bits.append("；".join(warnings[:2]))
    else:
        bits.append("形态正常")
    return " · ".join(bits)
