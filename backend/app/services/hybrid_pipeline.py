from __future__ import annotations

import json
import logging
import os
import subprocess
from collections import deque
from pathlib import Path

from app.models import VideoProcessResult
from app.services.video_pipeline import ProgressCallback, VideoBgmRemovalPipeline


EVENT_PREFIX = "STEMFLOW_EVENT\t"


class HybridVideoPipeline:
    """Use the local CPU pipeline or an isolated CUDA subprocess per job."""

    def __init__(
        self,
        local_pipeline: VideoBgmRemovalPipeline,
        *,
        cuda_python: Path | None,
        model_dir: Path,
        work_root: Path,
        audio_bitrate: str,
        keep_failed_work: bool,
        logger: logging.Logger,
    ) -> None:
        self.local_pipeline = local_pipeline
        self.cuda_python = cuda_python
        self.model_dir = model_dir
        self.work_root = work_root
        self.audio_bitrate = audio_bitrate
        self.keep_failed_work = keep_failed_work
        self.logger = logger

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
        if device != "cuda":
            return self.local_pipeline.process(
                source_video,
                output_video,
                job_id=job_id,
                model=model,
                prefer_video_copy=prefer_video_copy,
                progress=progress,
                device="cpu" if device == "cpu" else "auto",
            )
        if self.cuda_python is None or not self.cuda_python.is_file():
            raise RuntimeError(
                "CUDA worker was requested, but the NVIDIA runtime is unavailable"
            )
        return self._run_cuda(
            source_video,
            output_video,
            job_id=job_id,
            model=model,
            prefer_video_copy=prefer_video_copy,
            progress=progress,
        )

    def _run_cuda(
        self,
        source_video: Path,
        output_video: Path,
        *,
        job_id: str,
        model: str,
        prefer_video_copy: bool,
        progress: ProgressCallback | None,
    ) -> VideoProcessResult:
        command = [
            str(self.cuda_python),
            "-m",
            "app.gpu_worker",
            "--source",
            str(source_video),
            "--output",
            str(output_video),
            "--job-id",
            job_id,
            "--model",
            model,
            "--model-dir",
            str(self.model_dir),
            "--work-dir",
            str(self.work_root),
            "--audio-bitrate",
            self.audio_bitrate,
            "--video-copy",
            "1" if prefer_video_copy else "0",
            "--keep-failed-work",
            "1" if self.keep_failed_work else "0",
        ]
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdout is not None
        recent_lines: deque[str] = deque(maxlen=30)
        result_payload: dict[str, object] | None = None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            recent_lines.append(line)
            if not line.startswith(EVENT_PREFIX):
                self.logger.info("CUDA | %s", line)
                continue
            try:
                event = json.loads(line[len(EVENT_PREFIX):])
            except json.JSONDecodeError:
                self.logger.warning("Malformed CUDA worker event: %s", line)
                continue
            if event.get("kind") == "progress" and progress:
                progress(str(event["status"]), str(event["message"]))
            elif event.get("kind") == "result":
                result_payload = event

        return_code = process.wait()
        if return_code != 0 or result_payload is None:
            detail = "\n".join(recent_lines)
            raise RuntimeError(
                f"CUDA worker failed with exit code {return_code}: {detail}"
            )
        return VideoProcessResult(
            source=source_video,
            output_video=Path(str(result_payload["output_video"])),
            duration_seconds=float(result_payload["duration_seconds"]),
            used_video_copy=bool(result_payload["used_video_copy"]),
        )
