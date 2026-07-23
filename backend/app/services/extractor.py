import asyncio
import shutil
from pathlib import Path


class AudioExtractionError(RuntimeError):
    pass


class AudioExtractor:
    def __init__(self, ffmpeg_binary: str = "ffmpeg") -> None:
        self.ffmpeg_binary = ffmpeg_binary

    def available(self) -> bool:
        return shutil.which(self.ffmpeg_binary) is not None

    async def extract_audio(self, video_path: Path, target_path: Path) -> Path:
        if not self.available():
            raise AudioExtractionError("ffmpeg was not found. Install ffmpeg or use the Docker image.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise AudioExtractionError(f"ffmpeg could not extract audio from {video_path.name}: {message}")
        return target_path

