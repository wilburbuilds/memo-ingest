"""SQLite index of processed audio and in-progress stability observations.

A file is marked processed only after its note is on disk. If the process
dies between those two steps, the next run adopts the existing note instead
of writing a second one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from memo_ingest.gate import Observation


def _iso(now: datetime | None = None) -> str:
    return (now or datetime.now().astimezone()).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init()

    def close(self) -> None:
        self.conn.close()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed (
                sha256 TEXT PRIMARY KEY,
                original_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                note_path TEXT NOT NULL,
                model TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                duration_sec REAL,
                language TEXT
            );
            CREATE TABLE IF NOT EXISTS seen_files (
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ignore_notes (
                note_path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                ignored_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                files_seen INTEGER DEFAULT 0,
                files_waiting INTEGER DEFAULT 0,
                files_processed INTEGER DEFAULT 0,
                files_failed INTEGER DEFAULT 0,
                error TEXT
            );
            """
        )
        self.conn.commit()

    def get_observation(self, path: Path | str) -> Observation | None:
        row = self.conn.execute(
            "SELECT size, mtime_ns, first_seen FROM observations WHERE path = ?",
            (str(path),),
        ).fetchone()
        if row is None:
            return None
        return Observation(row["size"], row["mtime_ns"], row["first_seen"])

    def upsert_observation(self, path: Path | str, observation: Observation, now: float) -> None:
        self.conn.execute(
            """
            INSERT INTO observations (path, size, mtime_ns, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                first_seen = excluded.first_seen,
                last_seen = excluded.last_seen
            """,
            (str(path), observation.size, observation.mtime_ns, observation.first_seen, now),
        )
        self.conn.commit()

    def delete_observation(self, path: Path | str) -> None:
        self.conn.execute("DELETE FROM observations WHERE path = ?", (str(path),))
        self.conn.commit()

    def observation_paths(self) -> list[str]:
        rows = self.conn.execute("SELECT path FROM observations").fetchall()
        return [row["path"] for row in rows]

    def observation_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()
        return int(row["n"])

    def seen(self, path: Path | str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT path, sha256, size, mtime_ns FROM seen_files WHERE path = ?",
            (str(path),),
        ).fetchone()

    def is_processed(self, sha256: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed WHERE sha256 = ?",
            (sha256,),
        ).fetchone()
        return row is not None

    def processed_row(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM processed WHERE sha256 = ?",
            (sha256,),
        ).fetchone()

    def processed_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM processed").fetchone()
        return int(row["n"])

    def mark_processed(
        self,
        *,
        sha256: str,
        original_path: str,
        size: int,
        mtime_ns: int,
        note_path: str,
        model: str,
        duration_sec: float | None,
        language: str,
    ) -> None:
        now = _iso()
        self.conn.execute(
            """
            INSERT INTO processed (
                sha256, original_path, size, mtime_ns, note_path, model,
                processed_at, duration_sec, language
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha256,
                original_path,
                size,
                mtime_ns,
                note_path,
                model,
                now,
                duration_sec,
                language,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO seen_files (path, sha256, size, mtime_ns)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                sha256 = excluded.sha256,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns
            """,
            (original_path, sha256, size, mtime_ns),
        )
        self.conn.commit()

    def remember_seen(self, path: str, sha256: str, size: int, mtime_ns: int) -> None:
        self.conn.execute(
            """
            INSERT INTO seen_files (path, sha256, size, mtime_ns)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                sha256 = excluded.sha256,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns
            """,
            (path, sha256, size, mtime_ns),
        )
        self.conn.commit()

    def forget_sha(self, sha256: str) -> None:
        self.conn.execute("DELETE FROM processed WHERE sha256 = ?", (sha256,))
        self.conn.execute("DELETE FROM seen_files WHERE sha256 = ?", (sha256,))
        self.conn.commit()

    def ignore_note(self, note_path: str, sha256: str) -> None:
        self.conn.execute(
            """
            INSERT INTO ignore_notes (note_path, sha256, ignored_at)
            VALUES (?, ?, ?)
            ON CONFLICT(note_path) DO UPDATE SET
                sha256 = excluded.sha256,
                ignored_at = excluded.ignored_at
            """,
            (note_path, sha256, _iso()),
        )
        self.conn.commit()

    def ignored_note_paths(self) -> set[str]:
        rows = self.conn.execute("SELECT note_path FROM ignore_notes").fetchall()
        return {row["note_path"] for row in rows}

    def start_run(self) -> int:
        now = _iso()
        self.conn.execute(
            """
            UPDATE runs
            SET status = 'crashed',
                finished_at = ?,
                error = COALESCE(error, 'process exited before finishing')
            WHERE status = 'running'
            """,
            (now,),
        )
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
            (now,),
        )
        self.conn.execute(
            """
            DELETE FROM runs
            WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 200)
            """
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        files_seen: int = 0,
        files_waiting: int = 0,
        files_processed: int = 0,
        files_failed: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, files_seen = ?, files_waiting = ?,
                files_processed = ?, files_failed = ?, error = ?
            WHERE id = ?
            """,
            (
                _iso(),
                status,
                files_seen,
                files_waiting,
                files_processed,
                files_failed,
                error,
                run_id,
            ),
        )
        self.conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def last_error(self) -> str | None:
        row = self.conn.execute(
            """
            SELECT error FROM runs
            WHERE error IS NOT NULL AND error != ''
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return row["error"]
