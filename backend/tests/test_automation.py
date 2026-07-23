import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.repository import TaskRepository
from app.services.automation import AutomationService


class FakeTaskManager:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit(self, task_id: str) -> None:
        self.submitted.append(task_id)


def test_daily_schedule_uses_configured_timezone_and_next_day() -> None:
    config = {
        "schedule_mode": "daily",
        "daily_time": "00:00",
        "timezone": "Asia/Shanghai",
        "interval_minutes": 5,
    }

    before_midnight = AutomationService.next_scan_at(
        config,
        datetime(2026, 7, 23, 15, 30, tzinfo=timezone.utc),
    )
    after_midnight = AutomationService.next_scan_at(
        config,
        datetime(2026, 7, 23, 16, 30, tzinfo=timezone.utc),
    )

    assert before_midnight == datetime(2026, 7, 23, 16, 0, tzinfo=timezone.utc)
    assert after_midnight == datetime(2026, 7, 24, 16, 0, tzinfo=timezone.utc)


def test_interval_schedule_remains_available() -> None:
    next_scan = AutomationService.next_scan_at(
        {
            "schedule_mode": "interval",
            "interval_minutes": 30,
        },
        datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
    )

    assert next_scan == datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)


def automation_settings(tmp_path: Path, input_dir: Path, output_dir: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "tasks.db",
        model_dir=tmp_path / "models",
        automation_input_dir=input_dir,
        automation_output_dir=output_dir,
        automation_stable_seconds=60,
    ).prepare()


@pytest.mark.asyncio
async def test_automation_scan_queues_stable_file_once_and_writes_report(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    nested = input_dir / "series"
    nested.mkdir(parents=True)
    source = nested / "demo.wav"
    source.write_bytes(b"wave")
    old_time = time.time() - 120
    source.touch()
    source.chmod(0o644)
    import os
    os.utime(source, (old_time, old_time))

    settings = automation_settings(tmp_path, input_dir, output_dir)
    repository = TaskRepository(settings.database_path)
    manager = FakeTaskManager()
    service = AutomationService(settings, repository, manager)

    first = await service.scan_now()
    second = await service.scan_now()

    assert first["scanned_files"] == 1
    assert first["queued_files"] == 1
    assert second["queued_files"] == 0
    assert second["skipped_files"] == 1
    assert len(manager.submitted) == 1
    record = repository.list_automation_files()[0]
    assert record["relative_path"] == "series/demo.wav"
    assert record["status"] == "waiting"
    assert record["attempts"] == 1
    task = repository.get_task(record["task_id"])
    assert Path(task["output_dir"]) == output_dir / "series"
    assert settings.automation_report_path.is_file()
    assert zipfile.is_zipfile(settings.automation_report_path)


@pytest.mark.asyncio
async def test_automation_waits_for_copy_then_queues_same_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    source = input_dir / "fresh.mp4"
    source.write_bytes(b"video")

    settings = automation_settings(tmp_path, input_dir, output_dir)
    repository = TaskRepository(settings.database_path)
    manager = FakeTaskManager()
    service = AutomationService(settings, repository, manager)

    first = await service.scan_now()
    modified = source.stat().st_mtime
    monkeypatch.setattr(
        "app.services.automation.time.time",
        lambda: modified + 61,
    )
    second = await service.scan_now()

    assert first["waiting_for_copy"] == 1
    assert second["queued_files"] == 1
    assert len(manager.submitted) == 1


@pytest.mark.asyncio
async def test_automation_retries_failed_file_with_new_task(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    source = input_dir / "retry.wav"
    source.write_bytes(b"wave")
    old_time = time.time() - 120
    import os
    os.utime(source, (old_time, old_time))

    settings = automation_settings(tmp_path, input_dir, output_dir)
    repository = TaskRepository(settings.database_path)
    manager = FakeTaskManager()
    service = AutomationService(settings, repository, manager)
    await service.scan_now()
    record = repository.list_automation_files()[0]
    first_task_id = record["task_id"]
    repository.update_automation_file(
        record["id"],
        status="failed",
        progress=100,
        error="temporary failure",
    )

    result = await service.scan_now()
    retried = repository.get_automation_file(record["id"])

    assert result["retry_files"] == 1
    assert retried["task_id"] != first_task_id
    assert retried["attempts"] == 2
    assert retried["status"] == "retrying"
