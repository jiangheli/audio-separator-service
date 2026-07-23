import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.repository import TaskRepository, utcnow
from app.services.automation_report import AutomationReport
from app.services.scanner import MediaFile, scan_media
from app.task_manager import TaskManager


logger = logging.getLogger(__name__)


class AutomationService:
    def __init__(
        self,
        settings: Settings,
        repository: TaskRepository,
        task_manager: TaskManager,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.task_manager = task_manager
        self.report = AutomationReport(settings.automation_report_path)
        self._scan_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self.repository.ensure_automation_config(
            enabled=settings.automation_enabled,
            input_dir=str(settings.automation_input_dir),
            output_dir=str(settings.automation_output_dir),
            model=settings.automation_model,
            schedule_mode=settings.automation_schedule_mode,
            daily_time=settings.automation_daily_time,
            timezone=settings.automation_timezone,
            interval_minutes=settings.automation_interval_minutes,
            stable_seconds=settings.automation_stable_seconds,
            max_retries=settings.automation_max_retries,
        )

    async def start(self) -> None:
        if self._runner and not self._runner.done():
            return
        self._stop.clear()
        self._runner = asyncio.create_task(
            self._run(),
            name="audio-separator-automation",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._runner:
            await self._runner
            self._runner = None

    def status(self) -> dict[str, Any]:
        config = self.repository.get_automation_config()
        return {
            **config,
            "report_path": str(self.settings.automation_report_path),
            "counts": self.repository.automation_counts(),
        }

    def files(self, limit: int = 1000) -> list[dict[str, Any]]:
        return self.repository.list_automation_files(limit)

    async def update_config(self, values: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        next_scan = (
            self.next_scan_at(values, now).isoformat()
            if values["enabled"]
            else None
        )
        config = self.repository.update_automation_config(
            **values,
            next_scan_at=next_scan,
            last_error=None,
        )
        await self._refresh_report()
        return {
            **config,
            "report_path": str(self.settings.automation_report_path),
            "counts": self.repository.automation_counts(),
        }

    async def retry_failed(self) -> int:
        count = self.repository.reset_failed_automation_files()
        if count:
            await self.scan_now()
        else:
            await self._refresh_report()
        return count

    async def scan_now(self) -> dict[str, Any]:
        async with self._scan_lock:
            started = datetime.now(timezone.utc)
            config = self.repository.get_automation_config()
            result = {
                "scanned_files": 0,
                "queued_files": 0,
                "waiting_for_copy": 0,
                "skipped_files": 0,
                "retry_files": 0,
                "scan_started_at": started.isoformat(),
                "scan_finished_at": started.isoformat(),
            }
            try:
                input_root = Path(config["input_dir"]).expanduser().resolve()
                output_root = Path(config["output_dir"]).expanduser().resolve()
                if not input_root.is_dir():
                    raise FileNotFoundError(f"监控目录不存在或不是文件夹：{input_root}")
                output_root.mkdir(parents=True, exist_ok=True)
                media_files = scan_media(input_root)
                result["scanned_files"] = len(media_files)
                for media in media_files:
                    action = await self._handle_media(
                        media,
                        input_root,
                        output_root,
                        config,
                    )
                    result[action] += 1
                finished = datetime.now(timezone.utc)
                next_scan = self.next_scan_at(config, finished)
                self.repository.update_automation_config(
                    last_scan_at=finished.isoformat(),
                    next_scan_at=next_scan.isoformat() if config["enabled"] else None,
                    last_error=None,
                )
                result["scan_finished_at"] = finished.isoformat()
            except Exception as error:
                finished = datetime.now(timezone.utc)
                next_scan = self.next_scan_at(config, finished)
                self.repository.update_automation_config(
                    last_scan_at=finished.isoformat(),
                    next_scan_at=next_scan.isoformat() if config["enabled"] else None,
                    last_error=str(error),
                )
                result["scan_finished_at"] = finished.isoformat()
                await self._refresh_report()
                raise
            await self._refresh_report()
            return result

    async def _handle_media(
        self,
        media: MediaFile,
        input_root: Path,
        output_root: Path,
        config: dict[str, Any],
    ) -> str:
        stat = media.path.stat()
        fingerprint = hashlib.sha256(
            f"{media.path.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()
        ).hexdigest()
        record = self.repository.get_automation_file_by_fingerprint(fingerprint)
        stable = time.time() - stat.st_mtime >= int(config["stable_seconds"])
        if record is None:
            record = self.repository.add_automation_file(
                fingerprint=fingerprint,
                source_path=str(media.path.resolve()),
                relative_path=str(media.path.relative_to(input_root)),
                media_type=media.media_type,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                modified_at=datetime.fromtimestamp(
                    stat.st_mtime,
                    timezone.utc,
                ).isoformat(),
                status="waiting" if stable else "file_copying",
                model=config["model"],
            )
            if not stable:
                return "waiting_for_copy"
            await self._queue_record(record, output_root, config, retry=False)
            return "queued_files"

        if record["status"] == "file_copying":
            if not stable:
                return "waiting_for_copy"
            await self._queue_record(record, output_root, config, retry=False)
            return "queued_files"

        if (
            record["status"] == "failed"
            and int(record["attempts"]) <= int(config["max_retries"])
        ):
            await self._queue_record(record, output_root, config, retry=True)
            return "retry_files"
        return "skipped_files"

    async def _queue_record(
        self,
        record: dict[str, Any],
        output_root: Path,
        config: dict[str, Any],
        *,
        retry: bool,
    ) -> None:
        source = Path(record["source_path"])
        relative = Path(record["relative_path"])
        target_root = output_root / relative.parent
        target_root.mkdir(parents=True, exist_ok=True)
        task = self.repository.create_task(
            str(source),
            str(target_root),
            config["model"],
        )
        attempts = int(record["attempts"]) + 1
        self.repository.update_automation_file(
            record["id"],
            status="retrying" if retry else "waiting",
            progress=0,
            attempts=attempts,
            task_id=task["id"],
            model=config["model"],
            started_at=None,
            finished_at=None,
            duration_seconds=None,
            vocals_path=None,
            instrumental_path=None,
            error=None,
        )
        try:
            await self.task_manager.submit(task["id"])
        except Exception as error:
            self.repository.update_task(
                task["id"],
                status="failed",
                progress=100,
                finished_at=utcnow(),
                error=str(error),
            )
            self.repository.update_automation_file(
                record["id"],
                status="failed",
                progress=100,
                finished_at=utcnow(),
                error=str(error),
            )
            raise

    async def _run(self) -> None:
        await self._refresh_report()
        while not self._stop.is_set():
            try:
                changed = self.repository.reconcile_automation_files()
                config = self.repository.get_automation_config()
                if config["enabled"]:
                    if config.get("next_scan_at") is None:
                        self.repository.update_automation_config(
                            next_scan_at=self.next_scan_at(
                                config,
                                datetime.now(timezone.utc),
                            ).isoformat()
                        )
                    elif self._is_due(config["next_scan_at"]):
                        await self.scan_now()
                if changed or not self.settings.automation_report_path.exists():
                    await self._refresh_report()
            except Exception:
                logger.exception("Automatic folder scan failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.automation_poll_seconds,
                )
            except TimeoutError:
                pass
        self.repository.reconcile_automation_files()
        await self._refresh_report()

    async def _refresh_report(self) -> None:
        config = self.repository.get_automation_config()
        records = self.repository.list_automation_files()
        await asyncio.to_thread(self.report.export, config, records)

    @staticmethod
    def _is_due(value: str | None) -> bool:
        if value is None:
            return False
        return datetime.fromisoformat(value) <= datetime.now(timezone.utc)

    @staticmethod
    def next_scan_at(
        config: dict[str, Any],
        now: datetime | None = None,
    ) -> datetime:
        current = now or datetime.now(timezone.utc)
        if config.get("schedule_mode") == "interval":
            return current + timedelta(minutes=int(config["interval_minutes"]))

        schedule_timezone = ZoneInfo(config.get("timezone") or "Asia/Shanghai")
        local_now = current.astimezone(schedule_timezone)
        hour_text, minute_text = (config.get("daily_time") or "00:00").split(":")
        candidate = local_now.replace(
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0,
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)
