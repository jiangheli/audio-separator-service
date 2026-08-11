from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

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
from app.services.concatenator import FolderVideoConcatenator
from app.services.video_pipeline import VideoBgmRemovalPipeline


class AsyncReportWriter:
    """Coalesce expensive full-workbook exports away from inference threads."""

    def __init__(
        self,
        exporter: Callable[[], int],
        logger: logging.Logger,
        *,
        minimum_interval_seconds: float = 60.0,
    ) -> None:
        self.exporter = exporter
        self.logger = logger
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._condition = threading.Condition()
        self._requested_generation = 0
        self._completed_generation = 0
        self._force_generation = 0
        # Do not compete with model warm-up and initial audio extraction.
        self._last_export_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="stemflow-report-writer",
        )
        self._thread.start()

    def request(self, *, force: bool = False) -> int:
        with self._condition:
            self._requested_generation += 1
            generation = self._requested_generation
            if force:
                self._force_generation = max(
                    self._force_generation,
                    generation,
                )
            self._condition.notify_all()
            return generation

    def flush(self, timeout_seconds: float = 300.0) -> bool:
        target = self.request(force=True)
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while self._completed_generation < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.logger.warning(
                        "REPORT | final workbook export did not finish within %.0fs",
                        timeout_seconds,
                    )
                    return False
                self._condition.wait(timeout=remaining)
        return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._requested_generation <= self._completed_generation:
                    self._condition.wait()
                generation = self._requested_generation
                forced = self._force_generation > self._completed_generation
                delay = (
                    self._last_export_at
                    + self.minimum_interval_seconds
                    - time.monotonic()
                )
                if delay > 0 and not forced:
                    self._condition.wait(timeout=delay)
                    continue

            started = time.monotonic()
            rows = 0
            try:
                rows = self.exporter()
                self.logger.info(
                    "REPORT | exported %s row(s) in %.1fs; updates were coalesced",
                    rows,
                    time.monotonic() - started,
                )
            except OSError as error:
                self.logger.warning(
                    "REPORT | could not update Excel workbook "
                    "(close it in Excel and retry): %s",
                    error,
                )
            except Exception:
                self.logger.exception("REPORT | background workbook export failed")
            finally:
                with self._condition:
                    self._completed_generation = max(
                        self._completed_generation,
                        generation,
                    )
                    self._last_export_at = time.monotonic()
                    self._condition.notify_all()


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
        *,
        concatenator: FolderVideoConcatenator | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.pipeline = pipeline
        self.report = report
        self.logger = logger
        self.concatenator = concatenator
        self._report_writer = AsyncReportWriter(
            self._export_report,
            logger,
        )

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
            existing_job_ids: list[str] = []
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
                        input_root=self.config.input_dir,
                    )
                    discovered += 1
                    if not stable:
                        waiting_copy += 1
                    continue

                existing_job_ids.append(str(record["id"]))
                if record["status"] == "waiting_copy":
                    if stable:
                        self.repository.mark_ready(record["id"])
                    else:
                        waiting_copy += 1
                elif record["status"] == "completed":
                    if not Path(record["output_path"]).is_file():
                        self.repository.reset_for_rebuild(record["id"])

            # A single transaction keeps first-run migration fast even for
            # libraries containing tens of thousands of videos.
            self.repository.assign_input_root_many(
                existing_job_ids,
                self.config.input_dir,
            )

            recovered = self.repository.recover_interrupted(
                self.config.input_dir
            )
            if recovered:
                self.logger.warning(
                    "Recovered %s interrupted job(s) in the selected input "
                    "folder and marked them for retry",
                    recovered,
                )

            jobs = self.repository.eligible(
                self.config.max_retries,
                self.config.input_dir,
            )
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
            _cpu_workers, gpu_workers = controller.get_allocation()
            warm_gpu = getattr(self.pipeline, "warm_gpu", None)
            if jobs and gpu_workers > 0 and callable(warm_gpu):
                self.logger.info(
                    "Warming %s persistent CUDA worker(s) before the batch",
                    gpu_workers,
                )
                warmed = warm_gpu(gpu_workers)
                if (
                    isinstance(warmed, int)
                    and 0 < warmed < gpu_workers
                ):
                    cpu_workers, _requested_gpu_workers = (
                        controller.get_allocation()
                    )
                    controller.set_allocation(
                        cpu_workers=cpu_workers,
                        gpu_workers=warmed,
                    )
                    self.logger.warning(
                        "GPU concurrency was reduced from %s to %s because "
                        "the requested resident models did not fit or start",
                        gpu_workers,
                        warmed,
                    )
            outcomes = self._run_jobs(
                jobs,
                controller=controller,
                stop_event=stop_event,
                route_devices=route_devices,
            )

            summary["completed"] = outcomes.count("completed")
            summary["failed"] = outcomes.count("failed")
            performance_reader = getattr(
                self.pipeline,
                "performance_summary",
                None,
            )
            if callable(performance_reader):
                performance = performance_reader()
                summary["performance"] = performance
                self.logger.info("PERFORMANCE | %s", performance)
            summary["collections_completed"] = 0
            summary["collections_failed"] = 0
            if (
                self.config.concatenate_by_folder
                and self.concatenator is not None
                and not (stop_event is not None and stop_event.is_set())
            ):
                _cpu_workers, gpu_workers = controller.get_allocation()
                collections = self.concatenator.concatenate_completed(
                    self.repository.list_jobs(
                        input_root=self.config.input_dir
                    ),
                    input_root=self.config.input_dir,
                    output_root=self.config.output_dir,
                    output_suffix=self.config.output_suffix,
                    prefer_nvenc=gpu_workers > 0,
                )
                for collection in collections:
                    folder = (
                        str(collection.folder)
                        if collection.folder != Path(".")
                        else self.config.input_dir.name
                    )
                    if collection.status == "completed":
                        summary["collections_completed"] += 1
                        self.logger.info(
                            "Collection completed: %s (%s video(s)) -> %s",
                            folder,
                            collection.input_count,
                            collection.output,
                        )
                    elif collection.status == "failed":
                        summary["collections_failed"] += 1
                        self.logger.error(
                            "Collection failed: %s (%s video(s)): %s",
                            folder,
                            collection.input_count,
                            collection.error,
                        )
                    else:
                        self.logger.info(
                            "Collection unchanged: %s -> %s",
                            folder,
                            collection.output,
                        )

            summary["counts"] = self.repository.counts(self.config.input_dir)
            self.logger.info(
                "Batch finished: %s completed, %s failed",
                summary["completed"],
                summary["failed"],
            )
            self._refresh_report(wait=True)
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
        batch_started = time.monotonic()
        last_health_log = 0.0
        last_completion_at = batch_started
        last_stall_warning = 0.0
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
                target_resolver = getattr(
                    self.pipeline,
                    "scheduling_target",
                    None,
                )
                if callable(target_resolver):
                    cpu_target = int(target_resolver("cpu", cpu_target))
                    gpu_target = int(target_resolver("cuda", gpu_target))
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

                now = time.monotonic()
                active_cpu = sum(device == "cpu" for device in active.values())
                active_gpu = sum(device == "cuda" for device in active.values())
                if now - last_health_log >= 15.0:
                    diagnostics_reader = getattr(
                        self.pipeline,
                        "diagnostics_snapshot",
                        None,
                    )
                    diagnostics = (
                        diagnostics_reader()
                        if callable(diagnostics_reader)
                        else None
                    )
                    self.logger.info(
                        "HEALTH | submitted=%s/%s; waiting=%s; "
                        "routed CPU/GPU=%s/%s; pipeline slots CPU/GPU=%s/%s; "
                        "seconds_since_completion=%.1f; pipeline=%s",
                        next_job,
                        len(jobs),
                        len(jobs) - next_job,
                        active_cpu,
                        active_gpu,
                        cpu_target,
                        gpu_target,
                        now - last_completion_at,
                        diagnostics,
                    )
                    last_health_log = now
                if (
                    active
                    and now - last_completion_at >= 300.0
                    and now - last_stall_warning >= 60.0
                ):
                    self.logger.warning(
                        "STALL-ANALYSIS | no video completed for %.1fs; "
                        "inspect the latest HEALTH and GPU-HEALTH lines to "
                        "identify extraction, CUDA, or composition blocking",
                        now - last_completion_at,
                    )
                    last_stall_warning = now

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
                    last_completion_at = time.monotonic()
        elapsed = time.monotonic() - batch_started
        self.logger.info(
            "THROUGHPUT | finished=%s/%s in %.1fs; average=%.2f videos/minute",
            len(outcomes),
            len(jobs),
            elapsed,
            (len(outcomes) * 60 / elapsed) if elapsed > 0 else 0.0,
        )
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

    def _export_report(self) -> int:
        jobs = self.repository.list_jobs(input_root=self.config.input_dir)
        self.report.export(self.config, jobs)
        return len(jobs)

    def _refresh_report(self, *, wait: bool = False) -> None:
        if wait:
            self._report_writer.flush()
        else:
            self._report_writer.request()
