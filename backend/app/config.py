from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "UVR-MDX-NET-Inst_HQ_3.onnx"
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _expand_path(value: str | Path, *, base: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


@dataclass(slots=True)
class ServiceConfig:
    input_dir: Path
    output_dir: Path
    data_dir: Path
    model_dir: Path
    work_dir: Path
    log_dir: Path
    database_path: Path
    report_path: Path
    schedule_time: str = "00:00"
    model: str = DEFAULT_MODEL
    stable_seconds: int = 120
    max_retries: int = 3
    output_suffix: str = "_vocals_only"
    audio_bitrate: str = "192k"
    recursive: bool = True
    keep_failed_work: bool = False
    video_copy: bool = True

    @classmethod
    def load(cls, config_path: str | Path) -> "ServiceConfig":
        path = Path(config_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file does not exist: {path}")
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("Configuration root must be a JSON object")
        return cls.from_mapping(raw, base=path.parent)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, base: Path) -> "ServiceConfig":
        missing = [
            name
            for name in ("input_dir", "output_dir")
            if not str(raw.get(name, "")).strip()
        ]
        if missing:
            raise ValueError(f"Missing required configuration values: {', '.join(missing)}")

        data_dir = _expand_path(raw.get("data_dir", "./data"), base=base)
        config = cls(
            input_dir=_expand_path(raw["input_dir"], base=base),
            output_dir=_expand_path(raw["output_dir"], base=base),
            data_dir=data_dir,
            model_dir=_expand_path(raw.get("model_dir", data_dir / "models"), base=base),
            work_dir=_expand_path(raw.get("work_dir", data_dir / "work"), base=base),
            log_dir=_expand_path(raw.get("log_dir", data_dir / "logs"), base=base),
            database_path=_expand_path(
                raw.get("database_path", data_dir / "processing.db"),
                base=base,
            ),
            report_path=_expand_path(
                raw.get("report_path", data_dir / "processing_status.xlsx"),
                base=base,
            ),
            schedule_time=str(raw.get("schedule_time", "00:00")),
            model=str(raw.get("model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL,
            stable_seconds=int(raw.get("stable_seconds", 120)),
            max_retries=int(raw.get("max_retries", 3)),
            output_suffix=str(raw.get("output_suffix", "_vocals_only")),
            audio_bitrate=str(raw.get("audio_bitrate", "192k")),
            recursive=bool(raw.get("recursive", True)),
            keep_failed_work=bool(raw.get("keep_failed_work", False)),
            video_copy=bool(raw.get("video_copy", True)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not TIME_PATTERN.fullmatch(self.schedule_time):
            raise ValueError("schedule_time must use HH:MM in 24-hour format")
        if self.stable_seconds < 0 or self.stable_seconds > 86400:
            raise ValueError("stable_seconds must be between 0 and 86400")
        if self.max_retries < 0 or self.max_retries > 20:
            raise ValueError("max_retries must be between 0 and 20")
        if not self.output_suffix or any(char in self.output_suffix for char in '<>:"/\\|?*'):
            raise ValueError("output_suffix contains invalid filename characters")
        if not re.fullmatch(r"\d{2,4}k", self.audio_bitrate):
            raise ValueError("audio_bitrate must look like 128k or 192k")
        if self.input_dir == self.output_dir:
            raise ValueError("input_dir and output_dir must be different")

    def prepare_directories(self) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, Any]:
        values = asdict(self)
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in values.items()
        }
