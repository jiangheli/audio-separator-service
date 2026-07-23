import argparse
import asyncio
import json
import logging
import os
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Sequence

from app.cli.client import ServiceAPIClient, ServiceAPIError
from app.config import get_settings
from app.runtime import RuntimeDependencyError, ensure_ffmpeg
from app.services.extractor import AudioExtractor
from app.services.scanner import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, scan_media
from app.services.separator import AudioSeparatorService, MODEL_ALIASES, PythonAudioSeparatorEngine


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LOCAL = 3
EXIT_API = 4
EXIT_TASK = 5
EXIT_INTERNAL = 10
TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed"}
COMMANDS = {"capabilities", "separate", "health", "task", "agent"}


def cli_version() -> str:
    try:
        return version("audio-separator-service")
    except PackageNotFoundError:
        return "0.2.0"


class CLIError(RuntimeError):
    def __init__(self, message: str, code: int, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def envelope(action: str, *, data: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": error is None,
        "action": action,
        "data": data if error is None else None,
        "error": error,
        "meta": {"cli": "stemflow", "version": cli_version()},
    }


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def add_api_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-url",
        default=os.getenv("STEMFLOW_API_URL", "http://localhost:8000"),
        help="StemFlow API base URL (env: STEMFLOW_API_URL)",
    )
    parser.add_argument(
        "--public-api-url",
        default=os.getenv("STEMFLOW_PUBLIC_API_URL"),
        help="Base URL placed in result/playback links (env: STEMFLOW_PUBLIC_API_URL)",
    )
    parser.add_argument("--request-timeout", type=float, default=30, help="HTTP request timeout in seconds")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print one JSON envelope to stdout")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stemflow",
        description="AI-callable CLI for local vocal and instrumental separation",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {cli_version()}")
    subparsers = parser.add_subparsers(dest="command")

    capabilities = subparsers.add_parser("capabilities", help="Describe commands, models and JSON actions")
    capabilities.add_argument("--json", dest="json_output", action="store_true")

    separate = subparsers.add_parser("separate", help="Process a local file or folder synchronously")
    separate.add_argument("--input", "-i", required=True, help="Input file or directory")
    separate.add_argument("--output", "-o", required=True, help="Output directory")
    separate.add_argument("--model", "-m", default="default", help="Model alias or upstream model filename")
    separate.add_argument("--chunk-duration", type=float, default=None, help="Chunk size in seconds for long recordings")
    separate.add_argument("--json", dest="json_output", action="store_true", help="Print one JSON envelope to stdout")
    separate.add_argument("--verbose", action="store_true")

    health = subparsers.add_parser("health", help="Check the running service")
    add_api_options(health)

    task = subparsers.add_parser("task", help="Create, inspect, wait for and download service tasks")
    task_subparsers = task.add_subparsers(dest="task_command", required=True)

    task_list = task_subparsers.add_parser("list", help="List recent tasks")
    task_list.add_argument("--limit", type=int, default=50)
    add_api_options(task_list)

    create = task_subparsers.add_parser("create", help="Create an asynchronous task")
    create.add_argument("--input", "-i", required=True, help="Path visible inside the backend/worker")
    create.add_argument("--output", "-o", required=True, help="Output path visible inside the backend/worker")
    create.add_argument("--model", "-m", default="default")
    create.add_argument("--wait", action="store_true", help="Wait until the task reaches a terminal state")
    create.add_argument("--poll-interval", type=float, default=2)
    create.add_argument("--timeout", type=float, default=3600)
    create.add_argument("--include-files", action="store_true", help="Include result artifacts after waiting")
    add_api_options(create)

    for name, help_text in (
        ("get", "Get task progress"),
        ("files", "List result artifacts"),
        ("logs", "List task logs"),
    ):
        child = task_subparsers.add_parser(name, help=help_text)
        child.add_argument("task_id")
        add_api_options(child)

    wait = task_subparsers.add_parser("wait", help="Wait for a task and return its final state")
    wait.add_argument("task_id")
    wait.add_argument("--poll-interval", type=float, default=2)
    wait.add_argument("--timeout", type=float, default=3600)
    wait.add_argument("--include-files", action="store_true")
    add_api_options(wait)

    download = task_subparsers.add_parser("download", help="Download vocals/instrumental artifacts")
    download.add_argument("task_id")
    download.add_argument("--output", "-o", required=True, help="Local destination directory")
    download.add_argument(
        "--kind",
        action="append",
        choices=["vocals", "instrumental", "original"],
        help="Artifact kind; repeat to select multiple (default: vocals and instrumental)",
    )
    add_api_options(download)

    agent = subparsers.add_parser("agent", help="Execute one JSON request from an AI agent")
    source = agent.add_mutually_exclusive_group()
    source.add_argument("--request", help="Inline JSON object")
    source.add_argument("--request-file", help="JSON file path, or - for stdin")

    return parser


def capabilities_data() -> dict[str, Any]:
    models = {"default": "Runtime default configured by CPU/GPU deployment", **MODEL_ALIASES}
    return {
        "name": "StemFlow Audio Separator",
        "interface_version": "1.0",
        "supported_inputs": {
            "audio": sorted(extension.lstrip(".") for extension in AUDIO_EXTENSIONS),
            "video": sorted(extension.lstrip(".") for extension in VIDEO_EXTENSIONS),
        },
        "outputs": ["vocals.wav", "instrumental.wav"],
        "concurrency": {
            "environment": "AUDIO_SERVICE_WORKER_CONCURRENCY",
            "cpu_default": 2,
            "gpu_default": 1,
            "maximum": 16,
            "unit": "independent model inference slots",
        },
        "models": models,
        "commands": [
            "capabilities",
            "separate",
            "health",
            "task.list",
            "task.create",
            "task.get",
            "task.wait",
            "task.files",
            "task.logs",
            "task.download",
            "agent",
        ],
        "agent_actions": [
            "capabilities",
            "separate",
            "health",
            "task.list",
            "task.create",
            "task.get",
            "task.wait",
            "task.files",
            "task.logs",
            "task.download",
        ],
        "exit_codes": {
            "0": "success",
            "2": "invalid command or request",
            "3": "local input, dependency or inference failure",
            "4": "service API connection or HTTP failure",
            "5": "remote task failed or wait timed out",
            "10": "unexpected internal CLI error",
        },
    }


def make_client(args: argparse.Namespace) -> ServiceAPIClient:
    return ServiceAPIClient(args.api_url, timeout=args.request_timeout, public_base_url=args.public_api_url)


def require_positive(value: float, name: str) -> None:
    if value <= 0:
        raise CLIError(f"{name} must be greater than zero", EXIT_USAGE)


def wait_for_task(
    client: ServiceAPIClient,
    task_id: str,
    poll_interval: float,
    timeout: float,
    *,
    report_progress: bool,
) -> dict[str, Any]:
    require_positive(poll_interval, "poll_interval")
    require_positive(timeout, "timeout")
    deadline = time.monotonic() + timeout
    last_marker: tuple[Any, ...] | None = None
    while True:
        task = client.get(f"/api/tasks/{task_id}")
        marker = (task["status"], task["progress"], task.get("current_file"))
        if report_progress and marker != last_marker:
            print(
                f"[{task['progress']:5.1f}%] {task['status']} · {task.get('current_file') or '-'}",
                file=sys.stderr,
            )
            last_marker = marker
        if task["status"] in TERMINAL_STATUSES:
            if task["status"] == "failed":
                raise CLIError(
                    f"Task {task_id} failed: {task.get('error') or 'unknown error'}",
                    EXIT_TASK,
                    details={"task": task},
                )
            return task
        if time.monotonic() >= deadline:
            raise CLIError(f"Timed out waiting for task {task_id}", EXIT_TASK, details={"task": task})
        time.sleep(poll_interval)


async def run_local_separation(options: dict[str, Any], *, report_progress: bool) -> dict[str, Any]:
    settings = get_settings()
    ffmpeg_binary = ensure_ffmpeg()
    input_path = Path(str(options["input"])).expanduser().resolve()
    output_path = Path(str(options["output"])).expanduser().resolve()
    if not input_path.exists():
        raise CLIError(f"Input path does not exist: {input_path}", EXIT_LOCAL)
    media = scan_media(input_path)
    if not media:
        raise CLIError("No supported media files found", EXIT_LOCAL)
    verbose = bool(options.get("verbose", False))
    engine = PythonAudioSeparatorEngine(
        settings.model_dir,
        default_model=settings.default_model,
        output_format=settings.output_format,
        chunk_duration=options.get("chunk_duration") or settings.chunk_duration,
        mdx_segment_size=settings.mdx_segment_size,
        mdxc_segment_size=settings.mdxc_segment_size,
        mdxc_override_model_segment_size=settings.mdxc_override_model_segment_size,
        log_level=logging.DEBUG if verbose else logging.WARNING,
    )
    service = AudioSeparatorService(engine, AudioExtractor(ffmpeg_binary))

    def progress(item: Any, status: str, value: float, message: str) -> None:
        if report_progress:
            print(f"[{value:5.1f}%] {item.relative_path} · {status} · {message}", file=sys.stderr)

    started = time.monotonic()
    results = await service.process_folder(
        input_path,
        output_path,
        str(options.get("model", "default")),
        progress,
    )
    return {
        "input": str(input_path),
        "output": str(output_path),
        "model": str(options.get("model", "default")),
        "file_count": len(results),
        "duration_seconds": round(time.monotonic() - started, 3),
        "results": [result.model_dump(mode="json") for result in results],
    }


def remote_task_result(
    client: ServiceAPIClient,
    task: dict[str, Any],
    *,
    include_files: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {"task": task}
    if include_files and task["status"] in {"completed", "completed_with_errors"}:
        result["files"] = client.artifacts(task["id"])
    return result


def download_task_artifacts(
    client: ServiceAPIClient,
    task_id: str,
    output: str,
    kinds: list[str] | None,
) -> dict[str, Any]:
    destination = Path(output).expanduser().resolve()
    selected_kinds = set(kinds or ["vocals", "instrumental"])
    allowed_kinds = {"vocals", "instrumental", "original"}
    if not selected_kinds or not selected_kinds.issubset(allowed_kinds):
        raise CLIError("kind must contain vocals, instrumental, or original", EXIT_USAGE)
    artifacts = [record for record in client.artifacts(task_id) if record["kind"] in selected_kinds]
    if not artifacts:
        raise CLIError("No matching artifacts found", EXIT_TASK)
    downloaded = []
    for artifact in artifacts:
        target = destination / artifact["item_id"] / Path(artifact["name"]).name
        size = client.download(artifact["download_url"], target)
        downloaded.append({**artifact, "local_path": str(target), "downloaded_size": size})
    return {"task_id": task_id, "output": str(destination), "files": downloaded}


async def dispatch_agent(request: dict[str, Any]) -> tuple[str, Any]:
    action = str(request.get("action", "")).strip()
    if not action:
        raise CLIError("Agent request must include action", EXIT_USAGE)
    if action == "capabilities":
        return action, capabilities_data()
    if action == "separate":
        for key in ("input", "output"):
            if not request.get(key):
                raise CLIError(f"separate requires {key}", EXIT_USAGE)
        return action, await run_local_separation(request, report_progress=False)

    api_url = str(request.get("api_url") or os.getenv("STEMFLOW_API_URL", "http://localhost:8000"))
    public_api_url = request.get("public_api_url") or os.getenv("STEMFLOW_PUBLIC_API_URL")
    client = ServiceAPIClient(
        api_url,
        timeout=float(request.get("request_timeout", 30)),
        public_base_url=str(public_api_url) if public_api_url else None,
    )
    if action == "health":
        return action, client.get("/api/health")
    if action == "task.list":
        limit = int(request.get("limit", 50))
        if not 1 <= limit <= 200:
            raise CLIError("limit must be between 1 and 200", EXIT_USAGE)
        return action, client.get(f"/api/tasks?limit={limit}")
    if action == "task.create":
        input_value = request.get("input") or request.get("input_dir")
        output_value = request.get("output") or request.get("output_dir")
        if not input_value or not output_value:
            raise CLIError("task.create requires input and output", EXIT_USAGE)
        task = client.post(
            "/api/tasks",
            {"input_dir": str(input_value), "output_dir": str(output_value), "model": str(request.get("model", "default"))},
        )
        if request.get("wait"):
            task = wait_for_task(
                client,
                task["id"],
                float(request.get("poll_interval", 2)),
                float(request.get("timeout", 3600)),
                report_progress=False,
            )
        return action, remote_task_result(client, task, include_files=bool(request.get("include_files", False)))
    if action in {"task.get", "task.wait", "task.files", "task.logs", "task.download"}:
        task_id = str(request.get("task_id", "")).strip()
        if not task_id:
            raise CLIError(f"{action} requires task_id", EXIT_USAGE)
        if action == "task.get":
            return action, client.get(f"/api/tasks/{task_id}")
        if action == "task.files":
            return action, client.artifacts(task_id)
        if action == "task.logs":
            return action, client.get(f"/api/tasks/{task_id}/logs")
        if action == "task.download":
            output = str(request.get("output", "")).strip()
            if not output:
                raise CLIError("task.download requires output", EXIT_USAGE)
            kinds = request.get("kinds")
            if kinds is not None and not isinstance(kinds, list):
                raise CLIError("task.download kinds must be an array", EXIT_USAGE)
            return action, download_task_artifacts(client, task_id, output, kinds)
        task = wait_for_task(
            client,
            task_id,
            float(request.get("poll_interval", 2)),
            float(request.get("timeout", 3600)),
            report_progress=False,
        )
        return action, remote_task_result(client, task, include_files=bool(request.get("include_files", False)))
    raise CLIError(f"Unsupported agent action: {action}", EXIT_USAGE)


def read_agent_request(args: argparse.Namespace) -> dict[str, Any]:
    if args.request is not None:
        raw = args.request
    elif args.request_file:
        raw = sys.stdin.read() if args.request_file == "-" else Path(args.request_file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        raise CLIError("Provide --request, --request-file, or pipe a JSON object to stdin", EXIT_USAGE)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CLIError(f"Invalid JSON request: {error}", EXIT_USAGE) from error
    if not isinstance(value, dict):
        raise CLIError("Agent request must be a JSON object", EXIT_USAGE)
    return value


async def dispatch(args: argparse.Namespace) -> tuple[str, Any]:
    if args.command == "capabilities":
        return "capabilities", capabilities_data()
    if args.command == "separate":
        return "separate", await run_local_separation(vars(args), report_progress=not args.json_output)
    if args.command == "agent":
        return await dispatch_agent(read_agent_request(args))
    if args.command == "health":
        return "health", make_client(args).get("/api/health")
    if args.command != "task":
        raise CLIError("A command is required", EXIT_USAGE)

    client = make_client(args)
    if args.task_command == "list":
        if not 1 <= args.limit <= 200:
            raise CLIError("limit must be between 1 and 200", EXIT_USAGE)
        return "task.list", client.get(f"/api/tasks?limit={args.limit}")
    if args.task_command == "create":
        task = client.post(
            "/api/tasks",
            {"input_dir": args.input, "output_dir": args.output, "model": args.model},
        )
        if args.wait:
            task = wait_for_task(
                client,
                task["id"],
                args.poll_interval,
                args.timeout,
                report_progress=not args.json_output,
            )
        return "task.create", remote_task_result(client, task, include_files=args.include_files)
    if args.task_command == "get":
        return "task.get", client.get(f"/api/tasks/{args.task_id}")
    if args.task_command == "files":
        return "task.files", client.artifacts(args.task_id)
    if args.task_command == "logs":
        return "task.logs", client.get(f"/api/tasks/{args.task_id}/logs")
    if args.task_command == "wait":
        task = wait_for_task(
            client,
            args.task_id,
            args.poll_interval,
            args.timeout,
            report_progress=not args.json_output,
        )
        return "task.wait", remote_task_result(client, task, include_files=args.include_files)
    if args.task_command == "download":
        return "task.download", download_task_artifacts(client, args.task_id, args.output, args.kind)
    raise CLIError(f"Unsupported task command: {args.task_command}", EXIT_USAGE)


def render_text(action: str, data: Any) -> None:
    if action == "capabilities":
        print("StemFlow supports local separation and asynchronous service tasks.")
        print("Run `stemflow agent --request '{\"action\":\"capabilities\"}'` for the machine contract.")
    elif action == "separate":
        print(f"Completed {data['file_count']} file(s). Results: {data['output']}")
    elif action == "health":
        slots = data.get("worker_concurrency")
        suffix = f", {slots} inference slots" if slots else ""
        print(f"StemFlow API: {data['status']} ({data['task_mode']}{suffix})")
    elif action in {"task.create", "task.wait"}:
        task = data["task"]
        print(f"Task {task['id']}: {task['status']} · {task['progress']:.1f}%")
    elif action == "task.get":
        print(f"Task {data['id']}: {data['status']} · {data['progress']:.1f}%")
    elif action.startswith("task."):
        print_json(data)
    else:
        print_json(data)


def normalize_legacy_argv(argv: Sequence[str]) -> tuple[list[str], bool]:
    values = list(argv)
    if values and values[0] not in COMMANDS and values[0] not in {"-h", "--help", "--version"}:
        if "--input" in values or "-i" in values:
            return ["separate", *values], True
    return values, False


def main(argv: Sequence[str] | None = None) -> None:
    normalized, legacy = normalize_legacy_argv(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(normalized)
    if args.command is None:
        parser.print_help()
        raise SystemExit(EXIT_USAGE)
    json_output = args.command == "agent" or bool(getattr(args, "json_output", False))
    logging.basicConfig(
        level=logging.DEBUG if bool(getattr(args, "verbose", False)) else logging.WARNING,
        stream=sys.stderr,
    )
    action = args.command
    try:
        if args.command == "agent":
            request = read_agent_request(args)
            action = str(request.get("action") or "agent")
            action, data = asyncio.run(dispatch_agent(request))
        else:
            action, data = asyncio.run(dispatch(args))
        if legacy and args.command == "separate" and args.json_output:
            print_json(data["results"])
        elif json_output:
            print_json(envelope(action, data=data))
        else:
            render_text(action, data)
        raise SystemExit(EXIT_OK)
    except CLIError as error:
        code = error.code
        details = error.details
        message = str(error)
    except ServiceAPIError as error:
        code = EXIT_API
        details = {"status": error.status, "response": error.details}
        message = str(error)
    except (RuntimeDependencyError, OSError, ValueError) as error:
        code = EXIT_LOCAL
        details = None
        message = str(error)
    except Exception as error:  # pragma: no cover - last-resort process boundary
        logging.exception("Unexpected CLI error")
        code = EXIT_INTERNAL
        details = None
        message = str(error)
    error_value = {"code": code, "message": message, "details": details}
    if json_output:
        print_json(envelope(action, error=error_value))
    else:
        print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
