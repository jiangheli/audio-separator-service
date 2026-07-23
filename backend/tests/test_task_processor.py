import asyncio
from pathlib import Path

import pytest

from app.models import ProcessResult
from app.repository import TaskRepository
from app.services.task_processor import TaskProcessor


class FakeService:
    async def process_file(self, source: Path, output_dir: Path, model: str, progress):
        progress("separating", 50, "working")
        output_dir.mkdir(parents=True, exist_ok=True)
        vocals = output_dir / "vocals.wav"
        instrumental = output_dir / "instrumental.wav"
        vocals.write_bytes(b"vocals")
        instrumental.write_bytes(b"instrumental")
        return ProcessResult(
            source=source,
            media_type="audio",
            output_dir=output_dir,
            outputs={"vocals": vocals, "instrumental": instrumental},
            duration_seconds=1.5,
        )


@pytest.mark.asyncio
async def test_task_processor_persists_progress_and_results(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "demo.wav").write_bytes(b"wave")
    repository = TaskRepository(tmp_path / "tasks.db")
    task = repository.create_task(str(input_dir), str(tmp_path / "output"), "default")

    await TaskProcessor(repository, FakeService()).run(task["id"])

    completed = repository.get_task(task["id"])
    assert completed["status"] == "completed"
    assert completed["progress"] == 100
    assert repository.list_items(task["id"])[0]["status"] == "completed"
    assert {item["kind"] for item in repository.list_artifacts(task["id"])} == {"original", "vocals", "instrumental"}


class ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0


class ConcurrentFakeService:
    def __init__(self, tracker: ConcurrencyTracker) -> None:
        self.tracker = tracker

    async def process_file(self, source: Path, output_dir: Path, model: str, progress):
        self.tracker.active += 1
        self.tracker.max_active = max(self.tracker.max_active, self.tracker.active)
        try:
            progress("separating", 50, f"working on {source.name}")
            await asyncio.sleep(0.05)
            vocals = output_dir / "vocals.wav"
            instrumental = output_dir / "instrumental.wav"
            vocals.write_bytes(b"vocals")
            instrumental.write_bytes(b"instrumental")
            return ProcessResult(
                source=source,
                media_type="audio",
                output_dir=output_dir,
                outputs={"vocals": vocals, "instrumental": instrumental},
                duration_seconds=0.05,
            )
        finally:
            self.tracker.active -= 1


@pytest.mark.asyncio
async def test_task_processor_processes_batch_files_concurrently(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for index in range(4):
        (input_dir / f"demo-{index}.wav").write_bytes(b"wave")
    repository = TaskRepository(tmp_path / "tasks.db")
    task = repository.create_task(str(input_dir), str(tmp_path / "output"), "default")
    tracker = ConcurrencyTracker()

    def service_factory() -> ConcurrentFakeService:
        return ConcurrentFakeService(tracker)

    processor = TaskProcessor(
        repository,
        service_factory(),
        concurrency=2,
        service_factory=service_factory,
    )
    await processor.run(task["id"])

    completed = repository.get_task(task["id"])
    assert tracker.max_active == 2
    assert completed["status"] == "completed"
    assert completed["completed_files"] == 4
    assert all(item["status"] == "completed" for item in repository.list_items(task["id"]))
