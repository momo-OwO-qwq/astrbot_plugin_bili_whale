"""生物钟：什么时候醒、今天想不想看、醒来后算不算"用掉一次"。

对应 dsh-bili-whale 的 night-plan：
- 只在用户设定的作息窗口里随机一个时刻醒来（不是整点）；
- 每次唤醒之间至少隔 N 分钟；
- 有 30% 概率"今天不想看"——但它仍然会写日记、报备；
- 计划落盘，重启后不重抽；一天最多醒 N 次。

这里不依赖 astrbot / 网络，random 可注入，方便测试。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from .util import as_float, as_int, iso, now_local, parse_iso, today_str

RandomFn = Callable[[], float]


@dataclass(frozen=True)
class Window:
    start: int  # 从 00:00 起的分钟数
    end: int  # 可能大于 1440（跨午夜）
    label: str


@dataclass(frozen=True)
class Occurrence:
    key: str
    label: str
    start: datetime
    end: datetime
    day: date


def parse_windows(spec: str | list[str] | None) -> list[Window]:
    """``"12:00-14:00,18:00-24:00"`` -> [Window, Window]。"""
    if isinstance(spec, (list, tuple)):
        raw = ",".join(str(x) for x in spec)
    else:
        raw = str(spec or "")
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if not parts:
        parts = ["20:00-24:00"]
    windows: list[Window] = []
    for part in parts:
        if "-" not in part:
            raise ValueError(f"作息窗口要写成 HH:MM-HH:MM：{part}")
        left, right = (x.strip() for x in part.split("-", 1))
        start = _parse_clock(left)
        end = _parse_clock(right)
        if end <= start:
            end += 1440  # 跨午夜
        windows.append(Window(start=start, end=end, label=f"{left}-{right}"))
    return windows


def _parse_clock(value: str) -> int:
    bits = value.split(":")
    if len(bits) != 2 or not all(b.strip().isdigit() for b in bits):
        raise ValueError(f"无效时间：{value}")
    hour, minute = int(bits[0]), int(bits[1])
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        raise ValueError(f"无效时间：{value}")
    return hour * 60 + minute


def occurrences(spec: str | list[str] | None, now: datetime | None = None) -> list[Occurrence]:
    """今天仍然可能用到的窗口实例（含昨天开始、跨到今天的）。"""
    now = now or now_local()
    out: dict[str, Occurrence] = {}
    for window in parse_windows(spec):
        for delta in (0, -1):
            day = (now + timedelta(days=delta)).date()
            midnight = datetime.combine(day, time.min)
            start = midnight + timedelta(minutes=window.start)
            end = midnight + timedelta(minutes=window.end)
            if end <= now:
                continue
            key = f"{day.isoformat()}|{window.label}"
            out[key] = Occurrence(key=key, label=window.label, start=start, end=end, day=day)
    return sorted(out.values(), key=lambda o: (o.end, o.start))


def normalize_history(state: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or now_local()
    today = today_str(now)
    history = state.get("history")
    if not isinstance(history, dict):
        history = {}
    if history.get("date") != today:
        history = {
            "date": today,
            "wakes": 0,
            "used": {},
            # 跨午夜时仍然要守住"两次唤醒之间的最小间隔"，所以 last_woke 保留。
            "last_woke": history.get("last_woke", ""),
        }
    used = history.get("used")
    if not isinstance(used, dict):
        used = {}
    history["used"] = used
    last = parse_iso(history.get("last_woke"))
    if last and (now - last) > timedelta(days=3):
        history["last_woke"] = ""
    return history


def is_pending(plan: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not isinstance(plan, dict) or plan.get("fired"):
        return False
    at = parse_iso(plan.get("at"))
    end = parse_iso(plan.get("ends_at"))
    now = now or now_local()
    return bool(at and end and end > now)


def is_due(plan: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not isinstance(plan, dict) or plan.get("fired"):
        return False
    at = parse_iso(plan.get("at"))
    now = now or now_local()
    return bool(at and at <= now)


def is_expired(plan: dict[str, Any] | None, now: datetime | None = None, grace_min: int = 90) -> bool:
    """计划时间过了太久（比如插件当时没运行）——别再补跑，免得半夜突然发言。

    两个上限取更早的那个：
    - 计划时刻 + grace_min；
    - 窗口结束 + 30 分钟（避免 23:50 的计划拖到凌晨 1 点才发评论）。
    """
    if not isinstance(plan, dict) or plan.get("fired"):
        return False
    at = parse_iso(plan.get("at"))
    end = parse_iso(plan.get("ends_at"))
    now = now or now_local()
    if not at:
        return False
    hard = at + timedelta(minutes=grace_min)
    if end:
        hard = min(hard, end + timedelta(minutes=30))
    return now > hard


def make_plan(
    state: dict[str, Any],
    spec: str | list[str] | None,
    wakes_per_day: int = 1,
    skip_chance: float = 0.3,
    min_gap_min: int = 10,
    now: datetime | None = None,
    rng: RandomFn = random.random,
) -> dict[str, Any]:
    """返回并写入 ``state["plan"]``。

    action:
      - ``wake``：到点刷一轮；
      - ``skip``：到点不看，但写日记；
      - ``done``：今天没有可用窗口 / 次数已用完。
    """
    now = now or now_local()
    history = normalize_history(state, now)
    state["history"] = history
    current = state.get("plan")
    if is_pending(current, now):
        return current  # 计划落盘后不重抽

    wakes_per_day = as_int(wakes_per_day, 1, 0, 12)
    if history.get("wakes", 0) >= wakes_per_day or wakes_per_day == 0:
        plan = {
            "action": "done",
            "reason": "今天醒来的次数已用完" if wakes_per_day else "作息窗口次数设为 0",
            "date": today_str(now),
            "fired": True,
            "woke": False,
        }
        state["plan"] = plan
        return plan

    gap = max(60, as_int(min_gap_min, 10, 1, 720) * 60)
    last_woke = parse_iso(history.get("last_woke"))
    pool = [occ for occ in occurrences(spec, now) if occ.end > now + timedelta(seconds=60)]
    if not pool:
        plan = {
            "action": "done",
            "reason": "今天的作息窗口已经结束（或还没开始）",
            "date": today_str(now),
            "fired": True,
            "woke": False,
        }
        state["plan"] = plan
        return plan

    for occ in pool:
        earliest = now + timedelta(seconds=60)
        if occ.start > earliest:
            earliest = occ.start
        if last_woke and last_woke + timedelta(seconds=gap) > earliest:
            earliest = last_woke + timedelta(seconds=gap)
        latest = occ.end - timedelta(seconds=60)
        if earliest >= latest:
            continue
        span = (latest - earliest).total_seconds()
        at = earliest + timedelta(seconds=max(0.0, min(span, rng() * span)))
        skip = rng() < as_float(skip_chance, 0.3, 0.0, 1.0)
        plan = {
            "action": "skip" if skip else "wake",
            "at": iso(at),
            "ends_at": iso(occ.end),
            "date": today_str(now),
            "window": occ.label,
            "window_key": occ.key,
            "fired": False,
            "woke": False,
            "roll": "skip" if skip else "watch",
            "created_at": iso(now),
        }
        state["plan"] = plan
        return plan

    plan = {
        "action": "done",
        "reason": "窗口里排不下下一次（两次之间至少隔 %d 分钟）" % (gap // 60),
        "date": today_str(now),
        "fired": True,
        "woke": False,
    }
    state["plan"] = plan
    return plan


def complete_plan(
    state: dict[str, Any],
    ran: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """标记计划已处理；``skip`` 也会消耗一次唤醒名额（和 whale 一致）。"""
    now = now or now_local()
    plan = state.get("plan") or {}
    if plan.get("fired"):
        return plan
    history = normalize_history(state, now)
    if plan.get("action") in {"wake", "skip"}:
        history["wakes"] = int(history.get("wakes", 0)) + 1
        key = plan.get("window_key") or plan.get("window") or "unknown"
        history.setdefault("used", {})[key] = int(history["used"].get(key, 0)) + 1
        history["last_woke"] = iso(now)
    history["date"] = today_str(now)
    state["history"] = history
    plan = dict(plan)
    plan["fired"] = True
    plan["woke"] = bool(ran and plan.get("action") == "wake")
    plan["finished_at"] = iso(now)
    state["plan"] = plan
    return plan


def plan_status_text(state: dict[str, Any], spec: str, wakes_per_day: int, now: datetime | None = None) -> str:
    now = now or now_local()
    history = normalize_history(state, now)
    plan = state.get("plan") or {}
    lines = [f"作息：{spec} · 每天最多醒 {wakes_per_day} 次"]
    lines.append(f"今天已醒：{history.get('wakes', 0)}/{wakes_per_day}")
    if plan.get("action") == "done" and plan.get("reason"):
        lines.append(f"今日计划：{plan.get('reason')}")
    elif plan.get("fired"):
        lines.append(f"今日计划：已处理（{plan.get('action')}）")
    elif plan.get("action") in {"wake", "skip"}:
        at = parse_iso(plan.get("at"))
        kind = "刷一轮" if plan.get("action") == "wake" else "今天不想看（仍写日记）"
        if at:
            lines.append(f"下一次：{at.strftime('%m-%d %H:%M')} · {kind} · 窗口 {plan.get('window', '')}")
    elif plan.get("action") == "done":
        lines.append(f"今天：{plan.get('reason', '没有安排')}")
    else:
        lines.append("下一次：还没排（等下一个窗口）")
    return "\n".join(lines)


def next_wake_at(state: dict[str, Any]) -> datetime | None:
    plan = state.get("plan") or {}
    if plan.get("fired") or plan.get("action") not in {"wake", "skip"}:
        return None
    return parse_iso(plan.get("at"))
