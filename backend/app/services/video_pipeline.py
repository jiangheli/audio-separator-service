from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path

from app.models import VideoProcessResult
from app.services.composer import VideoComposer
from app.services.extractor import AudioExtractor
from app.services.separator import PythonAudioSeparatorEngine


ProgressCallback = Callable[[str, str], None]


class VideoBgmRemovalPipeline:
    def __init__(
        self,
        extractor: AudioExtractor,
        separator: PythonAudioSeparatorEngine,
        composer: VideoComposer,
        *,
        work_root: Path,
        keep_failed_work: bool = False,
    ) -> None:
        self.extractor = extractor
        self.separator = separator
        self.composer = composer
        self.work_root = work_root
        self.keep_failed_work = keep_failed_work

    def process(
        self,
        source_video: Path,
        output_video: Path,
        *,
        job_id: str,
        model: str,
        prefer_video_copy: bool,
        progress: ProgressCallback | None = None,
        device: str = "auto",
    ) -> VideoProcessResult:
        started = time.monotonic()
        job_work_dir = self.work_root / job_id
        separation_dir = job_work_dir / "separated"
        source_audio = job_work_dir / "source.wav"
        job_work_dir.mkdir(parents=True, exist_ok=True)
        succeeded = False
        try:
            if progress:
                progress("extracting_audio", f"Extracting temporary audio from {source_video.name}")
            self.extractor.extract_audio(source_video, source_audio)

            if progress:
                progress("separating_vocals", f"Separating vocals from {source_video.name}")
            vocals = self.separator.separate_vocals(
                source_audio,
                separation_dir,
                model,
                device=device,
            )

            if progress:
                progress("composing_video", f"Composing vocals-only video for {source_video.name}")
            used_video_copy = self.composer.compose(
                source_video,
                vocals,
                output_video,
                prefer_stream_copy=prefer_video_copy,
            )
            succeeded = True
            if progress:
                progress("completed", f"Completed {output_video.name}")
            return VideoProcessResult(
                source=source_video,
                output_video=output_video,
                duration_seconds=time.monotonic() - started,
                used_video_copy=used_video_copy,
            )
        finally:
            if succeeded or not self.keep_failed_work:
                shutil.rmtree(job_work_dir, ignore_errors=True)
