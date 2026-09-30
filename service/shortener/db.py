"""SQLite access with forward-only, versioned migrations.

Migrations are append-only: never edit an applied migration, add a new one.
Each migration runs in its own transaction and records its version, so a
failed migration leaves the schema at the previous version.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT    NOT NULL UNIQUE,
                target_url  TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                expires_at  TEXT,
                is_active   INTEGER NOT NULL DEFAULT 1
            )
            """,
            """
            CREATE TABLE clicks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id     INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
                clicked_at  TEXT    NOT NULL,
                referrer    TEXT,
                user_agent  TEXT
            )
            """,
            "CREATE INDEX idx_clicks_link_time ON clicks(link_id, clicked_at)",
        ],
    ),
]


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()  # serialises writers; SQLite allows one writer

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def schema_version(self) -> int:
        with self.read() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            return int(row["v"] or 0)

    def migrate(self) -> int:
        with self.write() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
        if self.path != ":memory:":
            with self.write() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
        current = self.schema_version()
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            with self.write() as conn:
                for sql in statements:
                    conn.execute(sql)
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            current = version
        return current

    def ping(self) -> bool:
        with self.read() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
