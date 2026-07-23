from __future__ import annotations

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

    def recover_interrupted(self) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET status='failed',
                    finished_at=?,
                    error='Previous process stopped before this job completed'
                WHERE status IN ({placeholders})
                """,
                (utcnow(), *sorted(ACTIVE_STATUSES)),
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
    ) -> dict[str, Any]:
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
                    detected_at, status, model, output_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(output_path),
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Could not register video job")
        return dict(row)

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

    def eligible(self, max_retries: int) -> list[dict[str, Any]]:
        maximum_attempts = max_retries + 1
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('pending', 'failed')
                  AND attempts < ?
                ORDER BY detected_at ASC
                """,
                (maximum_attempts,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_jobs(self, limit: int = 10000) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY detected_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts
