from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


TaskStatus = Literal[
    "waiting",
    "scanning",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
]
ItemStatus = Literal["waiting", "extracting_audio", "separating", "completed", "failed"]


class TaskCreate(BaseModel):
    input_dir: str
    output_dir: str
    model: str = "default"

    @field_validator("input_dir", "output_dir")
    @classmethod
    def non_empty_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Path cannot be empty")
        return value.strip()


class TaskRecord(BaseModel):
    id: str
    status: TaskStatus
    progress: float = 0
    current_file: str | None = None
    input_dir: str
    output_dir: str
    model: str
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class ItemRecord(BaseModel):
    id: str
    task_id: str
    name: str
    relative_path: str
    media_type: Literal["audio", "video"]
    status: ItemStatus
    progress: float = 0
    duration_seconds: float | None = None
    error: str | None = None


class ArtifactRecord(BaseModel):
    id: str
    task_id: str
    item_id: str
    kind: str
    name: str
    media_type: str
    size: int
    url: str
    download_url: str


class UploadResponse(BaseModel):
    upload_id: str
    input_dir: str
    file_count: int


class AutomationUpdate(BaseModel):
    enabled: bool
    input_dir: str
    output_dir: str
    model: str = "default"
    schedule_mode: Literal["daily", "interval"] = "daily"
    daily_time: str = Field(default="00:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = "Asia/Shanghai"
    interval_minutes: int = Field(default=5, ge=1, le=1440)
    stable_seconds: int = Field(default=60, ge=0, le=86400)
    max_retries: int = Field(default=3, ge=0, le=20)

    @field_validator("input_dir", "output_dir", "model")
    @classmethod
    def non_empty_automation_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value cannot be empty")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Unknown timezone") from error
        return value


class AutomationConfigRecord(BaseModel):
    enabled: bool
    input_dir: str
    output_dir: str
    model: str
    schedule_mode: Literal["daily", "interval"]
    daily_time: str
    timezone: str
    interval_minutes: int
    stable_seconds: int
    max_retries: int
    last_scan_at: datetime | None = None
    next_scan_at: datetime | None = None
    last_error: str | None = None
    report_path: str
    counts: dict[str, int] = Field(default_factory=dict)


class AutomationFileRecord(BaseModel):
    id: str
    source_path: str
    relative_path: str
    media_type: Literal["audio", "video"]
    size: int
    modified_at: datetime
    detected_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str
    progress: float = 0
    attempts: int = 0
    task_id: str | None = None
    model: str
    duration_seconds: float | None = None
    vocals_path: str | None = None
    instrumental_path: str | None = None
    error: str | None = None


class AutomationScanResult(BaseModel):
    scanned_files: int
    queued_files: int
    waiting_for_copy: int
    skipped_files: int
    retry_files: int
    scan_started_at: datetime
    scan_finished_at: datetime


class ProcessResult(BaseModel):
    source: Path
    media_type: Literal["audio", "video"]
    output_dir: Path
    outputs: dict[str, Path] = Field(default_factory=dict)
    duration_seconds: float
