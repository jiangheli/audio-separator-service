from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from app.config import ServiceConfig
from app.process_lock import ProcessLock
from app.report import ProcessingReport
from app.repository import ProcessingRepository, utcnow
from app.services.scanner import (
    file_fingerprint,
    is_file_stable,
    output_path_for,
    scan_videos,
)
from app.services.video_pipeline import VideoBgmRemovalPipeline


class ConcurrencyController:
    """Thread-safe CPU/GPU worker allocation that can change during a batch."""

    def __init__(
        self,
        initial: int,
        *,
        maximum: int | None = None,
    ) -> None:
        if maximum is not None and maximum < 1:
            raise ValueError("maximum must be at least 1")
        self.maximum = maximum
        self._cpu_workers = 1
        self._gpu_workers = 0
        self._lock = threading.Lock()
        self.set(initial)

    def get(self) -> int:
        with self._lock:
            return self._cpu_workers + self._gpu_workers

    def get_allocation(self) -> tuple[int, int]:
        with self._lock:
            return self._cpu_workers, self._gpu_workers

    def set(self, value: int) -> int:
        selected = self.set_allocation(cpu_workers=value, gpu_workers=0)
        return sum(selected)

    def set_allocation(
        self,
        *,
        cpu_workers: int,
        gpu_workers: int,
    ) -> tuple[int, int]:
        cpu = int(cpu_workers)
        gpu = int(gpu_workers)
        total = cpu + gpu
        if cpu < 0 or gpu < 0 or total < 1:
            raise ValueError(
                "CPU and GPU concurrency must be non-negative, "
                "with at least one worker in total"
            )
        if self.maximum is not None and total > self.maximum:
            raise ValueError(
                f"combined concurrency must be between 1 and {self.maximum}"
            )
        with self._lock:
            self._cpu_workers = cpu
            self._gpu_workers = gpu
        return cpu, gpu


class BatchRunner:
    def __init__(
        self,
        config: ServiceConfig,
        repository: ProcessingRepository,
        pipeline: VideoBgmRemovalPipeline,
        report: ProcessingReport,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.repository = repository
        self.pipeline = pipeline
        self.report = report
        self.logger = logger
        self._report_lock = threading.Lock()

    def run_once(
        self,
        *,
        max_files: int = 0,
        max_workers: int = 1,
        stop_event: threading.Event | None = None,
        concurrency: ConcurrencyController | None = None,
    ) -> dict[str, Any]:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        route_devices = concurrency is not None
        controller = concurrency or ConcurrencyController(
            max_workers,
            maximum=max_workers,
        )
        self.config.prepare_directories()
        lock_path = self.config.data_dir / "stemflow-video.lock"
        with ProcessLock(lock_path):
            recovered = self.repository.recover_interrupted()
            if recovered:
                self.logger.warning(
                    "Recovered %s interrupted job(s) and marked them for retry",
                    recovered,
                )

            videos = scan_videos(
                self.config.input_dir,
                recursive=self.config.recursive,
                excluded_roots=(
                    self.config.output_dir,
                    self.config.work_dir,
                    self.config.model_dir,
                ),
            )
            discovered = 0
            waiting_copy = 0
            for video in videos:
                fingerprint = file_fingerprint(video)
                record = self.repository.get_by_fingerprint(fingerprint)
                stable = is_file_stable(video, self.config.stable_seconds)
                if record is None:
                    self.repository.register(
                        video,
                        fingerprint=fingerprint,
                        model=self.config.model,
                        output_path=output_path_for(
                            self.config.output_dir,
                            video,
                            self.config.output_suffix,
                        ),
                        status="pending" if stable else "waiting_copy",
                    )
                    discovered += 1
                    if not stable:
                        waiting_copy += 1
                    continue

                if record["status"] == "waiting_copy":
                    if stable:
                        self.repository.mark_ready(record["id"])
                    else:
                        waiting_copy += 1
                elif record["status"] == "completed":
                    if not Path(record["output_path"]).is_file():
                        self.repository.reset_for_rebuild(record["id"])

            jobs = self.repository.eligible(self.config.max_retries)
            if max_files > 0:
                jobs = jobs[:max_files]

            summary = {
                "scanned": len(videos),
                "discovered": discovered,
                "waiting_copy": waiting_copy,
                "queued": len(jobs),
                "completed": 0,
                "failed": 0,
                "recovered": recovered,
            }
            self.logger.info(
                "Scan complete: %s video(s), %s new, %s ready, %s still copying; "
                "worker count: %s",
                len(videos),
                discovered,
                len(jobs),
                waiting_copy,
                max_workers,
            )

            self._refresh_report()
            outcomes = self._run_jobs(
                jobs,
                controller=controller,
                stop_event=stop_event,
                route_devices=route_devices,
            )

            summary["completed"] = outcomes.count("completed")
            summary["failed"] = outcomes.count("failed")

            summary["counts"] = self.repository.counts()
            self.logger.info(
                "Batch finished: %s completed, %s failed",
                summary["completed"],
                summary["failed"],
            )
            return summary

    def _run_jobs(
        self,
        jobs: list[dict[str, Any]],
        *,
        controller: ConcurrencyController,
        stop_event: threading.Event | None,
        route_devices: bool,
    ) -> list[str]:
        """Run jobs while honoring live increases and decreases in concurrency."""
        if not jobs:
            return []
        outcomes: list[str] = []
        next_job = 0
        active: dict[Future[str], str] = {}
        executor_capacity = controller.maximum or max(1, len(jobs))
        with ThreadPoolExecutor(
            max_workers=executor_capacity,
            thread_name_prefix="stemflow-worker",
        ) as executor:
            while next_job < len(jobs) or active:
                stopping = stop_event is not None and stop_event.is_set()
                cpu_target, gpu_target = controller.get_allocation()
                active_cpu = sum(device == "cpu" for device in active.values())
                active_gpu = sum(device == "cuda" for device in active.values())
                for device, target, current in (
                    ("cuda", gpu_target, active_gpu),
                    ("cpu", cpu_target, active_cpu),
                ):
                    while (
                        not stopping
                        and next_job < len(jobs)
                        and current < target
                    ):
                        future = executor.submit(
                            self._claim_and_process,
                            jobs[next_job],
                            stop_event,
                            device if route_devices else None,
                        )
                        active[future] = device
                        next_job += 1
                        current += 1

                if not active:
                    break

                finished, _pending = wait(
                    set(active),
                    timeout=0.25,
                    return_when=FIRST_COMPLETED,
                )
                for future in finished:
                    outcomes.append(future.result())
                    active.pop(future, None)
        return outcomes

    def _claim_and_process(
        self,
        job: dict[str, Any],
        stop_event: threading.Event | None,
        device: str | None = None,
    ) -> str:
        if stop_event is not None and stop_event.is_set():
            return "skipped"
        claimed = self.repository.claim(job["id"], self.config.max_retries)
        if claimed is None:
            return "skipped"
        return self._process_claimed(claimed, device=device)

    def _process_claimed(
        self,
        claimed: dict[str, Any],
        *,
        device: str | None = None,
    ) -> str:
        started = time.monotonic()
        source = Path(claimed["source_path"])
        output = Path(claimed["output_path"])
        if device is not None:
            claimed = self.repository.update(
                claimed["id"],
                device=device,
            )
        self.logger.info(
            "Processing %s on %s (attempt %s/%s)",
            claimed["relative_path"],
            (device or "auto").upper(),
            claimed["attempts"],
            self.config.max_retries + 1,
        )

        def progress(status: str, message: str) -> None:
            self.repository.update(claimed["id"], status=status)
            self.logger.info("%s | %s", claimed["relative_path"], message)
            self._refresh_report()

        try:
            process_options = {
                "job_id": claimed["id"],
                "model": claimed["model"],
                "prefer_video_copy": self.config.video_copy,
                "progress": progress,
            }
            if device is not None:
                process_options["device"] = device
            result = self.pipeline.process(source, output, **process_options)
            self.repository.update(
                claimed["id"],
                status="completed",
                finished_at=utcnow(),
                duration_seconds=result.duration_seconds,
                used_video_copy=result.used_video_copy,
                output_path=str(result.output_video),
                error=None,
            )
            self.logger.info(
                "Completed %s -> %s in %.1fs",
                claimed["relative_path"],
                result.output_video,
                result.duration_seconds,
            )
            return "completed"
        except Exception as error:
            duration = time.monotonic() - started
            self.repository.update(
                claimed["id"],
                status="failed",
                finished_at=utcnow(),
                duration_seconds=duration,
                error=str(error)[:8000],
            )
            self.logger.exception(
                "Failed %s after %.1fs",
                claimed["relative_path"],
                duration,
            )
            return "failed"
        finally:
            self._refresh_report()

    def _refresh_report(self) -> None:
        with self._report_lock:
            try:
                self.report.export(self.config, self.repository.list_jobs())
            except OSError as error:
                self.logger.warning(
                    "Could not update Excel report (close it in Excel and retry): %s",
                    error,
                )
