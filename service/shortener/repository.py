"""Persistence for links and click events. All SQL lives here."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .db import Database
from .errors import Conflict
from .models import Link, LinkStats

LINK_COLUMNS = "id, code, target_url, created_at, expires_at, is_active"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_link(row: sqlite3.Row) -> Link:
    return Link(
        id=row["id"],
        code=row["code"],
        target_url=row["target_url"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=_dt(row["expires_at"]),
        is_active=bool(row["is_active"]),
    )


class LinkRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        code: str,
        target_url: str,
        created_at: datetime,
        expires_at: datetime | None,
    ) -> Link:
        try:
            with self.db.write() as conn:
                cur = conn.execute(
                    "INSERT INTO links (code, target_url, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        code,
                        target_url,
                        created_at.isoformat(),
                        expires_at.isoformat() if expires_at else None,
                    ),
                )
                link_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"code '{code}' is already in use") from exc
        link = self.get_by_id(link_id)
        assert link is not None
        return link

    def get_by_id(self, link_id: int) -> Link | None:
        with self.db.read() as conn:
            row = conn.execute(
                f"SELECT {LINK_COLUMNS} FROM links WHERE id = ?", (link_id,)
            ).fetchone()
        return _row_to_link(row) if row else None

    def get_by_code(self, code: str) -> Link | None:
        with self.db.read() as conn:
            row = conn.execute(
                f"SELECT {LINK_COLUMNS} FROM links WHERE code = ?", (code,)
            ).fetchone()
        return _row_to_link(row) if row else None

    def deactivate(self, code: str) -> bool:
        with self.db.write() as conn:
            cur = conn.execute(
                "UPDATE links SET is_active = 0 WHERE code = ? AND is_active = 1", (code,)
            )
            return cur.rowcount > 0

    def record_click(
        self,
        link_id: int,
        clicked_at: datetime,
        referrer: str | None,
        user_agent: str | None,
    ) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO clicks (link_id, clicked_at, referrer, user_agent) "
                "VALUES (?, ?, ?, ?)",
                (link_id, clicked_at.isoformat(), referrer, user_agent),
            )

    def count_clicks(self, link_id: int) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM clicks WHERE link_id = ?", (link_id,)
            ).fetchone()
        return int(row["n"])

    def stats(self, link_id: int, top_n: int = 5) -> LinkStats:
        with self.db.read() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM clicks WHERE link_id = ?", (link_id,)
            ).fetchone()["n"]
            by_day = conn.execute(
                "SELECT substr(clicked_at, 1, 10) AS day, COUNT(*) AS n FROM clicks "
                "WHERE link_id = ? GROUP BY day ORDER BY day",
                (link_id,),
            ).fetchall()
            referrers = conn.execute(
                "SELECT COALESCE(referrer, '(direct)') AS ref, COUNT(*) AS n FROM clicks "
                "WHERE link_id = ? GROUP BY ref ORDER BY n DESC, ref LIMIT ?",
                (link_id, top_n),
            ).fetchall()
        return LinkStats(
            total_clicks=int(total),
            clicks_by_day=[(r["day"], int(r["n"])) for r in by_day],
            top_referrers=[(r["ref"], int(r["n"])) for r in referrers],
        )
