"""Unit tests for pure components: codes, URL validation, limiter, service, migrations."""

from __future__ import annotations

import pytest

from shortener import codes
from shortener.config import Settings
from shortener.db import MIGRATIONS, Database
from shortener.errors import CodeSpaceExhausted, Conflict, InvalidInput
from shortener.ratelimit import TokenBucketLimiter
from shortener.validation import validate_target_url

SETTINGS = Settings(base_url="https://sho.rt")


# --- codes -----------------------------------------------------------------

def test_generate_code_length_and_alphabet():
    code = codes.generate_code(9)
    assert len(code) == 9 and set(code) <= set(codes.ALPHABET)


def test_generate_code_rejects_short_length():
    with pytest.raises(ValueError):
        codes.generate_code(3)


@pytest.mark.parametrize("alias", ["ab", "has space", "x" * 33, "emoji😀", "../etc"])
def test_invalid_aliases(alias):
    with pytest.raises(ValueError):
        codes.validate_alias(alias)


def test_valid_alias_passthrough():
    assert codes.validate_alias("Promo_2026-q1") == "Promo_2026-q1"


# --- URL validation --------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com:8080/path?q=1#frag",
        "https://sub.domain.example.org/a/b",
        "https://93.184.216.34/",
    ],
)
def test_valid_urls(url):
    assert validate_target_url(url, SETTINGS) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "https://",
        "https://user:pw@bank.example.com",
        "https://bank.example.com@evil.test",
        "http://localhost/admin",
        "http://app.localhost/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "https://sho.rt/abc",
        "https://example.com/has space",
        "https://example.com:99999/",
    ],
)
def test_rejected_urls(url):
    with pytest.raises(InvalidInput):
        validate_target_url(url, SETTINGS)


def test_url_length_limit():
    with pytest.raises(InvalidInput):
        validate_target_url("https://example.com/" + "a" * 2100, SETTINGS)


def test_private_targets_allowed_when_configured():
    s = Settings(allow_private_targets=True)
    assert validate_target_url("http://10.0.0.5/", s) == "http://10.0.0.5/"


# --- rate limiter ----------------------------------------------------------

def test_token_bucket_refills_over_time():
    t = [0.0]
    limiter = TokenBucketLimiter(rate_per_minute=60, burst=2, clock=lambda: t[0])
    assert limiter.allow("a")[0] and limiter.allow("a")[0]
    allowed, retry_after = limiter.allow("a")
    assert not allowed and retry_after == pytest.approx(1.0)
    assert limiter.allow("b")[0]  # keys are independent
    t[0] += 1.0
    assert limiter.allow("a")[0]


def test_token_bucket_bounds_memory():
    limiter = TokenBucketLimiter(rate_per_minute=10, max_keys=3)
    for key in "abcdef":
        limiter.allow(key)
    assert len(limiter._buckets) == 3


# --- service ---------------------------------------------------------------

def test_collision_retries_then_succeeds(service, monkeypatch):
    service.create_link("https://example.com", custom_alias="AAAAAAA")
    seq = iter(["AAAAAAA", "AAAAAAA", "BBBBBBB"])
    monkeypatch.setattr("shortener.service.generate_code", lambda n: next(seq))
    assert service.create_link("https://example.com").code == "BBBBBBB"


def test_collision_exhaustion_raises(service, monkeypatch):
    service.create_link("https://example.com", custom_alias="AAAAAAA")
    monkeypatch.setattr("shortener.service.generate_code", lambda n: "AAAAAAA")
    with pytest.raises(CodeSpaceExhausted):
        service.create_link("https://example.com")


def test_repository_duplicate_code_raises_conflict(repo, clock):
    repo.create("dup1234", "https://a.test", clock.now, None)
    with pytest.raises(Conflict):
        repo.create("dup1234", "https://b.test", clock.now, None)


def test_long_headers_are_truncated(service, repo):
    link = service.create_link("https://example.com")
    service.resolve(link.code, referrer="r" * 5000, user_agent="u" * 5000)
    stats = repo.stats(link.id)
    assert len(stats.top_referrers[0][0]) == 512


# --- migrations ------------------------------------------------------------

def test_migrations_are_idempotent_and_versioned(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    latest = max(v for v, _ in MIGRATIONS)
    assert db.migrate() == latest
    assert db.migrate() == latest  # re-running is a no-op
    assert db.schema_version() == latest


def test_migration_versions_strictly_increase():
    versions = [v for v, _ in MIGRATIONS]
    assert versions == sorted(set(versions))


# --- structured logging ----------------------------------------------------

def test_json_log_formatter_includes_request_fields(tmp_path, monkeypatch):
    import importlib
    import json
    import logging

    monkeypatch.setenv("SHORTENER_DB_PATH", str(tmp_path / "main.db"))
    main = importlib.import_module("shortener.main")
    record = logging.LogRecord("shortener", logging.INFO, __file__, 1, "request", None, None)
    record.request_id, record.status = "abc", 201
    out = json.loads(main.JsonFormatter().format(record))
    assert out["msg"] == "request" and out["request_id"] == "abc" and out["status"] == 201
