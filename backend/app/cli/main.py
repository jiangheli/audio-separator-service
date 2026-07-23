from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Sequence

from app.config import ServiceConfig
from app.logging_setup import configure_logging
from app.process_lock import AlreadyRunningError
from app.report import ProcessingReport
from app.repository import ProcessingRepository
from app.runner import BatchRunner
from app.runtime import RuntimeDependencyError, ensure_ffmpeg
from app.services.composer import VideoComposer
from app.services.extractor import AudioExtractor
from app.services.separator import PythonAudioSeparatorEngine
from app.services.video_pipeline import VideoBgmRemovalPipeline


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUNTIME = 3
EXIT_INTERNAL = 10


def cli_version() -> str:
    try:
        return version("stemflow-video-bgm-remover")
    except PackageNotFoundError:
        return "1.0.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stemflow-video",
        description="Windows Server unattended video BGM removal",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {cli_version()}")
    commands = parser.add_subparsers(dest="command", required=True)

    run_once = commands.add_parser(
        "run-once",
        help="Scan the configured folder and process all eligible videos",
    )
    add_config_argument(run_once)
    run_once.add_argument("--max-files", type=int, default=0, help="0 processes all files")
    run_once.add_argument("--verbose", action="store_true")
    run_once.add_argument("--json", action="store_true", dest="json_output")

    status = commands.add_parser("status", help="Show processing counts and recent jobs")
    add_config_argument(status)
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--json", action="store_true", dest="json_output")

    validate = commands.add_parser("validate", help="Validate paths and runtime dependencies")
    add_config_argument(validate)
    validate.add_argument("--json", action="store_true", dest="json_output")

    report = commands.add_parser("report", help="Regenerate the Excel processing report")
    add_config_argument(report)
    report.add_argument("--json", action="store_true", dest="json_output")
    return parser


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default="config/stemflow.json",
        help="Path to the JSON configuration file",
    )


def make_runner(
    config: ServiceConfig,
    *,
    verbose: bool = False,
) -> tuple[BatchRunner, ProcessingRepository, logging.Logger]:
    config.prepare_directories()
    logger = configure_logging(config.log_dir, verbose=verbose)
    ffmpeg = ensure_ffmpeg()
    repository = ProcessingRepository(config.database_path)
    separator = PythonAudioSeparatorEngine(
        config.model_dir,
        default_model=config.model,
        log_level=logging.DEBUG if verbose else logging.INFO,
    )
    pipeline = VideoBgmRemovalPipeline(
        AudioExtractor(ffmpeg),
        separator,
        VideoComposer(ffmpeg, audio_bitrate=config.audio_bitrate),
        work_root=config.work_dir,
        keep_failed_work=config.keep_failed_work,
    )
    report = ProcessingReport(config.report_path)
    return BatchRunner(config, repository, pipeline, report, logger), repository, logger


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: bool(value) if key == "used_video_copy" and value is not None else value
        for key, value in job.items()
    }


def print_value(value: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    for key, item in value.items():
        if isinstance(item, dict):
            print(f"{key}:")
            for nested_key, nested_value in item.items():
                print(f"  {nested_key}: {nested_value}")
        elif isinstance(item, list):
            print(f"{key}:")
            for row in item:
                print(
                    "  "
                    + " | ".join(
                        str(row.get(name, ""))
                        for name in ("status", "relative_path", "output_path", "error")
                    )
                )
        else:
            print(f"{key}: {item}")


def execute(args: argparse.Namespace) -> int:
    config = ServiceConfig.load(args.config)
    if args.command == "validate":
        config.prepare_directories()
        ffmpeg = ensure_ffmpeg()
        if importlib.util.find_spec("audio_separator") is None:
            raise RuntimeDependencyError(
                "python-audio-separator is not installed in this environment"
            )
        value = {
            "ok": True,
            "config": str(Path(args.config).expanduser().resolve()),
            "input_dir": str(config.input_dir),
            "output_dir": str(config.output_dir),
            "ffmpeg": ffmpeg,
            "model": config.model,
            "schedule_time": config.schedule_time,
        }
        print_value(value, json_output=args.json_output)
        return EXIT_OK

    if args.command == "run-once":
        runner, _repository, _logger = make_runner(
            config,
            verbose=bool(getattr(args, "verbose", False)),
        )
        if args.max_files < 0:
            raise ValueError("--max-files cannot be negative")
        try:
            summary = runner.run_once(max_files=args.max_files)
        except AlreadyRunningError:
            summary = {
                "already_running": True,
                "message": "Another batch is already running; this scheduled invocation was skipped",
            }
        print_value(summary, json_output=args.json_output)
        return EXIT_OK

    config.prepare_directories()
    repository = ProcessingRepository(config.database_path)
    if args.command == "status":
        value = {
            "counts": repository.counts(),
            "recent_jobs": [
                public_job(job)
                for job in repository.list_jobs(max(1, min(args.limit, 1000)))
            ],
            "report_path": str(config.report_path),
            "log_path": str(config.log_dir / "stemflow-video.log"),
            "schedule_time": config.schedule_time,
        }
        print_value(value, json_output=args.json_output)
        return EXIT_OK

    if args.command == "report":
        report_path = ProcessingReport(config.report_path).export(
            config,
            repository.list_jobs(),
        )
        print_value(
            {"ok": True, "report_path": str(report_path)},
            json_output=args.json_output,
        )
        return EXIT_OK
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return execute(args)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except RuntimeDependencyError as error:
        print(f"Runtime error: {error}", file=sys.stderr)
        return EXIT_RUNTIME
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Unexpected error: {error}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
