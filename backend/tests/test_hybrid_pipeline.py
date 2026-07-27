from __future__ import annotations

import json
import logging
from pathlib import Path

from app.models import VideoProcessResult
from app.services.hybrid_pipeline import EVENT_PREFIX, HybridVideoPipeline


class FakeLocalPipeline:
    def __init__(self) -> None:
        self.device = ""

    def process(
        self,
        source: Path,
        output: Path,
        **options: object,
    ) -> VideoProcessResult:
        self.device = str(options["device"])
        return VideoProcessResult(source, output, 1.0, True)


def make_pipeline(tmp_path: Path, local: FakeLocalPipeline) -> HybridVideoPipeline:
    cuda_python = tmp_path / "python.exe"
    cuda_python.write_bytes(b"fake")
    return HybridVideoPipeline(
        local,  # type: ignore[arg-type]
        cuda_python=cuda_python,
        model_dir=tmp_path / "models",
        work_root=tmp_path / "work",
        audio_bitrate="192k",
        keep_failed_work=False,
        logger=logging.getLogger("test-hybrid-pipeline"),
    )


def test_hybrid_pipeline_keeps_cpu_work_local(tmp_path: Path) -> None:
    local = FakeLocalPipeline()
    pipeline = make_pipeline(tmp_path, local)

    pipeline.process(
        tmp_path / "source.mp4",
        tmp_path / "output.mp4",
        job_id="job",
        model="mdx",
        prefer_video_copy=True,
        device="cpu",
    )

    assert local.device == "cpu"


def test_hybrid_pipeline_streams_cuda_progress(tmp_path: Path, monkeypatch) -> None:
    events = [
        EVENT_PREFIX
        + json.dumps(
            {
                "kind": "progress",
                "status": "separating_vocals",
                "message": "GPU separating",
            }
        )
        + "\n",
        EVENT_PREFIX
        + json.dumps(
            {
                "kind": "result",
                "output_video": str(tmp_path / "output.mp4"),
                "duration_seconds": 2.5,
                "used_video_copy": True,
            }
        )
        + "\n",
    ]

    class FakeProcess:
        stdout = iter(events)

        @staticmethod
        def wait() -> int:
            return 0

    monkeypatch.setattr(
        "app.services.hybrid_pipeline.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    progress: list[tuple[str, str]] = []
    pipeline = make_pipeline(tmp_path, FakeLocalPipeline())

    result = pipeline.process(
        tmp_path / "source.mp4",
        tmp_path / "output.mp4",
        job_id="job",
        model="mdx",
        prefer_video_copy=True,
        device="cuda",
        progress=lambda status, message: progress.append((status, message)),
    )

    assert progress == [("separating_vocals", "GPU separating")]
    assert result.duration_seconds == 2.5
