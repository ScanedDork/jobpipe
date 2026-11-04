"""Application tracker: one SQLite table, exportable to CSV."""

from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

COLUMNS = ("id", "date", "company", "role", "url", "status", "resume_path", "cover_path",
           "ats_score", "notes")
STATUSES = ("prepared", "applied", "interview", "offer", "rejected", "withdrawn")
DEFAULT_STATUS = "prepared"  # documents generated; you still have to send them

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    company TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'prepared',
    resume_path TEXT NOT NULL DEFAULT '',
    cover_path TEXT NOT NULL DEFAULT '',
    ats_score INTEGER,
    notes TEXT NOT NULL DEFAULT ''
)
"""


class TrackerError(ValueError):
    pass


class Tracker:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, *, company: str, role: str = "", url: str = "",
            status: str = DEFAULT_STATUS, resume_path: str = "", cover_path: str = "",
            ats_score: int | None = None, notes: str = "", day: date | None = None) -> int:
        _check_status(status)
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO applications (date, company, role, url, status, resume_path, "
                "cover_path, ats_score, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((day or date.today()).isoformat(), company, role, url, status,
                 str(resume_path), str(cover_path), ats_score, notes),
            )
            return int(cur.lastrowid)

    def all(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM applications ORDER BY id")
            return [dict(row) for row in rows]

    def get(self, app_id: int) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM applications WHERE id = ?",
                               (app_id,)).fetchone()
        if row is None:
            raise TrackerError(f"No application with id {app_id}")
        return dict(row)

    def set_status(self, app_id: int, status: str) -> None:
        status = status.strip().lower()
        _check_status(status)
        with closing(self._connect()) as conn, conn:
            cur = conn.execute("UPDATE applications SET status = ? WHERE id = ?",
                               (status, app_id))
        if cur.rowcount == 0:
            raise TrackerError(f"No application with id {app_id}")

    def export_csv(self, path: str | Path) -> int:
        rows = self.all()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise TrackerError(f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}")
