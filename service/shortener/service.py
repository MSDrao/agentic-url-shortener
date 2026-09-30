"""Business logic. Framework-free so it can be unit tested without HTTP."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .codes import generate_code, validate_alias
from .config import Settings
from .errors import CodeSpaceExhausted, Conflict, Gone, InvalidInput, NotFound
from .models import Link, LinkStats
from .repository import LinkRepository
from .validation import validate_target_url

MAX_HEADER_LEN = 512


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _truncate(value: str | None) -> str | None:
    return value[:MAX_HEADER_LEN] if value else None


class LinkService:
    def __init__(
        self,
        repo: LinkRepository,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.repo = repo
        self.settings = settings
        self.clock = clock

    def create_link(
        self,
        url: str,
        custom_alias: str | None = None,
        expires_at: datetime | None = None,
    ) -> Link:
        target = validate_target_url(url, self.settings)
        now = self.clock()
        expiry = _as_utc(expires_at) if expires_at else None
        if expiry is not None and expiry <= now:
            raise InvalidInput("expires_at must be in the future")

        if custom_alias is not None:
            try:
                code = validate_alias(custom_alias, self.settings.extra_reserved_codes)
            except ValueError as exc:
                raise InvalidInput(str(exc)) from exc
            return self.repo.create(code, target, now, expiry)

        for _ in range(self.settings.max_code_attempts):
            code = generate_code(self.settings.code_length)
            try:
                return self.repo.create(code, target, now, expiry)
            except Conflict:
                continue  # random collision: retry with a fresh code
        raise CodeSpaceExhausted("could not allocate a unique code; retry later")

    def get_link(self, code: str) -> Link:
        link = self.repo.get_by_code(code)
        if link is None or not link.is_active:
            raise NotFound(f"link '{code}' not found")
        return link

    def resolve(
        self, code: str, referrer: str | None = None, user_agent: str | None = None
    ) -> Link:
        """Look up a link for redirect and record the click."""
        link = self.get_link(code)
        now = self.clock()
        if link.is_expired(now):
            raise Gone(f"link '{code}' has expired")
        self.repo.record_click(link.id, now, _truncate(referrer), _truncate(user_agent))
        return link

    def get_stats(self, code: str) -> tuple[Link, LinkStats]:
        link = self.get_link(code)
        return link, self.repo.stats(link.id)

    def delete_link(self, code: str) -> None:
        if not self.repo.deactivate(code):
            raise NotFound(f"link '{code}' not found")
