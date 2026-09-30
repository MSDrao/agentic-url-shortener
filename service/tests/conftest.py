from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.config import Settings
from shortener.db import Database
from shortener.repository import LinkRepository
from shortener.service import LinkService


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "test.db"),
        base_url="https://sho.rt",
        rate_limit_per_minute=1000,
    )


@pytest.fixture
def db(settings) -> Database:
    database = Database(settings.database_path)
    database.migrate()
    return database


@pytest.fixture
def repo(db) -> LinkRepository:
    return LinkRepository(db)


@pytest.fixture
def service(repo, settings, clock) -> LinkService:
    return LinkService(repo, settings, clock=clock)


@pytest.fixture
def client(settings, service) -> TestClient:
    return TestClient(create_app(settings, service=service), follow_redirects=False)
