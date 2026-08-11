from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import VideoFile


ACTIVE_STATUSES = {
    "processing",
    "extracting_audio",
    "separating_vocals",
    "composing_video",
}


def normalized_input_root(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProcessingRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source_path TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    detected_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    model TEXT NOT NULL,
                    device TEXT,
                    input_root TEXT,
                    output_path TEXT NOT NULL,
                    duration_seconds REAL,
                    used_video_copy INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source_path);
                CREATE INDEX IF NOT EXISTS idx_jobs_detected ON jobs(detected_at DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "device" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN device TEXT")
            if "input_root" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN input_root TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_root_status "
                "ON jobs(input_root, status)"
            )

    def recover_interrupted(self, input_root: str | Path | None = None) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        scope = " AND input_root=?" if input_root is not None else ""
        params: tuple[Any, ...] = (
            utcnow(),
            *sorted(ACTIVE_STATUSES),
            *(
                (normalized_input_root(input_root),)
                if input_root is not None
                else ()
            ),
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET status='failed',
                    finished_at=?,
                    error='Previous process stopped before this job completed'
                WHERE status IN ({placeholders}){scope}
                """,
                params,
            )
            return cursor.rowcount

    def get_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def register(
        self,
        video: VideoFile,
        *,
        fingerprint: str,
        model: str,
        output_path: Path,
        status: str,
        input_root: str | Path | None = None,
    ) -> dict[str, Any]:
        root_value = (
            normalized_input_root(input_root)
            if input_root is not None
            else None
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status='superseded',
                    finished_at=?,
                    error='Source file changed before this version was processed'
                WHERE source_path=?
                  AND fingerprint<>?
                  AND status IN ('waiting_copy', 'pending', 'failed')
                """,
                (utcnow(), str(video.path), fingerprint),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    id, fingerprint, source_path, relative_path, size, mtime_ns,
                    detected_at, status, model, input_root, output_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    fingerprint,
                    str(video.path),
                    str(video.relative_path),
                    video.size,
                    video.mtime_ns,
                    utcnow(),
                    status,
                    model,
                    root_value,
                    str(output_path),
                ),
            )
            if root_value is not None:
                connection.execute(
                    "UPDATE jobs SET input_root=? WHERE fingerprint=?",
                    (root_value, fingerprint),
                )
            row = connection.execute(
                "SELECT * FROM jobs WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Could not register video job")
        return dict(row)

    def assign_input_root(self, job_id: str, input_root: str | Path) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET input_root=? WHERE id=?",
                (normalized_input_root(input_root), job_id),
            )

    def assign_input_root_many(
        self,
        job_ids: list[str],
        input_root: str | Path,
    ) -> None:
        if not job_ids:
            return
        root = normalized_input_root(input_root)
        with self._lock, self._connect() as connection:
            connection.executemany(
                "UPDATE jobs SET input_root=? WHERE id=?",
                ((root, job_id) for job_id in job_ids),
            )

    def mark_ready(self, job_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status='pending', error=NULL WHERE id=? AND status='waiting_copy'",
                (job_id,),
            )

    def reset_for_rebuild(self, job_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status='pending',
                    attempts=0,
                    started_at=NULL,
                    finished_at=NULL,
                    duration_seconds=NULL,
                    used_video_copy=NULL,
                    error='Output video is missing; job will be rebuilt'
                WHERE id=?
                """,
                (job_id,),
            )

    def claim(self, job_id: str, max_retries: int) -> dict[str, Any] | None:
        maximum_attempts = max_retries + 1
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status='processing',
                    attempts=attempts+1,
                    started_at=?,
                    finished_at=NULL,
                    duration_seconds=NULL,
                    used_video_copy=NULL,
                    error=NULL
                WHERE id=?
                  AND status IN ('pending', 'failed')
                  AND attempts < ?
                """,
                (utcnow(), job_id, maximum_attempts),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "started_at",
            "finished_at",
            "duration_seconds",
            "used_video_copy",
            "output_path",
            "error",
            "model",
            "device",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job
        if "used_video_copy" in values and values["used_video_copy"] is not None:
            values["used_video_copy"] = int(bool(values["used_video_copy"]))
        assignment = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignment} WHERE id=?",
                (*values.values(), job_id),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def eligible(
        self,
        max_retries: int,
        input_root: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        maximum_attempts = max_retries + 1
        scope = " AND input_root=?" if input_root is not None else ""
        params: tuple[Any, ...] = (
            maximum_attempts,
            *(
                (normalized_input_root(input_root),)
                if input_root is not None
                else ()
            ),
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE status IN ('pending', 'failed')
                  AND attempts < ?
                  {scope}
                ORDER BY detected_at ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_jobs(
        self,
        limit: int = 10000,
        input_root: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE input_root=?" if input_root is not None else ""
        params: tuple[Any, ...] = (
            *(
                (normalized_input_root(input_root),)
                if input_root is not None
                else ()
            ),
            limit,
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs {where} ORDER BY detected_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def counts(self, input_root: str | Path | None = None) -> dict[str, int]:
        where = "WHERE input_root=?" if input_root is not None else ""
        params = (
            (normalized_input_root(input_root),)
            if input_root is not None
            else ()
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM jobs {where} "
                "GROUP BY status",
                params,
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts
