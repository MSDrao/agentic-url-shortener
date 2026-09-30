"""Short-code generation and custom-alias validation."""

from __future__ import annotations

import re
import secrets
import string

ALPHABET = string.ascii_letters + string.digits  # base62
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

# Paths the service owns; a code must never shadow them.
RESERVED_CODES = frozenset(
    {"api", "healthz", "readyz", "docs", "redoc", "openapi.json", "static", "admin"}
)


def generate_code(length: int) -> str:
    """Cryptographically random base62 code (62^7 ~= 3.5e12 space at length 7)."""
    if length < 4:
        raise ValueError("code length must be >= 4")
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def validate_alias(alias: str, extra_reserved: frozenset[str] = frozenset()) -> str:
    if not ALIAS_RE.fullmatch(alias):
        raise ValueError("alias must be 3-32 chars of letters, digits, '_' or '-'")
    if alias.lower() in RESERVED_CODES or alias.lower() in extra_reserved:
        raise ValueError(f"alias '{alias}' is reserved")
    return alias
