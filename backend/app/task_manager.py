import asyncio

from redis.asyncio import Redis

from app.config import Settings
from app.repository import TaskRepository
from app.services.task_processor import TaskProcessor


class TaskManager:
    def __init__(self, settings: Settings, repository: TaskRepository, processor: TaskProcessor) -> None:
        self.settings = settings
        self.repository = repository
        self.processor = processor
        self._background: set[asyncio.Task] = set()

    async def submit(self, task_id: str) -> None:
        if self.settings.task_mode == "redis":
            redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
            try:
                await redis.rpush(self.settings.queue_name, task_id)
            finally:
                await redis.aclose()
            return
        task = asyncio.create_task(self.processor.run(task_id), name=f"audio-task-{task_id}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def shutdown(self) -> None:
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

