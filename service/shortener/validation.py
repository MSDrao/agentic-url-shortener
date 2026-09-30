"""Target-URL validation.

Guards against: non-web schemes (javascript:, data:, file:), credential-bearing
URLs used for phishing (https://bank.com@evil.test), redirects into private
networks, and redirect loops through the shortener itself.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from .config import Settings
from .errors import InvalidInput

ALLOWED_SCHEMES = {"http", "https"}
LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def _is_private_host(host: str) -> bool:
    if host in LOCAL_HOSTNAMES or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_target_url(url: str, settings: Settings) -> str:
    candidate = (url or "").strip()
    if not candidate:
        raise InvalidInput("url is required")
    if len(candidate) > settings.max_url_length:
        raise InvalidInput(f"url exceeds {settings.max_url_length} characters")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in candidate):
        raise InvalidInput("url must not contain whitespace or control characters")

    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidInput("only http and https urls are allowed")
    if not parts.netloc or not parts.hostname:
        raise InvalidInput("url must include a host")
    if parts.username is not None or parts.password is not None:
        raise InvalidInput("urls with embedded credentials are not allowed")
    try:
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError as exc:
        raise InvalidInput("url has an invalid port") from exc

    host = parts.hostname.lower().rstrip(".")
    if not settings.allow_private_targets and _is_private_host(host):
        raise InvalidInput("urls pointing at private or local addresses are not allowed")

    own_host = (urlsplit(settings.base_url).hostname or "").lower()
    if own_host and host == own_host:
        raise InvalidInput("urls pointing at this shortener are not allowed")

    return candidate
