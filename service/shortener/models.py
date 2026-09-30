"""Internal domain models (persistence-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Link:
    id: int
    code: str
    target_url: str
    created_at: datetime
    expires_at: datetime | None
    is_active: bool

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True)
class LinkStats:
    total_clicks: int
    clicks_by_day: list[tuple[str, int]]
    top_referrers: list[tuple[str, int]]
