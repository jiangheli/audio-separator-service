from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import VideoProcessResult
from app.services.hybrid_pipeline import (
    HybridVideoPipeline,
    PersistentCudaPool,
    PersistentCudaWorker,
)


class FakeExtractor:
    def extract_audio(self, _source: Path, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
        return target


class FakeSeparator:
    default_model = "model.onnx"


class FakeComposer:
    def compose(
        self,
        _source: Path,
        _vocals: Path,
        output: Path,
        *,
        prefer_stream_copy: bool,
        prefer_nvenc: bool = False,
    ) -> bool:
        assert prefer_nvenc is True
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")
        return prefer_stream_copy


class FakeLocalPipeline:
    def __init__(self) -> None:
        self.device = ""
        self.extractor = FakeExtractor()
        self.separator = FakeSeparator()
        self.composer = FakeComposer()

    def process(
        self,
        source: Path,
        output: Path,
        **options: object,
    ) -> VideoProcessResult:
        self.device = str(options["device"])
        return VideoProcessResult(source, output, 1.0, True)


class FakeCudaPool:
    def __init__(self) -> None:
        self.size = 1
        self.calls = 0

    def set_size(self, value: int) -> None:
        self.size = value

    def separate(
        self,
        _audio: Path,
        output_dir: Path,
        _model: str,
        _logger: logging.Logger,
    ) -> tuple[Path, dict[str, object]]:
        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        vocals = output_dir / "vocals.wav"
        vocals.write_bytes(b"vocals")
        return vocals, {
            "device_used_gb": 5.0,
            "device_total_gb": 8.0,
            "torch_allocated_gb": 1.0,
        }


def make_pipeline(tmp_path: Path, local: FakeLocalPipeline) -> HybridVideoPipeline:
    cuda_python = tmp_path / "python.exe"
    cuda_python.write_bytes(b"fake")
    pipeline = HybridVideoPipeline(
        local,  # type: ignore[arg-type]
        cuda_python=cuda_python,
        model_dir=tmp_path / "models",
        work_root=tmp_path / "work",
        audio_bitrate="192k",
        keep_failed_work=False,
        logger=logging.getLogger("test-hybrid-pipeline"),
        gpu_workers=1,
        prepare_threads=2,
        compose_threads=2,
        prefetch=2,
    )
    pipeline._cuda_pool = FakeCudaPool()  # type: ignore[assignment]
    return pipeline


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


def test_hybrid_pipeline_runs_three_stages_and_reuses_pool(tmp_path: Path) -> None:
    local = FakeLocalPipeline()
    pipeline = make_pipeline(tmp_path, local)
    progress: list[str] = []

    for index in range(2):
        source = tmp_path / f"source-{index}.mp4"
        source.write_bytes(b"source")
        result = pipeline.process(
            source,
            tmp_path / f"output-{index}.mp4",
            job_id=f"job-{index}",
            model="mdx",
            prefer_video_copy=True,
            device="cuda",
            progress=lambda status, _message: progress.append(status),
        )
        assert result.used_video_copy is True

    pool = pipeline._cuda_pool
    assert isinstance(pool, FakeCudaPool)
    assert pool.calls == 2
    assert progress == [
        "extracting_audio",
        "separating_vocals",
        "composing_video",
        "completed",
    ] * 2
    assert pipeline.scheduling_target("cuda", 1) == 5
    performance = pipeline.performance_summary()
    assert performance["gpu_jobs"] == 2
    assert "bottleneck" in performance


def test_cuda_worker_command_contains_gpu_tuning(tmp_path: Path) -> None:
    worker = PersistentCudaWorker(
        tmp_path / "python.exe",
        model_dir=tmp_path / "models",
        work_root=tmp_path / "work",
        model="mdx.onnx",
        cpu_threads=4,
        batch_size=3,
        segment_size=256,
        index=0,
    )

    command = worker._command()

    assert command[command.index("--batch-size") + 1] == "3"
    assert command[command.index("--segment-size") + 1] == "256"


def test_cuda_worker_event_read_has_a_timeout(tmp_path: Path) -> None:
    worker = PersistentCudaWorker(
        tmp_path / "python.exe",
        model_dir=tmp_path / "models",
        work_root=tmp_path / "work",
        model="mdx.onnx",
        cpu_threads=4,
        batch_size=1,
        segment_size=256,
        index=0,
    )
    worker.process = SimpleNamespace(poll=lambda: None)  # type: ignore[assignment]

    with pytest.raises(TimeoutError, match="timed out"):
        worker._read_event(
            logging.getLogger("test-cuda-timeout"),
            expected={"ready"},
            timeout_seconds=0.01,
        )


def test_cuda_heartbeat_extends_inactivity_timeout(tmp_path: Path) -> None:
    from app.services.hybrid_pipeline import EVENT_PREFIX

    worker = PersistentCudaWorker(
        tmp_path / "python.exe",
        model_dir=tmp_path / "models",
        work_root=tmp_path / "work",
        model="mdx.onnx",
        cpu_threads=4,
        batch_size=1,
        segment_size=256,
        index=0,
    )
    worker.process = SimpleNamespace(poll=lambda: None)  # type: ignore[assignment]

    def send_events() -> None:
        time.sleep(0.03)
        worker._output_queue.put(
            EVENT_PREFIX
            + json.dumps(
                {
                    "kind": "heartbeat",
                    "request_id": "request",
                    "elapsed_seconds": 10,
                    "gpu_utilization": 80,
                    "memory_used_gb": 5,
                }
            )
        )
        time.sleep(0.03)
        worker._output_queue.put(
            EVENT_PREFIX
            + json.dumps(
                {
                    "kind": "result",
                    "request_id": "request",
                    "vocals": "vocals.wav",
                }
            )
        )

    thread = threading.Thread(target=send_events)
    thread.start()
    event = worker._read_event(
        logging.getLogger("test-cuda-heartbeat"),
        expected={"result"},
        request_id="request",
        timeout_seconds=0.04,
        reset_timeout_on_activity=True,
    )
    thread.join(timeout=1)

    assert event["kind"] == "result"
    assert worker.diagnostics()["last_gpu_utilization"] == 80


def test_cuda_zero_utilization_is_detected_as_a_stall(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.services.hybrid_pipeline as hybrid_pipeline

    monkeypatch.setattr(
        hybrid_pipeline,
        "GPU_ZERO_UTILIZATION_TIMEOUT_SECONDS",
        0.01,
    )
    worker = PersistentCudaWorker(
        tmp_path / "python.exe",
        model_dir=tmp_path / "models",
        work_root=tmp_path / "work",
        model="mdx.onnx",
        cpu_threads=4,
        batch_size=1,
        segment_size=256,
        index=0,
    )
    worker.process = SimpleNamespace(poll=lambda: None)  # type: ignore[assignment]
    def heartbeat() -> str:
        return hybrid_pipeline.EVENT_PREFIX + json.dumps(
            {
                "kind": "heartbeat",
                "request_id": "request",
                "elapsed_seconds": 10,
                "gpu_utilization": 0,
                "memory_used_gb": 5,
            }
        )
    worker._output_queue.put(heartbeat())

    def send_second_heartbeat() -> None:
        time.sleep(0.02)
        worker._output_queue.put(heartbeat())

    thread = threading.Thread(target=send_second_heartbeat)
    thread.start()
    with pytest.raises(TimeoutError, match="0% utilization"):
        worker._read_event(
            logging.getLogger("test-cuda-zero-utilization"),
            expected={"result"},
            request_id="request",
            timeout_seconds=0.1,
            reset_timeout_on_activity=True,
        )
    thread.join(timeout=1)


def test_cuda_request_restarts_once_after_transport_timeout(
    tmp_path: Path,
) -> None:
    worker = PersistentCudaWorker(
        tmp_path / "python.exe",
        model_dir=tmp_path / "models",
        work_root=tmp_path / "work",
        model="mdx.onnx",
        cpu_threads=4,
        batch_size=1,
        segment_size=256,
        index=0,
    )
    calls = 0

    def separate_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("no heartbeat")
        vocals = tmp_path / "vocals.wav"
        vocals.write_bytes(b"vocals")
        return vocals, {"kind": "result"}

    worker._separate_once = separate_once  # type: ignore[method-assign]
    vocals, _metrics = worker.separate(
        tmp_path / "source.wav",
        tmp_path / "separated",
        "mdx.onnx",
        logging.getLogger("test-cuda-recovery"),
    )

    assert vocals.read_bytes() == b"vocals"
    assert calls == 2
    assert worker.diagnostics()["restarts"] == 1


def test_cuda_pool_keeps_started_workers_when_next_worker_fails(
    tmp_path: Path,
) -> None:
    class FakeWorker:
        def __init__(self, fails: bool = False) -> None:
            self.fails = fails
            self.closed = False

        def start(self, _logger: logging.Logger) -> None:
            if self.fails:
                raise RuntimeError("CUDA out of memory")

        def close(self) -> None:
            self.closed = True

    pool = PersistentCudaPool(
        tmp_path / "python.exe",
        model_dir=tmp_path / "models",
        work_root=tmp_path / "work",
        model="mdx.onnx",
        cpu_threads=4,
        batch_size=2,
        segment_size=256,
    )
    workers = [FakeWorker(), FakeWorker(fails=True)]
    pool._worker = lambda index: workers[index]  # type: ignore[method-assign]

    warmed = pool.warm(2, logging.getLogger("test-cuda-pool-fallback"))

    assert warmed == 1
    assert pool._size == 1
    assert workers[1].closed is True
