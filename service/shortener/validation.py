"""Target-URL validation.

Guards against: non-web schemes (javascript:, data:, file:), credential-bearing
URLs used for phishing (https://bank.com@evil.test), redirects into private
networks, and redirect loops through the shortener itself.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from .config import Settings
from .errors import InvalidInput

ALLOWED_SCHEMES = {"http", "https"}
LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


# A label that is purely numeric in any base browsers accept (decimal, 0x-hex, 0-octal).
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")


def _is_noncanonical_numeric_host(host: str) -> bool:
    """True for IPv4 spellings browsers resolve but ipaddress rejects, e.g. 127.1,
    2130706433, 0x7f.0.0.1, 0177.0.0.1. Such hosts have no legitimate use and would
    let a private address slip past the private-host check, so they are refused."""
    labels = host.split(".")
    if not all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
        return False
    try:
        return str(ipaddress.IPv4Address(host)) != host
    except ValueError:
        return True


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

    try:
        # urlsplit raises ValueError on malformed authorities (e.g. "https://[invalid"),
        # and .hostname / .port can raise on bad IPv6 literals or ports.
        parts = urlsplit(candidate)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError as exc:
        raise InvalidInput("url is malformed") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidInput("only http and https urls are allowed")
    if not parts.netloc or not hostname:
        raise InvalidInput("url must include a host")
    if "[" in parts.netloc or "]" in parts.netloc:
        # A bracketed host must be a real IPv6 literal. Checked explicitly because older
        # Python releases (e.g. macOS's 3.9.6) do not validate brackets inside urlsplit.
        try:
            ipaddress.IPv6Address(hostname.split("%", 1)[0])
        except ValueError as exc:
            raise InvalidInput("url is malformed") from exc
    if parts.username is not None or parts.password is not None:
        raise InvalidInput("urls with embedded credentials are not allowed")

    host = hostname.lower().rstrip(".")
    if _is_noncanonical_numeric_host(host):
        raise InvalidInput("numeric hosts must be canonical dotted-quad IPv4 addresses")
    if not settings.allow_private_targets and _is_private_host(host):
        raise InvalidInput("urls pointing at private or local addresses are not allowed")

    own_host = (urlsplit(settings.base_url).hostname or "").lower()
    if own_host and host == own_host:
        raise InvalidInput("urls pointing at this shortener are not allowed")

    return candidate
