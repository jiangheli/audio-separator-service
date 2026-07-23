from functools import lru_cache

from app.config import get_settings
from app.repository import TaskRepository
from app.runtime import ensure_ffmpeg
from app.services.automation import AutomationService
from app.services.extractor import AudioExtractor
from app.services.separator import AudioSeparatorService, PythonAudioSeparatorEngine
from app.services.task_processor import TaskProcessor
from app.task_manager import TaskManager


@lru_cache
def get_repository() -> TaskRepository:
    return TaskRepository(get_settings().database_path)


@lru_cache
def get_audio_service() -> AudioSeparatorService:
    return create_audio_service()


def create_audio_service() -> AudioSeparatorService:
    settings = get_settings()
    ffmpeg_binary = ensure_ffmpeg()
    engine = PythonAudioSeparatorEngine(
        settings.model_dir,
        default_model=settings.default_model,
        output_format=settings.output_format,
        chunk_duration=settings.chunk_duration,
        mdx_segment_size=settings.mdx_segment_size,
        mdxc_segment_size=settings.mdxc_segment_size,
        mdxc_override_model_segment_size=settings.mdxc_override_model_segment_size,
    )
    return AudioSeparatorService(engine, AudioExtractor(ffmpeg_binary))


@lru_cache
def get_processor() -> TaskProcessor:
    settings = get_settings()
    return TaskProcessor(
        get_repository(),
        get_audio_service(),
        concurrency=settings.worker_concurrency,
        service_factory=create_audio_service,
    )


def create_processor(*, concurrency: int = 1) -> TaskProcessor:
    return TaskProcessor(
        TaskRepository(get_settings().database_path),
        create_audio_service(),
        concurrency=concurrency,
        service_factory=create_audio_service if concurrency > 1 else None,
    )


@lru_cache
def get_task_manager() -> TaskManager:
    return TaskManager(get_settings(), get_repository(), get_processor())


@lru_cache
def get_automation_service() -> AutomationService:
    return AutomationService(
        get_settings(),
        get_repository(),
        get_task_manager(),
    )
