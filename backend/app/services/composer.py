from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from app.services.extractor import CREATE_NO_WINDOW


class VideoCompositionError(RuntimeError):
    pass


class VideoComposer:
    def __init__(self, ffmpeg_binary: str, *, audio_bitrate: str = "192k") -> None:
        self.ffmpeg_binary = ffmpeg_binary
        self.audio_bitrate = audio_bitrate
        self._last_error = ""

    def compose(
        self,
        source_video: Path,
        vocals_audio: Path,
        output_video: Path,
        *,
        prefer_stream_copy: bool = True,
    ) -> bool:
        output_video.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_video.with_name(
            f".{output_video.stem}.{uuid.uuid4().hex}.tmp.mp4"
        )
        try:
            if prefer_stream_copy:
                copied = self._run(source_video, vocals_audio, temporary, copy_video=True)
                if copied:
                    os.replace(temporary, output_video)
                    return True
                temporary.unlink(missing_ok=True)

            if not self._run(source_video, vocals_audio, temporary, copy_video=False):
                raise VideoCompositionError(
                    f"FFmpeg could not compose the vocals-only video "
                    f"{source_video.name}: {self._last_error or 'unknown FFmpeg error'}"
                )
            os.replace(temporary, output_video)
            return False
        finally:
            temporary.unlink(missing_ok=True)

    def _run(
        self,
        source_video: Path,
        vocals_audio: Path,
        temporary: Path,
        *,
        copy_video: bool,
    ) -> bool:
        video_codec = ["-c:v", "copy"] if copy_video else [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
        ]
        command = [
            self.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_video),
            "-i",
            str(vocals_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-sn",
            *video_codec,
            "-c:a",
            "aac",
            "-b:a",
            self.audio_bitrate,
            "-ar",
            "44100",
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        succeeded = (
            result.returncode == 0
            and temporary.is_file()
            and temporary.stat().st_size > 0
        )
        if not succeeded:
            self._last_error = result.stderr.strip()[-6000:]
        return succeeded
