from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Iterable

from app.models import VideoFile


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def scan_videos(
    input_dir: Path,
    *,
    recursive: bool = True,
    excluded_roots: Iterable[Path] = (),
) -> list[VideoFile]:
    root = input_dir.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {root}")

    excluded = tuple(path.expanduser().resolve() for path in excluded_roots)
    candidates = root.rglob("*") if recursive else root.glob("*")
    videos: list[VideoFile] = []
    for path in sorted(candidates):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        resolved = path.resolve()
        if any(is_relative_to(resolved, blocked) for blocked in excluded):
            continue
        stat = resolved.stat()
        videos.append(
            VideoFile(
                path=resolved,
                relative_path=resolved.relative_to(root),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return videos


def file_fingerprint(video: VideoFile) -> str:
    material = f"{video.path}\0{video.size}\0{video.mtime_ns}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def is_file_stable(video: VideoFile, stable_seconds: int, *, now: float | None = None) -> bool:
    if stable_seconds <= 0:
        return True
    current = time.time() if now is None else now
    return current - (video.mtime_ns / 1_000_000_000) >= stable_seconds


def output_path_for(output_root: Path, video: VideoFile, suffix: str) -> Path:
    return output_root / video.relative_path.parent / f"{video.path.stem}{suffix}.mp4"
