from __future__ import annotations

import subprocess
from pathlib import Path


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AudioExtractionError(RuntimeError):
    pass


class AudioExtractor:
    def __init__(self, ffmpeg_binary: str) -> None:
        self.ffmpeg_binary = ffmpeg_binary

    def extract_audio(self, video_path: Path, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-map",
            "0:a:0",
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(target_path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        if result.returncode != 0 or not target_path.is_file():
            message = result.stderr.strip() or "the video has no readable audio track"
            raise AudioExtractionError(
                f"Could not extract audio from {video_path.name}: {message}"
            )
        return target_path
