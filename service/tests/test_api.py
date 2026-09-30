"""Integration tests through the HTTP layer (real SQLite, fake clock)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.config import Settings


def _create(client, **body):
    body.setdefault("url", "https://example.com/some/page?x=1")
    return client.post("/api/v1/links", json=body)


def test_health_and_readiness(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json() == {"status": "ready"}


def test_create_link_returns_201_with_short_url(client):
    resp = _create(client)
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["code"]) == 7
    assert data["short_url"] == f"https://sho.rt/{data['code']}"
    assert data["target_url"] == "https://example.com/some/page?x=1"
    assert data["is_active"] is True
    assert resp.headers["Location"] == f"/api/v1/links/{data['code']}"
    assert "X-Request-ID" in resp.headers


def test_redirect_is_307_and_counts_click(client):
    code = _create(client).json()["code"]
    resp = client.get(f"/{code}", headers={"Referer": "https://news.test/"})
    assert resp.status_code == 307
    assert resp.headers["location"] == "https://example.com/some/page?x=1"
    assert "max-age=0" in resp.headers["cache-control"]
    stats = client.get(f"/api/v1/links/{code}/stats").json()
    assert stats["total_clicks"] == 1
    assert stats["top_referrers"] == [{"referrer": "https://news.test/", "clicks": 1}]
    assert stats["clicks_by_day"] == [{"day": "2026-01-15", "clicks": 1}]


def test_stats_aggregate_by_day_and_referrer(client, clock):
    code = _create(client).json()["code"]
    client.get(f"/{code}")
    client.get(f"/{code}", headers={"Referer": "https://a.test/"})
    clock.advance(days=1)
    client.get(f"/{code}", headers={"Referer": "https://a.test/"})
    stats = client.get(f"/api/v1/links/{code}/stats").json()
    assert stats["total_clicks"] == 3
    assert stats["clicks_by_day"] == [
        {"day": "2026-01-15", "clicks": 2},
        {"day": "2026-01-16", "clicks": 1},
    ]
    assert stats["top_referrers"][0] == {"referrer": "https://a.test/", "clicks": 2}


def test_custom_alias_and_conflict(client):
    assert _create(client, custom_alias="launch-2026").status_code == 201
    dup = _create(client, custom_alias="launch-2026")
    assert dup.status_code == 409
    assert "already in use" in dup.json()["detail"]


def test_reserved_alias_rejected_case_insensitively(client):
    for alias in ("api", "API", "Healthz"):
        assert _create(client, custom_alias=alias).status_code == 422


def test_invalid_url_rejected(client):
    for url in ("javascript:alert(1)", "ftp://x.test/file", "http://127.0.0.1/admin", ""):
        assert _create(client, url=url).status_code == 422, url


def test_unknown_fields_rejected(client):
    assert _create(client, owner="someone").status_code == 422


def test_unknown_code_is_404(client):
    resp = client.get("/nope123")
    assert resp.status_code == 404
    assert resp.json()["request_id"]


def test_expired_link_returns_410_and_does_not_count(client, clock):
    expiry = (clock.now + timedelta(hours=1)).isoformat()
    code = _create(client, expires_at=expiry).json()["code"]
    assert client.get(f"/{code}").status_code == 307
    clock.advance(hours=2)
    assert client.get(f"/{code}").status_code == 410
    assert client.get(f"/api/v1/links/{code}/stats").json()["total_clicks"] == 1


def test_expiry_in_past_rejected(client, clock):
    past = (clock.now - timedelta(minutes=1)).isoformat()
    assert _create(client, expires_at=past).status_code == 422


def test_delete_deactivates_link(client):
    code = _create(client).json()["code"]
    assert client.delete(f"/api/v1/links/{code}").status_code == 204
    assert client.get(f"/{code}").status_code == 404
    assert client.delete(f"/api/v1/links/{code}").status_code == 404


def test_api_key_enforced_on_writes_when_configured(tmp_path):
    settings = Settings(database_path=str(tmp_path / "k.db"), api_key="s3cret")
    client = TestClient(create_app(settings), follow_redirects=False)
    body = {"url": "https://example.com"}
    assert client.post("/api/v1/links", json=body).status_code == 401
    assert client.post("/api/v1/links", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    created = client.post("/api/v1/links", json=body, headers={"X-API-Key": "s3cret"})
    assert created.status_code == 201
    code = created.json()["code"]
    # reads and redirects stay public
    assert client.get(f"/api/v1/links/{code}").status_code == 200
    assert client.delete(f"/api/v1/links/{code}").status_code == 401


def test_rate_limit_returns_429_with_retry_after(tmp_path):
    settings = Settings(database_path=str(tmp_path / "r.db"), rate_limit_per_minute=2)
    client = TestClient(create_app(settings))
    body = {"url": "https://example.com"}
    assert client.post("/api/v1/links", json=body).status_code == 201
    assert client.post("/api/v1/links", json=body).status_code == 201
    limited = client.post("/api/v1/links", json=body)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_openapi_contract_lists_core_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/api/v1/links", "/api/v1/links/{code}", "/api/v1/links/{code}/stats", "/{code}"):
        assert path in paths
