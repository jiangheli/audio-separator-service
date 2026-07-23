import asyncio
from collections.abc import Callable
from pathlib import Path

from app.repository import TaskRepository, utcnow
from app.services.scanner import reserve_output_subdirectory, scan_media
from app.services.separator import AudioSeparatorService


ServiceFactory = Callable[[], AudioSeparatorService]


class TaskProcessor:
    def __init__(
        self,
        repository: TaskRepository,
        service: AudioSeparatorService,
        *,
        concurrency: int = 1,
        service_factory: ServiceFactory | None = None,
    ) -> None:
        self.repository = repository
        self.service = service
        self.service_factory = service_factory
        self.concurrency = max(1, concurrency) if service_factory else 1

    async def prepare(self, task_id: str) -> list[str]:
        """Scan one task once and persist its independently queueable media items."""
        task = self.repository.get_task(task_id)
        if not task or not self.repository.claim_task(task_id):
            return []
        try:
            input_path = Path(task["input_dir"])
            output_root = Path(task["output_dir"])
            output_root.mkdir(parents=True, exist_ok=True)
            media_files = scan_media(input_path)
            if not media_files:
                raise ValueError("No supported audio or video files were found in the input path")

            item_ids = []
            for media in media_files:
                item = self.repository.add_item(
                    task_id,
                    media.path.name,
                    str(media.relative_path),
                    str(media.path),
                    media.media_type,
                )
                item_ids.append(item["id"])

            self.repository.update_task(
                task_id,
                status="running",
                total_files=len(media_files),
                progress=0,
                completed_files=0,
                failed_files=0,
            )
            self.repository.log(
                task_id,
                f"Found {len(media_files)} supported media files; queued for concurrent processing",
            )
            return item_ids
        except Exception as error:
            self.repository.update_task(
                task_id,
                status="failed",
                error=str(error),
                progress=100,
                current_file=None,
                finished_at=utcnow(),
            )
            self.repository.log(task_id, f"Task failed during scanning: {error}", "error")
            return []

    async def _run_item(
        self,
        task_id: str,
        item_id: str,
        service: AudioSeparatorService,
    ) -> None:
        item = self.repository.claim_item(task_id, item_id)
        task = self.repository.get_task(task_id)
        if not item or not task:
            return

        source = Path(item["source_path"])
        output_root = Path(task["output_dir"])
        target = reserve_output_subdirectory(output_root, Path(item["relative_path"]))

        def on_progress(status: str, item_progress: float, message: str) -> None:
            self.repository.update_item(item_id, status=status, progress=item_progress)
            self.repository.sync_task_from_items(task_id, current_file=item["name"])
            self.repository.log(task_id, message)

        try:
            result = await service.process_file(source, target, task["model"], on_progress)
            self.repository.update_item(
                item_id,
                status="completed",
                progress=100,
                duration_seconds=result.duration_seconds,
            )
            self.repository.add_artifact(
                task_id,
                item_id,
                "original",
                source.name,
                source,
                item["media_type"],
            )
            for kind, path in result.outputs.items():
                self.repository.add_artifact(task_id, item_id, kind, path.name, path, "audio")
            self.repository.log(task_id, f"{item['name']} completed")
        except Exception as error:
            self.repository.update_item(item_id, status="failed", progress=100, error=str(error))
            self.repository.log(task_id, f"{item['name']} failed: {error}", "error")
        final_task = self.repository.sync_task_from_items(task_id, current_file=item["name"])
        if final_task and final_task["status"] in {"completed", "completed_with_errors", "failed"}:
            self.repository.log(
                task_id,
                f"Task finished: {final_task['completed_files']} completed, "
                f"{final_task['failed_files']} failed",
            )

    async def run_item(self, task_id: str, item_id: str) -> None:
        await self._run_item(task_id, item_id, self.service)

    async def run(self, task_id: str) -> None:
        """Local-mode fallback: process one task with the configured slot count."""
        item_ids = await self.prepare(task_id)
        if not item_ids:
            return
        queue: asyncio.Queue[str] = asyncio.Queue()
        for item_id in item_ids:
            queue.put_nowait(item_id)

        async def run_slot(service: AudioSeparatorService) -> None:
            while not queue.empty():
                try:
                    item_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._run_item(task_id, item_id, service)
                finally:
                    queue.task_done()

        services = [self.service]
        if self.service_factory:
            services.extend(self.service_factory() for _ in range(self.concurrency - 1))
        await asyncio.gather(*(run_slot(service) for service in services))
