import logging
import zipfile
from pathlib import Path

from app.config import ServiceConfig
from app.models import VideoProcessResult
from app.report import ProcessingReport
from app.repository import ProcessingRepository
from app.runner import BatchRunner


class FakePipeline:
    def __init__(self) -> None:
        self.calls = 0

    def process(
        self,
        source: Path,
        output: Path,
        *,
        job_id: str,
        model: str,
        prefer_video_copy: bool,
        progress: object,
    ) -> VideoProcessResult:
        self.calls += 1
        progress("extracting_audio", "extracting")
        progress("separating_vocals", "separating")
        progress("composing_video", "composing")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"vocals-only-video")
        return VideoProcessResult(source, output, 1.25, prefer_video_copy)


def make_config(tmp_path: Path) -> ServiceConfig:
    return ServiceConfig.from_mapping(
        {
            "input_dir": str(tmp_path / "input"),
            "output_dir": str(tmp_path / "output"),
            "data_dir": str(tmp_path / "data"),
            "stable_seconds": 0,
            "model": "mdx",
        },
        base=tmp_path,
    )


def test_runner_processes_each_video_once_and_writes_report(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.prepare_directories()
    source = config.input_dir / "series" / "episode.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    repository = ProcessingRepository(config.database_path)
    pipeline = FakePipeline()
    runner = BatchRunner(
        config,
        repository,
        pipeline,
        ProcessingReport(config.report_path),
        logging.getLogger("test-runner"),
    )

    first = runner.run_once()
    second = runner.run_once()

    assert first["completed"] == 1
    assert second["completed"] == 0
    assert pipeline.calls == 1
    output = config.output_dir / "series" / "episode_vocals_only.mp4"
    assert output.read_bytes() == b"vocals-only-video"
    output.unlink()
    rebuilt = runner.run_once()
    assert rebuilt["completed"] == 1
    assert pipeline.calls == 2
    assert output.read_bytes() == b"vocals-only-video"
    assert zipfile.is_zipfile(config.report_path)
    record = repository.list_jobs()[0]
    assert record["status"] == "completed"
    assert record["output_path"] == str(output)
