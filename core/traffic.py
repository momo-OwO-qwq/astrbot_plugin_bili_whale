"""请求保护层：节流、预算、风控熔断。

B 站的 412 是 IP 级限流，换接口救不了。所以这里做三件事：
1. 两次请求之间至少隔 N 毫秒（默认 1500ms，写死的底线 300ms）；
2. 读/写按小时、按天记账，超了直接不发；
3. 撞到 412 / -352 / -799 等码就熔断 5/10/20/30 分钟，期间不发请求。

注意：``before_read`` / ``before_write`` 会**先记账再请求**。
失败也算一次，宁可少刷，不要因为失败重试把额度刷爆。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Mapping

from . import quota as quota_mod
from .errors import BiliRisk, BudgetError
from .store import Store
from .util import as_int, cfg_get

ConfigGetter = Callable[[], Mapping[str, Any] | Any]


class TrafficGuard:
    def __init__(
        self,
        store: Store,
        config_getter: ConfigGetter,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Any] = asyncio.sleep,
    ):
        self.store = store
        self.config_getter = config_getter
        self.clock = clock
        self.sleeper = sleeper
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    # ── 配置 ─────────────────────────────────────────────────────────────
    @property
    def cfg(self) -> Mapping[str, Any] | Any:
        return self.config_getter()

    @property
    def limits(self) -> quota_mod.Limits:
        return quota_mod.Limits.from_config(self.cfg)

    @property
    def quota(self) -> dict[str, Any]:
        return self.store.quota

    def interval_sec(self) -> float:
        # 界面上允许调大；但不允许调到 300ms 以下（连击是最容易被判机器人的特征）。
        ms = as_int(cfg_get(self.cfg, "risk.request_interval_ms", 1500), 1500, 300, 60_000)
        return ms / 1000.0

    # ── 状态 ─────────────────────────────────────────────────────────────
    def ban_seconds(self) -> int:
        return quota_mod.ban_remaining_sec(self.quota)

    def is_banned(self) -> bool:
        return self.ban_seconds() > 0

    def ban_text(self) -> str:
        left = self.ban_seconds()
        if not left:
            return ""
        return f"平台要求暂停：{(left + 59) // 60} 分钟后自动恢复"

    def status_text(self) -> str:
        return quota_mod.status_text(self.quota, self.limits)

    def _save(self) -> None:
        try:
            self.store.save()
        except OSError:
            pass

    # ── 节流 ─────────────────────────────────────────────────────────────
    async def _pace(self) -> None:
        async with self._lock:
            wait = self.interval_sec() - (self.clock() - self._last_request)
            if wait > 0:
                await self.sleeper(wait)
            self._last_request = self.clock()

    # ── 读 ───────────────────────────────────────────────────────────────
    async def before_read(self) -> None:
        decision = quota_mod.can_read(self.quota, self.limits)
        if not decision:
            if self.is_banned():
                raise BiliRisk(decision.reason)
            raise BudgetError(decision.reason)
        await self._pace()
        quota_mod.record_read(self.quota)
        self._save()

    # ── 写 ───────────────────────────────────────────────────────────────
    async def before_write(self) -> None:
        decision = quota_mod.can_write(self.quota, self.limits)
        if not decision:
            if self.is_banned():
                raise BiliRisk(decision.reason)
            raise BudgetError(decision.reason)
        await self._pace()
        quota_mod.record_write(self.quota)
        self._save()

    # ── 风控 ─────────────────────────────────────────────────────────────
    def hit_risk(self, code: int | str, message: str = "") -> int:
        seconds = quota_mod.hit_risk(self.quota, code, message)
        self._save()
        return seconds

    def note_success(self) -> None:
        quota_mod.note_success(self.quota)
        self._save()

    def status(self) -> dict[str, Any]:
        limits = self.limits
        quota_mod.normalize(self.quota)
        return {
            "banned": self.is_banned(),
            "ban_text": self.ban_text(),
            "reads_hour": self.quota.get("reads_hour", 0),
            "reads_day": self.quota.get("reads_day", 0),
            "writes_hour": self.quota.get("writes_hour", 0),
            "writes_day": self.quota.get("writes_day", 0),
            "shares_day": self.quota.get("shares_day", 0),
            "limits": limits,
        }
