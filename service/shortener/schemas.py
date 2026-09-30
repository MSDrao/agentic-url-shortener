"""Public API contract (request/response models). Changes here are API changes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., description="Destination http(s) URL", max_length=4096)
    custom_alias: str | None = Field(
        default=None, description="Optional vanity code: 3-32 chars [A-Za-z0-9_-]"
    )
    expires_at: datetime | None = Field(
        default=None, description="Optional expiry (ISO-8601). Naive times are UTC."
    )


class LinkResponse(BaseModel):
    code: str
    short_url: str
    target_url: str
    created_at: datetime
    expires_at: datetime | None
    is_active: bool


class DayCount(BaseModel):
    day: str
    clicks: int


class ReferrerCount(BaseModel):
    referrer: str
    clicks: int


class StatsResponse(BaseModel):
    code: str
    total_clicks: int
    clicks_by_day: list[DayCount]
    top_referrers: list[ReferrerCount]


class ErrorResponse(BaseModel):
    detail: str
    request_id: str | None = None
