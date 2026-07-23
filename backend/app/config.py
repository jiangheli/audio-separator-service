from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUDIO_SERVICE_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./data")
    database_path: Path | None = None
    model_dir: Path | None = None
    task_mode: str = "local"
    redis_url: str = "redis://redis:6379/0"
    queue_name: str = "audio-separator:tasks"
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    default_model: str = "UVR-MDX-NET-Inst_HQ_3.onnx"
    output_format: str = "WAV"
    chunk_duration: float | None = None
    mdx_segment_size: int = 32
    mdxc_segment_size: int = 256
    mdxc_override_model_segment_size: bool = True
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    automation_enabled: bool = False
    automation_input_dir: Path = Path("/input")
    automation_output_dir: Path = Path("/output")
    automation_schedule_mode: str = "daily"
    automation_daily_time: str = "00:00"
    automation_timezone: str = "Asia/Shanghai"
    automation_interval_minutes: int = Field(default=5, ge=1, le=1440)
    automation_stable_seconds: int = Field(default=60, ge=0, le=86400)
    automation_max_retries: int = Field(default=3, ge=0, le=20)
    automation_model: str = "default"
    automation_poll_seconds: int = Field(default=2, ge=1, le=60)

    def prepare(self) -> "Settings":
        self.data_dir = self.data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "output").mkdir(parents=True, exist_ok=True)
        if self.database_path is None:
            self.database_path = self.data_dir / "tasks.db"
        else:
            self.database_path = self.database_path.expanduser().resolve()
        if self.model_dir is None:
            self.model_dir = self.data_dir / "models"
        else:
            self.model_dir = self.model_dir.expanduser().resolve()
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.automation_input_dir = self.automation_input_dir.expanduser().resolve()
        self.automation_output_dir = self.automation_output_dir.expanduser().resolve()
        return self

    @property
    def automation_report_path(self) -> Path:
        return self.data_dir / "processing_status.xlsx"

    @property
    def origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings().prepare()
