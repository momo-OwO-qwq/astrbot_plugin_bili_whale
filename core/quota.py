"""配额、冷却与风控熔断（纯函数，方便单测）。

这些限制不是"建议"，是代码强制的：
- 读：每小时 / 每天上限，撞 412 后熔断；
- 写：每小时 / 每天上限 + 两次写之间至少 N 秒；
- 分享：每天最多 N 条、同一视频 N 天内一次、评分与理由门槛。

默认值按"刚创的小号"设定，见 README。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from .util import as_bool, as_int, cfg_get, iso, now_local, parse_iso, today_str

# B 站风控/限流码：撞到就停手，不硬顶。
RISK_CODES = {-412, -352, -799, -509, -10403, -429, 412, 352, 799, 509, 10403, 429}
# 熔断时长按"连续撞墙次数"递增（分钟）。
BACKOFF_MINUTES = (5, 10, 20, 30)


@dataclass(frozen=True)
class Limits:
    read_per_hour: int = 600
    read_per_day: int = 3000
    write_per_hour: int = 12
    write_per_day: int = 30
    write_cooldown_sec: int = 120
    share_max_per_day: int = 3
    same_video_days: int = 7
    min_score: int = 13
    min_reason_len: int = 8
    dm_per_day: int = 3

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | Any) -> "Limits":
        return cls(
            read_per_hour=as_int(cfg_get(cfg, "risk.read_per_hour", 600), 600, 1, 100_000),
            read_per_day=as_int(cfg_get(cfg, "risk.read_per_day", 3000), 3000, 1, 1_000_000),
            write_per_hour=as_int(cfg_get(cfg, "risk.write_per_hour", 12), 12, 1, 1000),
            write_per_day=as_int(cfg_get(cfg, "risk.write_per_day", 30), 30, 1, 10_000),
            write_cooldown_sec=as_int(cfg_get(cfg, "risk.write_cooldown_sec", 120), 120, 0, 86_400),
            share_max_per_day=as_int(cfg_get(cfg, "share.max_per_day", 3), 3, 0, 20),
            same_video_days=as_int(cfg_get(cfg, "share.same_video_days", 7), 7, 1, 365),
            min_score=as_int(cfg_get(cfg, "share.min_score", 13), 13, 0, 20),
            min_reason_len=as_int(cfg_get(cfg, "share.min_reason_len", 8), 8, 1, 200),
            dm_per_day=as_int(cfg_get(cfg, "push.bili_dm.max_per_day", 3), 3, 0, 20),
        )


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:  # 让调用方可以 `if decision:`
        return self.ok


def _hour_key(now: datetime) -> str:
    return now.strftime("%Y%m%d%H")


def normalize(quota: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """按天/小时滚动计数器，并清理过期的分享判重记录。"""
    now = now or now_local()
    today = today_str(now)
    hour = _hour_key(now)
    if quota.get("date") != today:
        quota["date"] = today
        quota["reads_day"] = 0
        quota["writes_day"] = 0
        quota["shares_day"] = 0
        quota["dm_day"] = 0
    if quota.get("hour") != hour:
        quota["hour"] = hour
        quota["reads_hour"] = 0
        quota["writes_hour"] = 0
    share_bv = quota.get("share_bv")
    if not isinstance(share_bv, dict):
        share_bv = {}
    # 判重记录保留得比 same_video_days 的最长可配值更久（配置上限 365 天）。
    cutoff = now - timedelta(days=400)
    quota["share_bv"] = {
        str(k): v
        for k, v in share_bv.items()
        if (parse_iso(v) or cutoff) >= cutoff
    }
    return quota


def ban_remaining_sec(quota: Mapping[str, Any], now: datetime | None = None) -> int:
    now = now or now_local()
    until = parse_iso(quota.get("ban_until"))
    if not until:
        return 0
    return max(0, int((until - now).total_seconds()))


def ban_decision(quota: Mapping[str, Any], now: datetime | None = None) -> Decision:
    left = ban_remaining_sec(quota, now)
    if left <= 0:
        return Decision(True)
    minutes = max(1, (left + 59) // 60)
    reason = quota.get("ban_reason") or "平台风控"
    return Decision(False, f"平台要求暂停：{reason}；约 {minutes} 分钟后自动恢复")


def can_read(quota: dict[str, Any], limits: Limits, now: datetime | None = None) -> Decision:
    now = now or now_local()
    normalize(quota, now)
    banned = ban_decision(quota, now)
    if not banned:
        return banned
    if quota["reads_hour"] >= limits.read_per_hour:
        return Decision(False, f"本小时读取额度已满（{limits.read_per_hour}/时）")
    if quota["reads_day"] >= limits.read_per_day:
        return Decision(False, f"今天读取额度已满（{limits.read_per_day}/天）")
    return Decision(True)


def record_read(quota: dict[str, Any], now: datetime | None = None) -> None:
    now = now or now_local()
    normalize(quota, now)
    quota["reads_hour"] = int(quota.get("reads_hour", 0)) + 1
    quota["reads_day"] = int(quota.get("reads_day", 0)) + 1


def can_write(quota: dict[str, Any], limits: Limits, now: datetime | None = None) -> Decision:
    now = now or now_local()
    normalize(quota, now)
    banned = ban_decision(quota, now)
    if not banned:
        return banned
    if quota["writes_hour"] >= limits.write_per_hour:
        return Decision(False, f"本小时写入额度已满（{limits.write_per_hour}/时）")
    if quota["writes_day"] >= limits.write_per_day:
        return Decision(False, f"今天写入额度已满（{limits.write_per_day}/天）")
    last = parse_iso(quota.get("last_write"))
    if last and limits.write_cooldown_sec > 0:
        left = limits.write_cooldown_sec - int((now - last).total_seconds())
        if left > 0:
            return Decision(False, f"两次写之间要隔 {limits.write_cooldown_sec} 秒，还差 {left} 秒")
    return Decision(True)


def record_write(quota: dict[str, Any], now: datetime | None = None) -> None:
    now = now or now_local()
    normalize(quota, now)
    quota["writes_hour"] = int(quota.get("writes_hour", 0)) + 1
    quota["writes_day"] = int(quota.get("writes_day", 0)) + 1
    quota["last_write"] = iso(now)


def can_share(
    quota: dict[str, Any],
    limits: Limits,
    bvid: str,
    score: int,
    reason: str,
    now: datetime | None = None,
) -> Decision:
    """分享前的硬闸门；与写额度分开检查，调用方两者都要过。"""
    now = now or now_local()
    normalize(quota, now)
    if not bvid:
        return Decision(False, "没有 BV 号")
    if int(score) < limits.min_score:
        return Decision(False, f"评分 {int(score)}/20，低于分享门槛 {limits.min_score}")
    if len((reason or "").strip()) < limits.min_reason_len:
        return Decision(False, f"理由不足 {limits.min_reason_len} 字，说不清为什么值得就不发")
    if int(quota.get("shares_day", 0)) >= limits.share_max_per_day:
        return Decision(False, f"今天分享额度已满（{limits.share_max_per_day} 条/天）")
    last = parse_iso(quota.get("share_bv", {}).get(bvid))
    if last:
        left = limits.same_video_days * 86_400 - int((now - last).total_seconds())
        if left > 0:
            days = max(1, (left + 86_399) // 86_400)
            return Decision(False, f"这个视频 {limits.same_video_days} 天内分享过了，还要等约 {days} 天")
    return Decision(True)


def record_share(quota: dict[str, Any], bvid: str, now: datetime | None = None) -> None:
    now = now or now_local()
    normalize(quota, now)
    quota["shares_day"] = int(quota.get("shares_day", 0)) + 1
    quota.setdefault("share_bv", {})[bvid] = iso(now)


def can_dm(quota: dict[str, Any], limits: Limits, now: datetime | None = None) -> Decision:
    """B 站私信推送的每日上限（写预算和冷却由 TrafficGuard 另管）。"""
    now = now or now_local()
    normalize(quota, now)
    if limits.dm_per_day <= 0:
        return Decision(False, "B 站私信推送的每日上限是 0")
    used = int(quota.get("dm_day", 0))
    if used >= limits.dm_per_day:
        return Decision(False, f"今天的 B 站私信推送额度已满（{limits.dm_per_day} 条/天）")
    return Decision(True)


def record_dm(quota: dict[str, Any], now: datetime | None = None) -> None:
    now = now or now_local()
    normalize(quota, now)
    quota["dm_day"] = int(quota.get("dm_day", 0)) + 1


def hit_risk(
    quota: dict[str, Any],
    code: int | str,
    message: str = "",
    now: datetime | None = None,
) -> int:
    """撞到风控码：熔断 5/10/20/30 分钟，返回熔断秒数。"""
    now = now or now_local()
    normalize(quota, now)
    level = as_int(quota.get("ban_level", 0), 0, 0, len(BACKOFF_MINUTES) - 1)
    minutes = BACKOFF_MINUTES[min(level, len(BACKOFF_MINUTES) - 1)]
    until = now + timedelta(minutes=minutes)
    quota["ban_until"] = iso(until)
    quota["ban_level"] = min(level + 1, len(BACKOFF_MINUTES) - 1)
    quota["ban_reason"] = f"{message or 'HTTP 412/风控'}（code={code}）"
    quota["ban_hits"] = int(quota.get("ban_hits", 0)) + 1
    return minutes * 60


def note_success(quota: dict[str, Any], now: datetime | None = None) -> None:
    """一次成功请求：等熔断结束后把"连续撞墙次数"往回退一格。"""
    now = now or now_local()
    if ban_remaining_sec(quota, now) > 0:
        return
    level = as_int(quota.get("ban_level", 0), 0, 0, len(BACKOFF_MINUTES) - 1)
    if level:
        quota["ban_level"] = level - 1
    if quota.get("ban_until"):
        quota["ban_until"] = ""
        quota["ban_reason"] = ""


def status_text(quota: dict[str, Any], limits: Limits, now: datetime | None = None) -> str:
    now = now or now_local()
    normalize(quota, now)
    left = ban_remaining_sec(quota, now)
    ban = f"⚠️ 风控熔断中，约 {(left + 59) // 60} 分钟" if left else "✓ 未熔断"
    return (
        f"{ban}\n"
        f"读：{quota.get('reads_hour', 0)}/{limits.read_per_hour} 时 · "
        f"{quota.get('reads_day', 0)}/{limits.read_per_day} 天\n"
        f"写：{quota.get('writes_hour', 0)}/{limits.write_per_hour} 时 · "
        f"{quota.get('writes_day', 0)}/{limits.write_per_day} 天\n"
        f"分享：{quota.get('shares_day', 0)}/{limits.share_max_per_day} 条/天\n"
        f"B站私信：{quota.get('dm_day', 0)}/{limits.dm_per_day} 条/天"
    )


def effective_pubdate_days(days: int) -> int:
    return as_int(days, 7, 1, 365)


def share_config_enabled(cfg: Mapping[str, Any] | Any) -> bool:
    enabled = cfg_get(cfg, "share.enabled", None)
    if enabled is not None:
        return as_bool(enabled, False)
    mode = str(cfg_get(cfg, "share.mode", "off") or "off").strip().lower()
    return mode in {"auto", "manual"}


def share_auto_enabled(cfg: Mapping[str, Any] | Any) -> bool:
    if not share_config_enabled(cfg):
        return False
    auto = cfg_get(cfg, "share.auto_send", None)
    if auto is not None:
        return as_bool(auto, False)
    return str(cfg_get(cfg, "share.mode", "off") or "off").strip().lower() == "auto"


def share_manual_enabled(cfg: Mapping[str, Any] | Any) -> bool:
    return share_config_enabled(cfg)


def format_share_quota(quota: dict[str, Any], limits: Limits) -> str:
    normalize(quota)
    return f"{quota.get('shares_day', 0)}/{limits.share_max_per_day} 条/天"


def hour_of(now: datetime) -> str:
    return _hour_key(now)


def write_cooldown_left(quota: Mapping[str, Any], limits: Limits, now: datetime | None = None) -> int:
    now = now or now_local()
    last = parse_iso(quota.get("last_write"))
    if not last:
        return 0
    return max(0, limits.write_cooldown_sec - int((now - last).total_seconds()))


def minutes_until_next_hour(now: datetime | None = None) -> int:
    now = now or now_local()
    return 60 - now.minute

