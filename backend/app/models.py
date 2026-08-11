from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VideoFile:
    path: Path
    relative_path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class VideoProcessResult:
    source: Path
    output_video: Path
    duration_seconds: float
    used_video_copy: bool
