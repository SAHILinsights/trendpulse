"""YouTube Data API quota accounting.

The default project quota is 10,000 units per day, reset at midnight
Pacific Time. Costs used here follow the published quota table:
``videos.list`` = 1, ``search.list`` = 100, ``videoCategories.list`` = 1
(per call, independent of ``maxResults``).

The tracker is seeded at run start with the units already spent today
(summed from ``api_request_log``), so successive runs share one daily
budget. ``search.list`` may never eat into ``refresh_reserve`` — cheap metric
refreshes always have room.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from trendpulse.settings import QuotaCfg
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse

COSTS: dict[str, int] = {
    "videos.list": 1,
    "search.list": 100,
    "videoCategories.list": 1,
}


class QuotaBudgetExceeded(RuntimeError):
    """Raised before a request that would exceed the configured daily budget."""


def quota_day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """Return [start, end) of the quota day containing ``now`` as UTC datetimes."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    start_local = datetime.combine(local.date(), time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def units_used_today(wh: Warehouse, now: datetime, tz: str) -> int:
    start, end = quota_day_bounds(now, tz)
    t = s.api_request_log
    v = wh.scalar(select(func.coalesce(func.sum(t.c.quota_cost), 0))
                  .where(t.c.requested_at >= start, t.c.requested_at < end))
    return int(v or 0)


@dataclass
class QuotaTracker:
    cfg: QuotaCfg
    used_before_run: int = 0
    used_this_run: int = 0
    by_endpoint: dict[str, int] = field(default_factory=dict)

    @property
    def used_today(self) -> int:
        return self.used_before_run + self.used_this_run

    @property
    def remaining(self) -> int:
        return max(0, self.cfg.daily_budget - self.used_today)

    def can_spend(self, endpoint: str, calls: int = 1) -> bool:
        cost = COSTS[endpoint] * calls
        ceiling = self.cfg.daily_budget
        if endpoint == "search.list":
            ceiling -= self.cfg.refresh_reserve
        return self.used_today + cost <= ceiling

    def reserve(self, endpoint: str) -> int:
        if not self.can_spend(endpoint):
            raise QuotaBudgetExceeded(
                f"{endpoint} would exceed daily budget (used {self.used_today}/{self.cfg.daily_budget})")
        return COSTS[endpoint]

    def record(self, endpoint: str, cost: int) -> None:
        self.used_this_run += cost
        self.by_endpoint[endpoint] = self.by_endpoint.get(endpoint, 0) + cost
