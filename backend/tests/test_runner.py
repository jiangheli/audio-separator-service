import logging
import threading
import time
import zipfile
from pathlib import Path

import pytest

from app.config import ServiceConfig
from app.models import VideoProcessResult
from app.report import ProcessingReport
from app.repository import ProcessingRepository
from app.runner import BatchRunner, ConcurrencyController
from app.services.concatenator import ConcatenationResult


class FakePipeline:
    def __init__(self) -> None:
        self.calls = 0
        self.devices: list[str] = []

    def process(
        self,
        source: Path,
        output: Path,
        *,
        job_id: str,
        model: str,
        prefer_video_copy: bool,
        progress: object,
        device: str = "auto",
    ) -> VideoProcessResult:
        assert device in {"auto", "cpu", "cuda"}
        self.calls += 1
        self.devices.append(device)
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


def test_runner_processes_jobs_with_selected_worker_count(tmp_path: Path) -> None:
    class ConcurrentPipeline(FakePipeline):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def process(self, *args: object, **kwargs: object) -> VideoProcessResult:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                return super().process(*args, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1

    config = make_config(tmp_path)
    config.prepare_directories()
    for index in range(4):
        (config.input_dir / f"episode-{index}.mp4").write_bytes(b"video")
    repository = ProcessingRepository(config.database_path)
    pipeline = ConcurrentPipeline()
    runner = BatchRunner(
        config,
        repository,
        pipeline,
        ProcessingReport(config.report_path),
        logging.getLogger("test-concurrent-runner"),
    )

    summary = runner.run_once(max_workers=3)

    assert summary["completed"] == 4
    assert summary["failed"] == 0
    assert pipeline.max_active == 3
    assert len(list(config.output_dir.glob("*_vocals_only.mp4"))) == 4


def test_runner_leaves_unclaimed_jobs_pending_after_stop(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.prepare_directories()
    for index in range(3):
        (config.input_dir / f"episode-{index}.mp4").write_bytes(b"video")
    repository = ProcessingRepository(config.database_path)
    pipeline = FakePipeline()
    runner = BatchRunner(
        config,
        repository,
        pipeline,
        ProcessingReport(config.report_path),
        logging.getLogger("test-stop-runner"),
    )
    stop_event = threading.Event()
    stop_event.set()

    summary = runner.run_once(max_workers=3, stop_event=stop_event)

    assert summary["completed"] == 0
    assert summary["failed"] == 0
    assert pipeline.calls == 0
    assert repository.counts()["pending"] == 3


def test_runner_applies_live_concurrency_increase(tmp_path: Path) -> None:
    first_started = threading.Event()
    allow_first_to_finish = threading.Event()

    class AdjustablePipeline(FakePipeline):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def process(self, *args: object, **kwargs: object) -> VideoProcessResult:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.calls == 0:
                    first_started.set()
            if self.calls == 0:
                allow_first_to_finish.wait(timeout=2)
            try:
                time.sleep(0.05)
                return super().process(*args, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1

    config = make_config(tmp_path)
    config.prepare_directories()
    for index in range(4):
        (config.input_dir / f"episode-{index}.mp4").write_bytes(b"video")
    repository = ProcessingRepository(config.database_path)
    pipeline = AdjustablePipeline()
    runner = BatchRunner(
        config,
        repository,
        pipeline,
        ProcessingReport(config.report_path),
        logging.getLogger("test-adjustable-runner"),
    )
    controller = ConcurrencyController(1, maximum=4)
    result: dict[str, object] = {}

    thread = threading.Thread(
        target=lambda: result.update(
            runner.run_once(
                max_workers=1,
                concurrency=controller,
            )
        )
    )
    thread.start()
    assert first_started.wait(timeout=2)
    controller.set(3)
    time.sleep(0.35)
    allow_first_to_finish.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result["completed"] == 4
    assert pipeline.max_active >= 2


def test_runner_routes_and_records_mixed_cpu_cuda_jobs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.prepare_directories()
    for index in range(4):
        (config.input_dir / f"mixed-{index}.mp4").write_bytes(b"video")
    repository = ProcessingRepository(config.database_path)
    pipeline = FakePipeline()
    runner = BatchRunner(
        config,
        repository,
        pipeline,
        ProcessingReport(config.report_path),
        logging.getLogger("test-mixed-runner"),
    )
    controller = ConcurrencyController(1, maximum=4)
    controller.set_allocation(cpu_workers=1, gpu_workers=1)

    summary = runner.run_once(
        max_workers=2,
        concurrency=controller,
    )

    assert summary["completed"] == 4
    assert {"cpu", "cuda"}.issubset(set(pipeline.devices))
    recorded_devices = {
        str(job["device"])
        for job in repository.list_jobs()
    }
    assert recorded_devices == {"cpu", "cuda"}


def test_controller_allows_zero_per_device_and_has_no_default_cap() -> None:
    controller = ConcurrencyController(1)

    assert controller.set_allocation(cpu_workers=0, gpu_workers=20) == (0, 20)
    assert controller.set_allocation(cpu_workers=30, gpu_workers=0) == (30, 0)

    with pytest.raises(ValueError, match="at least one worker"):
        controller.set_allocation(cpu_workers=0, gpu_workers=0)


def test_runner_expands_cuda_slots_for_pipeline_stages(tmp_path: Path) -> None:
    class PipelinedGpu(FakePipeline):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()
            self.warmed = 0

        def warm_gpu(self, workers: int) -> None:
            self.warmed = workers

        def scheduling_target(self, device: str, workers: int) -> int:
            return workers + 4 if device == "cuda" and workers else workers

        def process(self, *args: object, **kwargs: object) -> VideoProcessResult:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                return super().process(*args, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1

    config = make_config(tmp_path)
    config.prepare_directories()
    for index in range(5):
        (config.input_dir / f"pipeline-{index}.mp4").write_bytes(b"video")
    repository = ProcessingRepository(config.database_path)
    pipeline = PipelinedGpu()
    runner = BatchRunner(
        config,
        repository,
        pipeline,
        ProcessingReport(config.report_path),
        logging.getLogger("test-pipelined-gpu"),
    )
    controller = ConcurrencyController(1)
    controller.set_allocation(cpu_workers=0, gpu_workers=1)

    result = runner.run_once(
        max_workers=1,
        concurrency=controller,
    )

    assert result["completed"] == 5
    assert pipeline.warmed == 1
    assert pipeline.max_active >= 3
    assert set(pipeline.devices) == {"cuda"}


def test_runner_builds_folder_collections_after_processing(tmp_path: Path) -> None:
    class FakeConcatenator:
        def __init__(self) -> None:
            self.jobs: list[dict[str, object]] = []

        def concatenate_completed(self, jobs, **_options):
            self.jobs = list(jobs)
            return [
                ConcatenationResult(
                    Path("series"),
                    tmp_path / "output" / "series" / "series_合集_vocals_only.mp4",
                    2,
                    "completed",
                )
            ]

    config = make_config(tmp_path)
    config.concatenate_by_folder = True
    config.prepare_directories()
    for index in range(2):
        source = config.input_dir / "series" / f"episode-{index}.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"video")
    repository = ProcessingRepository(config.database_path)
    concatenator = FakeConcatenator()
    runner = BatchRunner(
        config,
        repository,
        FakePipeline(),
        ProcessingReport(config.report_path),
        logging.getLogger("test-collection-runner"),
        concatenator=concatenator,  # type: ignore[arg-type]
    )

    summary = runner.run_once()

    assert summary["completed"] == 2
    assert summary["collections_completed"] == 1
    assert summary["collections_failed"] == 0
    assert len(concatenator.jobs) == 2


def test_runner_does_not_process_jobs_from_previous_input_folder(
    tmp_path: Path,
) -> None:
    first_config = make_config(tmp_path)
    first_config.prepare_directories()
    first_source = first_config.input_dir / "old.mp4"
    first_source.write_bytes(b"old")
    repository = ProcessingRepository(first_config.database_path)
    stopped = threading.Event()
    stopped.set()
    BatchRunner(
        first_config,
        repository,
        FakePipeline(),
        ProcessingReport(first_config.report_path),
        logging.getLogger("test-first-root"),
    ).run_once(stop_event=stopped)

    second_config = ServiceConfig.from_mapping(
        {
            "input_dir": str(tmp_path / "input-2"),
            "output_dir": str(tmp_path / "output-2"),
            "data_dir": str(tmp_path / "data"),
            "stable_seconds": 0,
            "model": "mdx",
        },
        base=tmp_path,
    )
    second_config.prepare_directories()
    (second_config.input_dir / "new.mp4").write_bytes(b"new")
    pipeline = FakePipeline()
    summary = BatchRunner(
        second_config,
        repository,
        pipeline,
        ProcessingReport(second_config.report_path),
        logging.getLogger("test-second-root"),
    ).run_once()

    assert summary["scanned"] == 1
    assert summary["queued"] == 1
    assert summary["completed"] == 1
    assert pipeline.calls == 1
    assert repository.counts(first_config.input_dir)["pending"] == 1
    assert repository.counts(second_config.input_dir)["completed"] == 1


def test_runner_reduces_gpu_allocation_when_warmup_falls_back(
    tmp_path: Path,
) -> None:
    class FallbackGpuPipeline(FakePipeline):
        def warm_gpu(self, workers: int) -> int:
            assert workers == 2
            return 1

    config = make_config(tmp_path)
    config.prepare_directories()
    (config.input_dir / "episode.mp4").write_bytes(b"video")
    repository = ProcessingRepository(config.database_path)
    pipeline = FallbackGpuPipeline()
    controller = ConcurrencyController(1)
    controller.set_allocation(cpu_workers=0, gpu_workers=2)

    summary = BatchRunner(
        config,
        repository,
        pipeline,
        ProcessingReport(config.report_path),
        logging.getLogger("test-gpu-warm-fallback"),
    ).run_once(concurrency=controller)

    assert summary["completed"] == 1
    assert controller.get_allocation() == (0, 1)
    assert pipeline.devices == ["cuda"]


def test_runner_coalesces_excel_exports_instead_of_blocking_every_stage(
    tmp_path: Path,
) -> None:
    class CountingReport:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = threading.Lock()

        def export(self, _config, _jobs):
            with self.lock:
                self.calls += 1
            time.sleep(0.05)
            return tmp_path / "report.xlsx"

    config = make_config(tmp_path)
    config.prepare_directories()
    for index in range(8):
        (config.input_dir / f"episode-{index}.mp4").write_bytes(b"video")
    report = CountingReport()
    summary = BatchRunner(
        config,
        ProcessingRepository(config.database_path),
        FakePipeline(),
        report,  # type: ignore[arg-type]
        logging.getLogger("test-async-report"),
    ).run_once(max_workers=4)

    assert summary["completed"] == 8
    assert 1 <= report.calls <= 2
