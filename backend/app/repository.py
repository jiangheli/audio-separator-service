import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    current_file TEXT,
                    input_dir TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    model TEXT NOT NULL,
                    total_files INTEGER NOT NULL DEFAULT 0,
                    completed_files INTEGER NOT NULL DEFAULT 0,
                    failed_files INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    duration_seconds REAL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_items_task ON items(task_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
                CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id);
                CREATE TABLE IF NOT EXISTS automation_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    input_dir TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    model TEXT NOT NULL,
                    schedule_mode TEXT NOT NULL DEFAULT 'daily',
                    daily_time TEXT NOT NULL DEFAULT '00:00',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    interval_minutes INTEGER NOT NULL DEFAULT 5,
                    stable_seconds INTEGER NOT NULL DEFAULT 60,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    last_scan_at TEXT,
                    next_scan_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_files (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source_path TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    modified_at TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    task_id TEXT,
                    model TEXT NOT NULL,
                    duration_seconds REAL,
                    vocals_path TEXT,
                    instrumental_path TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_automation_files_status ON automation_files(status);
                CREATE INDEX IF NOT EXISTS idx_automation_files_task ON automation_files(task_id);
                CREATE INDEX IF NOT EXISTS idx_automation_files_source ON automation_files(source_path);
                """
            )
            automation_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(automation_config)"
                ).fetchall()
            }
            for name, definition in {
                "schedule_mode": "TEXT NOT NULL DEFAULT 'daily'",
                "daily_time": "TEXT NOT NULL DEFAULT '00:00'",
                "timezone": "TEXT NOT NULL DEFAULT 'Asia/Shanghai'",
            }.items():
                if name not in automation_columns:
                    connection.execute(
                        f"ALTER TABLE automation_config ADD COLUMN {name} {definition}"
                    )

    def ensure_automation_config(
        self,
        *,
        enabled: bool,
        input_dir: str,
        output_dir: str,
        model: str,
        schedule_mode: str,
        daily_time: str,
        timezone: str,
        interval_minutes: int,
        stable_seconds: int,
        max_retries: int,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO automation_config (
                    id, enabled, input_dir, output_dir, model, schedule_mode,
                    daily_time, timezone, interval_minutes, stable_seconds,
                    max_retries, updated_at
                ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(enabled),
                    input_dir,
                    output_dir,
                    model,
                    schedule_mode,
                    daily_time,
                    timezone,
                    interval_minutes,
                    stable_seconds,
                    max_retries,
                    utcnow(),
                ),
            )
        return self.get_automation_config()

    def get_automation_config(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM automation_config WHERE id=1").fetchone()
        if not row:
            raise RuntimeError("Automation configuration has not been initialized")
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        return value

    def update_automation_config(self, **fields: Any) -> dict[str, Any]:
        allowed = {
            "enabled", "input_dir", "output_dir", "model", "schedule_mode",
            "daily_time", "timezone", "interval_minutes", "stable_seconds",
            "max_retries", "last_scan_at", "next_scan_at", "last_error",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if "enabled" in values:
            values["enabled"] = int(bool(values["enabled"]))
        values["updated_at"] = utcnow()
        assignment = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE automation_config SET {assignment} WHERE id=1",
                (*values.values(),),
            )
        return self.get_automation_config()

    def get_automation_file_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_files WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def add_automation_file(
        self,
        *,
        fingerprint: str,
        source_path: str,
        relative_path: str,
        media_type: str,
        size: int,
        mtime_ns: int,
        modified_at: str,
        status: str,
        model: str,
    ) -> dict[str, Any]:
        record_id = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_files (
                    id,fingerprint,source_path,relative_path,media_type,size,mtime_ns,
                    modified_at,detected_at,status,model
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    fingerprint,
                    source_path,
                    relative_path,
                    media_type,
                    size,
                    mtime_ns,
                    modified_at,
                    utcnow(),
                    status,
                    model,
                ),
            )
            row = connection.execute(
                "SELECT * FROM automation_files WHERE id=?",
                (record_id,),
            ).fetchone()
        return dict(row)

    def update_automation_file(self, record_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status", "progress", "attempts", "task_id", "model", "started_at",
            "finished_at", "duration_seconds", "vocals_path", "instrumental_path", "error",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return self.get_automation_file(record_id)
        assignment = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE automation_files SET {assignment} WHERE id=?",
                (*values.values(), record_id),
            )
        return self.get_automation_file(record_id)

    def get_automation_file(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_files WHERE id=?",
                (record_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_automation_files(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_files
                ORDER BY detected_at DESC, relative_path ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def automation_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM automation_files GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts

    def reset_failed_automation_files(self) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_files
                SET attempts=0, task_id=NULL, progress=0, error=NULL,
                    started_at=NULL, finished_at=NULL
                WHERE status='failed'
                """
            )
        return cursor.rowcount

    def reconcile_automation_files(self) -> bool:
        changed = False
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT af.id, af.task_id, af.status AS automation_status,
                       af.progress AS automation_progress,
                       t.status AS task_status, t.progress AS task_progress,
                       t.started_at, t.finished_at, t.error AS task_error
                FROM automation_files af
                JOIN tasks t ON t.id=af.task_id
                WHERE af.task_id IS NOT NULL
                  AND af.status IN ('waiting','processing','retrying')
                """
            ).fetchall()
            for row in rows:
                task_status = row["task_status"]
                if task_status in {"waiting", "scanning"}:
                    status = "waiting"
                elif task_status == "running":
                    status = "processing"
                elif task_status == "completed":
                    status = "completed"
                elif task_status in {"failed", "completed_with_errors"}:
                    status = "failed"
                else:
                    status = row["automation_status"]

                item = connection.execute(
                    """
                    SELECT id,duration_seconds,error FROM items
                    WHERE task_id=? ORDER BY rowid LIMIT 1
                    """,
                    (row["task_id"],),
                ).fetchone()
                artifacts = connection.execute(
                    "SELECT kind,path FROM artifacts WHERE task_id=?",
                    (row["task_id"],),
                ).fetchall()
                artifact_paths = {artifact["kind"]: artifact["path"] for artifact in artifacts}
                error = row["task_error"] or (item["error"] if item else None)
                progress = float(row["task_progress"] or 0)
                if (
                    status != row["automation_status"]
                    or progress != float(row["automation_progress"] or 0)
                ):
                    changed = True
                connection.execute(
                    """
                    UPDATE automation_files
                    SET status=?, progress=?, started_at=COALESCE(?, started_at),
                        finished_at=?, duration_seconds=?, vocals_path=?,
                        instrumental_path=?, error=?
                    WHERE id=?
                    """,
                    (
                        status,
                        progress,
                        row["started_at"],
                        row["finished_at"],
                        item["duration_seconds"] if item else None,
                        artifact_paths.get("vocals"),
                        artifact_paths.get("instrumental"),
                        error,
                        row["id"],
                    ),
                )
        return changed

    def create_task(self, input_dir: str, output_dir: str, model: str) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks (id,status,input_dir,output_dir,model,created_at) VALUES (?,?,?,?,?,?)",
                (task_id, "waiting", input_dir, output_dir, model, utcnow()),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def claim_task(self, task_id: str) -> bool:
        """Atomically claim a waiting task for scanning and queue expansion."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status='scanning', progress=0, started_at=?, finished_at=NULL, error=NULL
                WHERE id=? AND status='waiting'
                """,
                (utcnow(), task_id),
            )
        return cursor.rowcount == 1

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "status", "progress", "current_file", "total_files", "completed_files",
            "failed_files", "started_at", "finished_at", "error",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        assignment = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE tasks SET {assignment} WHERE id=?", (*values.values(), task_id))

    def add_item(self, task_id: str, name: str, relative_path: str, source_path: str, media_type: str) -> dict[str, Any]:
        item_id = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO items (id,task_id,name,relative_path,source_path,media_type,status) VALUES (?,?,?,?,?,?,?)",
                (item_id, task_id, name, relative_path, source_path, media_type, "waiting"),
            )
        return self.get_item(item_id)

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def claim_item(self, task_id: str, item_id: str) -> dict[str, Any] | None:
        """Atomically claim one waiting media item for one inference slot."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE items
                SET status='separating', progress=1, error=NULL
                WHERE id=? AND task_id=? AND status='waiting'
                """,
                (item_id, task_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def list_items(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM items WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_item(self, item_id: str, **fields: Any) -> None:
        allowed = {"status", "progress", "duration_seconds", "error"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        assignment = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE items SET {assignment} WHERE id=?", (*values.values(), item_id))

    def add_artifact(self, task_id: str, item_id: str, kind: str, name: str, path: Path, media_type: str) -> dict[str, Any]:
        artifact_id = uuid.uuid4().hex
        size = path.stat().st_size if path.exists() else 0
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO artifacts (id,task_id,item_id,kind,name,path,media_type,size) VALUES (?,?,?,?,?,?,?,?)",
                (artifact_id, task_id, item_id, kind, name, str(path.resolve()), media_type, size),
            )
        return self.get_artifact(task_id, artifact_id)

    def get_artifact(self, task_id: str, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE task_id=? AND id=?", (task_id, artifact_id)).fetchone()
        return dict(row) if row else None

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def sync_task_from_items(self, task_id: str, current_file: str | None = None) -> dict[str, Any] | None:
        """Recalculate aggregate progress under one SQLite write transaction."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return None
            stats = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                    COALESCE(AVG(progress), 0) AS progress
                FROM items
                WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            total = int(stats["total"] or 0)
            completed = int(stats["completed"] or 0)
            failed = int(stats["failed"] or 0)
            progress = round(float(stats["progress"] or 0), 1)
            finished = total > 0 and completed + failed == total
            if finished:
                status = "completed" if failed == 0 else ("failed" if completed == 0 else "completed_with_errors")
                connection.execute(
                    """
                    UPDATE tasks
                    SET status=?, progress=100, current_file=NULL, total_files=?,
                        completed_files=?, failed_files=?, finished_at=?, error=?
                    WHERE id=?
                    """,
                    (
                        status,
                        total,
                        completed,
                        failed,
                        utcnow(),
                        "All files failed" if completed == 0 else None,
                        task_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status='running', progress=?, current_file=?, total_files=?,
                        completed_files=?, failed_files=?
                    WHERE id=?
                    """,
                    (progress, current_file, total, completed, failed, task_id),
                )
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def log(self, task_id: str, message: str, level: str = "info") -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO logs (task_id,created_at,level,message) VALUES (?,?,?,?)",
                (task_id, utcnow(), level, message),
            )

    def list_logs(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT created_at,level,message FROM logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]
