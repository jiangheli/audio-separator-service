from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from app.models import VideoProcessResult
from app.services.video_pipeline import ProgressCallback, VideoBgmRemovalPipeline


EVENT_PREFIX = "STEMFLOW_EVENT\t"
_WORKER_REGISTRY: dict[
    tuple[str, str, str, int, int, int, int],
    "PersistentCudaWorker",
] = {}
_WORKER_REGISTRY_LOCK = threading.Lock()


class PersistentCudaWorker:
    """One long-lived CUDA model process serving multiple audio requests."""

    def __init__(
        self,
        python: Path,
        *,
        model_dir: Path,
        work_root: Path,
        model: str,
        cpu_threads: int,
        batch_size: int,
        segment_size: int,
        index: int,
    ) -> None:
        self.python = python
        self.model_dir = model_dir
        self.work_root = work_root
        self.model = model
        self.cpu_threads = cpu_threads
        self.batch_size = batch_size
        self.segment_size = segment_size
        self.index = index
        self.process: subprocess.Popen[str] | None = None
        self._request_lock = threading.Lock()
        self._start_lock = threading.Lock()

    def _command(self) -> list[str]:
        return [
            str(self.python),
            "-m",
            "app.gpu_worker",
            "--serve",
            "--model",
            self.model,
            "--model-dir",
            str(self.model_dir),
            "--work-dir",
            str(self.work_root),
            "--cpu-threads",
            str(self.cpu_threads),
            "--batch-size",
            str(self.batch_size),
            "--segment-size",
            str(self.segment_size),
        ]

    def start(self, logger: logging.Logger) -> None:
        with self._start_lock:
            if self.process is not None and self.process.poll() is None:
                return
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            self.process = subprocess.Popen(
                self._command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            event = self._read_event(logger, expected={"ready", "error"})
            if event.get("kind") != "ready":
                self.close()
                raise RuntimeError(
                    f"Persistent CUDA worker failed to start: "
                    f"{event.get('error', event)}"
                )
            logger.info(
                "CUDA worker %s ready; model=%s; device=%s; providers=%s; "
                "load=%.1fs; segment=%s; batch=%s; device VRAM=%.2f/%.2fGB",
                self.index,
                event.get("model"),
                event.get("device"),
                event.get("providers"),
                float(event.get("load_seconds", 0)),
                event.get("segment_size"),
                event.get("batch_size"),
                float(event.get("device_used_gb", 0)),
                float(event.get("device_total_gb", 0)),
            )

    def _read_event(
        self,
        logger: logging.Logger,
        *,
        expected: set[str],
        request_id: str | None = None,
    ) -> dict[str, object]:
        process = self.process
        if process is None or process.stdout is None:
            raise RuntimeError("Persistent CUDA worker is not running")
        recent: deque[str] = deque(maxlen=40)
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            recent.append(line)
            if not line.startswith(EVENT_PREFIX):
                logger.info("CUDA[%s] | %s", self.index, line)
                continue
            try:
                event = json.loads(line[len(EVENT_PREFIX):])
            except json.JSONDecodeError:
                logger.warning("Malformed CUDA worker event: %s", line)
                continue
            if (
                event.get("kind") in expected
                and (
                    request_id is None
                    or str(event.get("request_id", "")) == request_id
                )
            ):
                return event
        return_code = process.poll()
        raise RuntimeError(
            f"Persistent CUDA worker exited with {return_code}: "
            + "\n".join(recent)
        )

    def separate(
        self,
        audio_path: Path,
        output_dir: Path,
        model: str,
        logger: logging.Logger,
    ) -> tuple[Path, dict[str, object]]:
        with self._request_lock:
            self.start(logger)
            assert self.process is not None and self.process.stdin is not None
            request_id = uuid.uuid4().hex
            request = {
                "kind": "separate",
                "request_id": request_id,
                "audio_path": str(audio_path),
                "output_dir": str(output_dir),
                "model": model,
            }
            try:
                self.process.stdin.write(
                    json.dumps(request, ensure_ascii=False) + "\n"
                )
                self.process.stdin.flush()
                event = self._read_event(
                    logger,
                    expected={"result", "error"},
                    request_id=request_id,
                )
            except (BrokenPipeError, OSError):
                self.close()
                raise
            if event.get("kind") == "error":
                raise RuntimeError(str(event.get("error", "CUDA inference failed")))
            return Path(str(event["vocals"])), event

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write('{"kind":"shutdown"}\n')
                process.stdin.flush()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def _close_registered_workers() -> None:
    with _WORKER_REGISTRY_LOCK:
        workers = list(_WORKER_REGISTRY.values())
        _WORKER_REGISTRY.clear()
    for worker in workers:
        worker.close()


atexit.register(_close_registered_workers)


class PersistentCudaPool:
    def __init__(
        self,
        python: Path,
        *,
        model_dir: Path,
        work_root: Path,
        model: str,
        cpu_threads: int,
        batch_size: int,
        segment_size: int,
    ) -> None:
        self.python = python
        self.model_dir = model_dir
        self.work_root = work_root
        self.model = model
        self.cpu_threads = cpu_threads
        self.batch_size = batch_size
        self.segment_size = segment_size
        self._condition = threading.Condition()
        self._busy: set[int] = set()
        self._size = 1
        self._retire_incompatible_workers()

    def set_size(self, value: int) -> None:
        close_indices: list[int] = []
        with self._condition:
            previous = self._size
            self._size = max(1, int(value))
            if self._size < previous:
                close_indices = [
                    index
                    for index in range(self._size, previous)
                    if index not in self._busy
                ]
            self._condition.notify_all()
        for index in close_indices:
            self._worker(index).close()

    def warm(self, count: int, logger: logging.Logger) -> None:
        self.set_size(count)
        for index in range(max(1, count)):
            self._worker(index).start(logger)

    def _retire_incompatible_workers(self) -> None:
        prefix = (str(self.python), str(self.model_dir), self.model)
        expected = (self.cpu_threads, self.batch_size, self.segment_size)
        retired: list[PersistentCudaWorker] = []
        with _WORKER_REGISTRY_LOCK:
            for key in list(_WORKER_REGISTRY):
                if key[:3] == prefix and key[3:6] != expected:
                    retired.append(_WORKER_REGISTRY.pop(key))
        for worker in retired:
            worker.close()

    def _worker(self, index: int) -> PersistentCudaWorker:
        key = (
            str(self.python),
            str(self.model_dir),
            self.model,
            self.cpu_threads,
            self.batch_size,
            self.segment_size,
            index,
        )
        with _WORKER_REGISTRY_LOCK:
            worker = _WORKER_REGISTRY.get(key)
            if worker is None:
                worker = PersistentCudaWorker(
                    self.python,
                    model_dir=self.model_dir,
                    work_root=self.work_root,
                    model=self.model,
                    cpu_threads=self.cpu_threads,
                    batch_size=self.batch_size,
                    segment_size=self.segment_size,
                    index=index,
                )
                _WORKER_REGISTRY[key] = worker
            return worker

    def separate(
        self,
        audio_path: Path,
        output_dir: Path,
        model: str,
        logger: logging.Logger,
    ) -> tuple[Path, dict[str, object]]:
        with self._condition:
            while True:
                available = next(
                    (
                        index
                        for index in range(self._size)
                        if index not in self._busy
                    ),
                    None,
                )
                if available is not None:
                    self._busy.add(available)
                    break
                self._condition.wait()
        try:
            return self._worker(available).separate(
                audio_path,
                output_dir,
                model,
                logger,
            )
        finally:
            should_close = False
            with self._condition:
                self._busy.remove(available)
                should_close = available >= self._size
                self._condition.notify()
            if should_close:
                self._worker(available).close()


class HybridVideoPipeline:
    """CPU preparation/composition around a persistent CUDA inference pool."""

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
        gpu_workers: int = 1,
        prepare_threads: int = 2,
        compose_threads: int = 2,
        prefetch: int = 2,
        gpu_cpu_threads: int = 4,
        gpu_batch_size: int = 2,
        gpu_segment_size: int = 256,
    ) -> None:
        self.local_pipeline = local_pipeline
        self.cuda_python = cuda_python
        self.model_dir = model_dir
        self.work_root = work_root
        self.audio_bitrate = audio_bitrate
        self.keep_failed_work = keep_failed_work
        self.logger = logger
        self.prepare_threads = max(1, prepare_threads)
        self.compose_threads = max(1, compose_threads)
        self.prefetch = max(0, prefetch)
        self._configured_gpu_workers = max(1, gpu_workers)
        self._extract_slots = threading.BoundedSemaphore(self.prepare_threads)
        self._compose_slots = threading.BoundedSemaphore(self.compose_threads)
        self._prepared_slots = threading.BoundedSemaphore(
            self._configured_gpu_workers + self.prefetch
        )
        self._cuda_pool = (
            PersistentCudaPool(
                cuda_python,
                model_dir=model_dir,
                work_root=work_root,
                model=local_pipeline.separator.default_model,
                cpu_threads=max(1, gpu_cpu_threads),
                batch_size=max(1, gpu_batch_size),
                segment_size=max(32, gpu_segment_size),
            )
            if cuda_python is not None
            else None
        )

    def scheduling_target(self, device: str, workers: int) -> int:
        if device != "cuda" or workers < 1:
            return workers
        if self._cuda_pool is not None:
            self._cuda_pool.set_size(workers)
        return workers + self.prefetch + self.compose_threads

    def warm_gpu(self, workers: int) -> None:
        if workers > 0 and self._cuda_pool is not None:
            self._cuda_pool.warm(workers, self.logger)

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
        if self._cuda_pool is None:
            raise RuntimeError(
                "CUDA worker was requested, but the NVIDIA runtime is unavailable"
            )
        return self._run_cuda_pipeline(
            source_video,
            output_video,
            job_id=job_id,
            model=model,
            prefer_video_copy=prefer_video_copy,
            progress=progress,
        )

    def _run_cuda_pipeline(
        self,
        source_video: Path,
        output_video: Path,
        *,
        job_id: str,
        model: str,
        prefer_video_copy: bool,
        progress: ProgressCallback | None,
    ) -> VideoProcessResult:
        started = time.monotonic()
        job_work_dir = self.work_root / job_id
        separation_dir = job_work_dir / "separated"
        source_audio = job_work_dir / "source.wav"
        job_work_dir.mkdir(parents=True, exist_ok=True)
        succeeded = False
        extraction_seconds = 0.0
        inference_seconds = 0.0
        compose_seconds = 0.0
        try:
            with self._prepared_slots:
                if progress:
                    progress(
                        "extracting_audio",
                        f"CPU preparing audio for {source_video.name}",
                    )
                stage_started = time.monotonic()
                with self._extract_slots:
                    self.local_pipeline.extractor.extract_audio(
                        source_video,
                        source_audio,
                    )
                extraction_seconds = time.monotonic() - stage_started

                if progress:
                    progress(
                        "separating_vocals",
                        f"Queued for resident CUDA model: {source_video.name}",
                    )
                stage_started = time.monotonic()
                vocals, metrics = self._cuda_pool.separate(
                    source_audio,
                    separation_dir,
                    model,
                    self.logger,
                )
                inference_seconds = time.monotonic() - stage_started
                self.logger.info(
                    "%s | CUDA inference %.1fs; GPU utilization avg/peak=%s/%s%%; "
                    "device VRAM=%s/%sGB; PyTorch peak allocated/reserved=%s/%sGB",
                    source_video.name,
                    inference_seconds,
                    metrics.get("gpu_utilization_avg", "?"),
                    metrics.get("gpu_utilization_peak", "?"),
                    metrics.get("device_used_gb", "?"),
                    metrics.get("device_total_gb", "?"),
                    metrics.get("torch_peak_allocated_gb", "?"),
                    metrics.get("torch_peak_reserved_gb", "?"),
                )

            if progress:
                progress(
                    "composing_video",
                    f"CPU composing vocals-only video for {source_video.name}",
                )
            stage_started = time.monotonic()
            with self._compose_slots:
                used_video_copy = self.local_pipeline.composer.compose(
                    source_video,
                    vocals,
                    output_video,
                    prefer_stream_copy=prefer_video_copy,
                    prefer_nvenc=True,
                )
            compose_seconds = time.monotonic() - stage_started
            succeeded = True
            total = time.monotonic() - started
            self.logger.info(
                "%s | pipeline timing: prepare=%.1fs, CUDA=%.1fs, "
                "compose=%.1fs, total=%.1fs",
                source_video.name,
                extraction_seconds,
                inference_seconds,
                compose_seconds,
                total,
            )
            if progress:
                progress("completed", f"Completed {output_video.name}")
            return VideoProcessResult(
                source=source_video,
                output_video=output_video,
                duration_seconds=total,
                used_video_copy=used_video_copy,
            )
        finally:
            if succeeded or not self.keep_failed_work:
                shutil.rmtree(job_work_dir, ignore_errors=True)
