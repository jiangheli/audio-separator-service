from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from app.models import VideoProcessResult
from app.services.video_pipeline import ProgressCallback, VideoBgmRemovalPipeline


EVENT_PREFIX = "STEMFLOW_EVENT\t"
GPU_REQUEST_IDLE_TIMEOUT_SECONDS = 180.0
GPU_ZERO_UTILIZATION_TIMEOUT_SECONDS = 180.0
_WORKER_REGISTRY: dict[
    tuple[str, str, str, int, int, int, int],
    "PersistentCudaWorker",
] = {}
_WORKER_REGISTRY_LOCK = threading.Lock()


class CudaWorkerTransportError(RuntimeError):
    """The resident CUDA process stopped responding or disconnected."""


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
        self._output_queue: queue.Queue[str | None] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._recent_output: deque[str] = deque(maxlen=40)
        self._request_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._current_request_id = ""
        self._current_audio = ""
        self._request_started_at = 0.0
        self._last_heartbeat_at = 0.0
        self._last_heartbeat: dict[str, object] = {}
        self._completed_requests = 0
        self._restarts = 0

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
            output_queue: queue.Queue[str | None] = queue.Queue()
            self._output_queue = output_queue
            self._recent_output.clear()
            self._reader_thread = threading.Thread(
                target=self._pump_output,
                args=(self.process, output_queue),
                daemon=True,
                name=f"stemflow-cuda-output-{self.index}",
            )
            self._reader_thread.start()
            try:
                event = self._read_event(
                    logger,
                    expected={"ready", "error"},
                    timeout_seconds=900,
                )
            except TimeoutError:
                self.close()
                raise RuntimeError(
                    f"CUDA worker {self.index + 1} did not finish loading "
                    "within 900 seconds. Reduce GPU concurrency, batch size, "
                    "or segment size and retry."
                ) from None
            if event.get("kind") != "ready":
                self.close()
                raise RuntimeError(
                    f"Persistent CUDA worker failed to start: "
                    f"{event.get('error', event)}"
                )
            logger.info(
                "CUDA worker %s ready; pid=%s; model=%s; device=%s; providers=%s; "
                "load=%.1fs; segment=%s; batch=%s; device VRAM=%.2f/%.2fGB",
                self.index + 1,
                self.process.pid,
                event.get("model"),
                event.get("device"),
                event.get("providers"),
                float(event.get("load_seconds", 0)),
                event.get("segment_size"),
                event.get("batch_size"),
                float(event.get("device_used_gb", 0)),
                float(event.get("device_total_gb", 0)),
            )

    @staticmethod
    def _pump_output(
        process: subprocess.Popen[str],
        output_queue: queue.Queue[str | None],
    ) -> None:
        if process.stdout is None:
            output_queue.put(None)
            return
        try:
            for raw_line in process.stdout:
                output_queue.put(raw_line.rstrip())
        finally:
            output_queue.put(None)

    def _read_event(
        self,
        logger: logging.Logger,
        *,
        expected: set[str],
        request_id: str | None = None,
        timeout_seconds: float | None = None,
        reset_timeout_on_activity: bool = False,
    ) -> dict[str, object]:
        process = self.process
        if process is None:
            raise RuntimeError("Persistent CUDA worker is not running")
        deadline = (
            time.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )
        zero_utilization_since: float | None = None
        while True:
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout == 0:
                    raise TimeoutError("CUDA worker event timed out")
            try:
                line = self._output_queue.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError("CUDA worker event timed out") from None
            if line is None:
                return_code = process.poll()
                raise CudaWorkerTransportError(
                    f"Persistent CUDA worker exited with {return_code}: "
                    + "\n".join(self._recent_output)
                )
            if not line:
                continue
            if reset_timeout_on_activity and timeout_seconds is not None:
                deadline = time.monotonic() + timeout_seconds
            self._recent_output.append(line)
            if not line.startswith(EVENT_PREFIX):
                logger.info("CUDA[%s] | %s", self.index + 1, line)
                continue
            try:
                event = json.loads(line[len(EVENT_PREFIX):])
            except json.JSONDecodeError:
                logger.warning("Malformed CUDA worker event: %s", line)
                continue
            if (
                event.get("kind") == "heartbeat"
                and request_id is not None
                and str(event.get("request_id", "")) == request_id
            ):
                with self._state_lock:
                    self._last_heartbeat_at = time.monotonic()
                    self._last_heartbeat = dict(event)
                logger.info(
                    "GPU-HEALTH | worker=%s; file=%s; elapsed=%ss; "
                    "utilization=%s%%; VRAM=%sGB",
                    self.index + 1,
                    Path(self._current_audio).name,
                    event.get("elapsed_seconds", "?"),
                    event.get("gpu_utilization", "?"),
                    event.get("memory_used_gb", "?"),
                )
                utilization = event.get("gpu_utilization")
                if isinstance(utilization, (int, float)):
                    if float(utilization) <= 1.0:
                        if zero_utilization_since is None:
                            zero_utilization_since = time.monotonic()
                        elif (
                            time.monotonic() - zero_utilization_since
                            >= GPU_ZERO_UTILIZATION_TIMEOUT_SECONDS
                        ):
                            raise TimeoutError(
                                "CUDA worker remained at 0% utilization for "
                                f"{GPU_ZERO_UTILIZATION_TIMEOUT_SECONDS:.0f}s"
                            )
                    else:
                        zero_utilization_since = None
                continue
            if (
                event.get("kind") in expected
                and (
                    request_id is None
                    or str(event.get("request_id", "")) == request_id
                )
            ):
                return event

    def separate(
        self,
        audio_path: Path,
        output_dir: Path,
        model: str,
        logger: logging.Logger,
    ) -> tuple[Path, dict[str, object]]:
        with self._request_lock:
            for attempt in range(2):
                try:
                    return self._separate_once(
                        audio_path,
                        output_dir,
                        model,
                        logger,
                    )
                except (
                    BrokenPipeError,
                    CudaWorkerTransportError,
                    OSError,
                    TimeoutError,
                ) as error:
                    self.close()
                    with self._state_lock:
                        self._restarts += 1
                    if attempt >= 1:
                        raise RuntimeError(
                            f"CUDA worker {self.index + 1} remained unavailable "
                            f"after an automatic restart: {error}"
                        ) from error
                    logger.warning(
                        "GPU-RECOVERY | worker=%s stopped responding while "
                        "processing %s (%s); restarting once and retrying",
                        self.index + 1,
                        audio_path.name,
                        error,
                    )
                    shutil.rmtree(output_dir, ignore_errors=True)
            raise AssertionError("unreachable")

    def _separate_once(
        self,
        audio_path: Path,
        output_dir: Path,
        model: str,
        logger: logging.Logger,
    ) -> tuple[Path, dict[str, object]]:
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
        with self._state_lock:
            self._current_request_id = request_id
            self._current_audio = str(audio_path)
            self._request_started_at = time.monotonic()
            self._last_heartbeat_at = self._request_started_at
            self._last_heartbeat = {}
        logger.info(
            "GPU-REQUEST | worker=%s; pid=%s; request=%s; file=%s; state=started",
            self.index + 1,
            self.process.pid,
            request_id[:8],
            audio_path.name,
        )
        try:
            self.process.stdin.write(
                json.dumps(request, ensure_ascii=False) + "\n"
            )
            self.process.stdin.flush()
            event = self._read_event(
                logger,
                expected={"result", "error"},
                request_id=request_id,
                timeout_seconds=GPU_REQUEST_IDLE_TIMEOUT_SECONDS,
                reset_timeout_on_activity=True,
            )
            if event.get("kind") == "error":
                raise RuntimeError(
                    str(event.get("error", "CUDA inference failed"))
                )
            with self._state_lock:
                self._completed_requests += 1
            logger.info(
                "GPU-REQUEST | worker=%s; request=%s; file=%s; "
                "state=completed; inference=%ss",
                self.index + 1,
                request_id[:8],
                audio_path.name,
                event.get("inference_seconds", "?"),
            )
            return Path(str(event["vocals"])), event
        finally:
            with self._state_lock:
                self._current_request_id = ""
                self._current_audio = ""
                self._request_started_at = 0.0

    def diagnostics(self) -> dict[str, object]:
        process = self.process
        now = time.monotonic()
        with self._state_lock:
            return {
                "index": self.index + 1,
                "pid": getattr(process, "pid", None)
                if process is not None
                else None,
                "alive": process is not None and process.poll() is None,
                "busy": bool(self._current_request_id),
                "file": Path(self._current_audio).name if self._current_audio else "",
                "request_seconds": round(now - self._request_started_at, 1)
                if self._request_started_at
                else 0.0,
                "heartbeat_age_seconds": round(now - self._last_heartbeat_at, 1)
                if self._last_heartbeat_at
                else None,
                "last_gpu_utilization": self._last_heartbeat.get(
                    "gpu_utilization"
                ),
                "last_vram_gb": self._last_heartbeat.get("memory_used_gb"),
                "completed_requests": self._completed_requests,
                "restarts": self._restarts,
            }

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

    def warm(self, count: int, logger: logging.Logger) -> int:
        self.set_size(count)
        started = 0
        for index in range(max(1, count)):
            try:
                self._worker(index).start(logger)
                started += 1
            except Exception as error:
                if started == 0:
                    raise
                self.set_size(started)
                logger.warning(
                    "CUDA worker %s could not start (%s). Continuing with "
                    "%s resident CUDA worker(s).",
                    index + 1,
                    error,
                    started,
                )
                return started
        return started

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
        queued_at = time.monotonic()
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
        queue_wait_seconds = time.monotonic() - queued_at
        if queue_wait_seconds >= 1.0:
            logger.info(
                "GPU-QUEUE | file=%s waited %.1fs for resident worker %s/%s",
                audio_path.name,
                queue_wait_seconds,
                available + 1,
                self._size,
            )
        try:
            vocals, metrics = self._worker(available).separate(
                audio_path,
                output_dir,
                model,
                logger,
            )
            metrics = dict(metrics)
            metrics["queue_wait_seconds"] = round(queue_wait_seconds, 3)
            metrics["worker_index"] = available + 1
            return vocals, metrics
        finally:
            should_close = False
            with self._condition:
                self._busy.remove(available)
                should_close = available >= self._size
                self._condition.notify()
            if should_close:
                self._worker(available).close()

    def diagnostics(self) -> dict[str, object]:
        with self._condition:
            size = self._size
            busy = sorted(index + 1 for index in self._busy)
        return {
            "size": size,
            "busy": busy,
            "workers": [
                self._worker(index).diagnostics()
                for index in range(size)
            ],
        }


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
        self._diagnostics_lock = threading.Lock()
        self._stages: dict[str, dict[str, Any]] = {}
        self._performance: dict[str, float] = {
            "jobs": 0.0,
            "prepare": 0.0,
            "gpu_queue": 0.0,
            "cuda": 0.0,
            "compose_queue": 0.0,
            "compose": 0.0,
            "total": 0.0,
            "gpu_utilization": 0.0,
            "gpu_utilization_samples": 0.0,
        }
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

    def warm_gpu(self, workers: int) -> int:
        if workers > 0 and self._cuda_pool is not None:
            return self._cuda_pool.warm(workers, self.logger)
        return 0

    def _set_stage(self, job_id: str, filename: str, stage: str) -> None:
        with self._diagnostics_lock:
            self._stages[job_id] = {
                "file": filename,
                "stage": stage,
                "started_at": time.monotonic(),
            }

    def _clear_stage(self, job_id: str) -> None:
        with self._diagnostics_lock:
            self._stages.pop(job_id, None)

    def diagnostics_snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        with self._diagnostics_lock:
            stages = [dict(value) for value in self._stages.values()]
        stage_counts: dict[str, int] = {}
        oldest: dict[str, object] | None = None
        for stage in stages:
            name = str(stage["stage"])
            stage_counts[name] = stage_counts.get(name, 0) + 1
            seconds = now - float(stage["started_at"])
            if oldest is None or seconds > float(oldest["seconds"]):
                oldest = {
                    "file": stage["file"],
                    "stage": name,
                    "seconds": round(seconds, 1),
                }
        pool_diagnostics = getattr(self._cuda_pool, "diagnostics", None)
        return {
            "stages": stage_counts,
            "oldest": oldest,
            "cuda_pool": pool_diagnostics()
            if callable(pool_diagnostics)
            else None,
        }

    def _record_performance(
        self,
        *,
        prepare_seconds: float,
        gpu_queue_seconds: float,
        inference_seconds: float,
        compose_queue_seconds: float,
        compose_seconds: float,
        total_seconds: float,
        gpu_utilization: object,
    ) -> None:
        with self._diagnostics_lock:
            self._performance["jobs"] += 1
            self._performance["prepare"] += prepare_seconds
            self._performance["gpu_queue"] += gpu_queue_seconds
            self._performance["cuda"] += inference_seconds
            self._performance["compose_queue"] += compose_queue_seconds
            self._performance["compose"] += compose_seconds
            self._performance["total"] += total_seconds
            if isinstance(gpu_utilization, (int, float)):
                self._performance["gpu_utilization"] += float(gpu_utilization)
                self._performance["gpu_utilization_samples"] += 1

    def performance_summary(self) -> dict[str, object]:
        with self._diagnostics_lock:
            values = dict(self._performance)
        jobs = int(values["jobs"])
        if jobs == 0:
            return {"gpu_jobs": 0}
        stage_averages = {
            name: round(values[name] / jobs, 2)
            for name in (
                "prepare",
                "gpu_queue",
                "cuda",
                "compose_queue",
                "compose",
                "total",
            )
        }
        bottleneck = max(
            ("prepare", "gpu_queue", "cuda", "compose_queue", "compose"),
            key=lambda name: stage_averages[name],
        )
        utilization_samples = int(values["gpu_utilization_samples"])
        average_utilization = (
            round(
                values["gpu_utilization"] / utilization_samples,
                1,
            )
            if utilization_samples
            else None
        )
        if average_utilization is None:
            analysis = "GPU telemetry unavailable; inspect GPU-HEALTH log lines"
        elif average_utilization < 20 and stage_averages["prepare"] > stage_averages["cuda"]:
            analysis = (
                "GPU is starved by audio preparation; increase prepare threads "
                "or check source-disk throughput"
            )
        elif average_utilization < 20:
            analysis = (
                "CUDA occupancy is low during inference; use one resident GPU "
                "worker and tune batch/segment within available VRAM"
            )
        elif bottleneck == "gpu_queue":
            analysis = (
                "GPU queue is the bottleneck; do not add workers unless VRAM "
                "can hold another resident model"
            )
        elif bottleneck in {"compose", "compose_queue"}:
            analysis = (
                "video composition is the bottleneck; verify stream-copy is "
                "being used and source/output disks are fast"
            )
        else:
            analysis = f"largest average stage is {bottleneck}"
        pool_reader = getattr(self._cuda_pool, "diagnostics", None)
        pool_diagnostics = (
            pool_reader()
            if callable(pool_reader)
            else {"workers": []}
        )
        restart_count = sum(
            int(worker.get("restarts", 0))
            for worker in pool_diagnostics.get("workers", [])
            if isinstance(worker, dict)
        )
        return {
            "gpu_jobs": jobs,
            **{f"avg_{key}_seconds": value for key, value in stage_averages.items()},
            "avg_gpu_utilization": average_utilization,
            "bottleneck": bottleneck,
            "worker_restarts": restart_count,
            "analysis": analysis,
        }

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
        prepare_slot_wait_seconds = 0.0
        extract_queue_seconds = 0.0
        extraction_seconds = 0.0
        gpu_queue_seconds = 0.0
        inference_seconds = 0.0
        compose_queue_seconds = 0.0
        compose_seconds = 0.0
        try:
            self._set_stage(
                job_id,
                source_video.name,
                "waiting_prepare_slot",
            )
            stage_started = time.monotonic()
            with self._prepared_slots:
                prepare_slot_wait_seconds = time.monotonic() - stage_started
                if progress:
                    progress(
                        "extracting_audio",
                        f"CPU preparing audio for {source_video.name}",
                    )
                self._set_stage(
                    job_id,
                    source_video.name,
                    "waiting_audio_extract",
                )
                stage_started = time.monotonic()
                with self._extract_slots:
                    extract_queue_seconds = time.monotonic() - stage_started
                    self._set_stage(
                        job_id,
                        source_video.name,
                        "extracting_audio",
                    )
                    stage_started = time.monotonic()
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
                self._set_stage(
                    job_id,
                    source_video.name,
                    "separating_vocals",
                )
                stage_started = time.monotonic()
                vocals, metrics = self._cuda_pool.separate(
                    source_audio,
                    separation_dir,
                    model,
                    self.logger,
                )
                parent_inference_seconds = time.monotonic() - stage_started
                gpu_queue_seconds = float(metrics.get("queue_wait_seconds", 0.0))
                inference_seconds = float(
                    metrics.get(
                        "inference_seconds",
                        max(0.0, parent_inference_seconds - gpu_queue_seconds),
                    )
                )
                self.logger.info(
                    "%s | CUDA queue %.1fs, inference %.1fs; worker=%s; "
                    "GPU utilization avg/peak=%s/%s%%; "
                    "device VRAM=%s/%sGB; PyTorch peak allocated/reserved=%s/%sGB",
                    source_video.name,
                    gpu_queue_seconds,
                    inference_seconds,
                    metrics.get("worker_index", "?"),
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
            self._set_stage(
                job_id,
                source_video.name,
                "waiting_video_compose",
            )
            stage_started = time.monotonic()
            with self._compose_slots:
                compose_queue_seconds = time.monotonic() - stage_started
                self._set_stage(
                    job_id,
                    source_video.name,
                    "composing_video",
                )
                stage_started = time.monotonic()
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
                "%s | PIPELINE timing: prepare-slot=%.1fs, extract-queue=%.1fs, "
                "extract=%.1fs, GPU-queue=%.1fs, CUDA=%.1fs, "
                "compose-queue=%.1fs, compose=%.1fs, total=%.1fs",
                source_video.name,
                prepare_slot_wait_seconds,
                extract_queue_seconds,
                extraction_seconds,
                gpu_queue_seconds,
                inference_seconds,
                compose_queue_seconds,
                compose_seconds,
                total,
            )
            self._record_performance(
                prepare_seconds=(
                    prepare_slot_wait_seconds
                    + extract_queue_seconds
                    + extraction_seconds
                ),
                gpu_queue_seconds=gpu_queue_seconds,
                inference_seconds=inference_seconds,
                compose_queue_seconds=compose_queue_seconds,
                compose_seconds=compose_seconds,
                total_seconds=total,
                gpu_utilization=metrics.get("gpu_utilization_avg"),
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
            self._clear_stage(job_id)
            if succeeded or not self.keep_failed_work:
                shutil.rmtree(job_work_dir, ignore_errors=True)
