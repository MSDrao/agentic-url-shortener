"""Runtime configuration, read from environment variables (12-factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_path: str = "shortener.db"
    base_url: str = "http://localhost:8000"
    # When set, write operations (create/delete) require header X-API-Key.
    api_key: str | None = None
    code_length: int = 7
    max_code_attempts: int = 5
    max_url_length: int = 2048
    rate_limit_per_minute: int = 60
    # Private/loopback targets are rejected by default (SSRF / internal-network abuse).
    allow_private_targets: bool = False
    extra_reserved_codes: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            database_path=env.get("SHORTENER_DB_PATH", cls.database_path),
            base_url=env.get("SHORTENER_BASE_URL", cls.base_url).rstrip("/"),
            api_key=env.get("SHORTENER_API_KEY") or None,
            code_length=int(env.get("SHORTENER_CODE_LENGTH", cls.code_length)),
            max_url_length=int(env.get("SHORTENER_MAX_URL_LENGTH", cls.max_url_length)),
            rate_limit_per_minute=int(
                env.get("SHORTENER_RATE_LIMIT_PER_MINUTE", cls.rate_limit_per_minute)
            ),
            allow_private_targets=_bool(env.get("SHORTENER_ALLOW_PRIVATE_TARGETS"), False),
        )
