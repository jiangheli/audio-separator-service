from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

from app.runtime import ensure_ffmpeg
from app.services.hybrid_pipeline import EVENT_PREFIX
from app.services.separator import PythonAudioSeparatorEngine


def _event(value: dict[str, object]) -> None:
    print(
        EVENT_PREFIX + json.dumps(value, ensure_ascii=False),
        flush=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--segment-size", type=int, default=256)
    return parser


def _configure_runtime(cpu_threads: int) -> tuple[object, object]:
    threads = max(1, cpu_threads)
    value = str(threads)
    os.environ["OMP_NUM_THREADS"] = value
    os.environ["MKL_NUM_THREADS"] = value
    os.environ["OPENBLAS_NUM_THREADS"] = value
    import onnxruntime as ort
    import torch

    torch.set_num_threads(threads)
    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA PyTorch cannot access an NVIDIA GPU")
    providers = ort.get_available_providers()
    if "CUDAExecutionProvider" not in providers:
        raise RuntimeError("ONNX Runtime CUDAExecutionProvider is unavailable")
    return torch, ort


def _vram_metrics(torch: object) -> dict[str, float]:
    free_bytes, total_bytes = torch.cuda.mem_get_info(0)
    divisor = 1024**3
    return {
        "device_used_gb": round((total_bytes - free_bytes) / divisor, 3),
        "device_total_gb": round(total_bytes / divisor, 3),
        "torch_allocated_gb": round(
            torch.cuda.memory_allocated(0) / divisor,
            3,
        ),
        "torch_reserved_gb": round(
            torch.cuda.memory_reserved(0) / divisor,
            3,
        ),
        "torch_peak_allocated_gb": round(
            torch.cuda.max_memory_allocated(0) / divisor,
            3,
        ),
        "torch_peak_reserved_gb": round(
            torch.cuda.max_memory_reserved(0) / divisor,
            3,
        ),
    }


class _NvidiaSampler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._samples: list[tuple[float, float]] = []
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="stemflow-gpu-monitor",
        )

    def start(self) -> None:
        self._thread.start()

    def finish(self) -> dict[str, float]:
        self._stop.set()
        self._thread.join(timeout=2)
        if not self._samples:
            return {}
        utilization = [sample[0] for sample in self._samples]
        memory = [sample[1] for sample in self._samples]
        return {
            "gpu_utilization_avg": round(sum(utilization) / len(utilization), 1),
            "gpu_utilization_peak": round(max(utilization), 1),
            "nvidia_smi_peak_memory_gb": round(max(memory) / 1024, 3),
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=utilization.gpu,memory.used",
                        "--format=csv,noheader,nounits",
                        "--id=0",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
                first_line = result.stdout.strip().splitlines()[0]
                utilization, memory = (
                    float(value.strip()) for value in first_line.split(",")[:2]
                )
                self._samples.append((utilization, memory))
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                return
            self._stop.wait(0.75)


def _serve(args: argparse.Namespace) -> int:
    torch, ort = _configure_runtime(args.cpu_threads)
    ensure_ffmpeg()
    engine = PythonAudioSeparatorEngine(
        Path(args.model_dir),
        default_model=args.model,
        mdx_segment_size=max(32, args.segment_size),
        batch_size=max(1, args.batch_size),
    )
    started = time.monotonic()
    engine.load_model(args.model, device="cuda")
    _event(
        {
            "kind": "ready",
            "model": engine.resolve_model(args.model),
            "device": torch.cuda.get_device_name(0),
            "providers": ort.get_available_providers(),
            "load_seconds": time.monotonic() - started,
            "batch_size": max(1, args.batch_size),
            "segment_size": max(32, args.segment_size),
            **_vram_metrics(torch),
        }
    )
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        request: dict[str, object] = {}
        try:
            request = json.loads(raw_line)
            if request.get("kind") == "shutdown":
                _event({"kind": "stopped"})
                return 0
            request_id = str(request["request_id"])
            audio = Path(str(request["audio_path"]))
            output_dir = Path(str(request["output_dir"]))
            model = str(request.get("model") or args.model)
            inference_started = time.monotonic()
            torch.cuda.reset_peak_memory_stats(0)
            sampler = _NvidiaSampler()
            sampler.start()
            try:
                vocals = engine.separate_vocals(
                    audio,
                    output_dir,
                    model,
                    device="cuda",
                )
                torch.cuda.synchronize(0)
            finally:
                utilization_metrics = sampler.finish()
            _event(
                {
                    "kind": "result",
                    "request_id": request_id,
                    "vocals": str(vocals),
                    "inference_seconds": time.monotonic() - inference_started,
                    **_vram_metrics(torch),
                    **utilization_metrics,
                }
            )
        except Exception as error:
            logging.exception("Persistent CUDA request failed")
            error_message = f"{type(error).__name__}: {error}"
            if "out of memory" in str(error).lower():
                try:
                    torch.cuda.empty_cache()
                except RuntimeError:
                    pass
                error_message += (
                    f"；CUDA 显存不足，请把 GPU 批量从 {args.batch_size} 调低，"
                    f"或把 GPU 分块从 {args.segment_size} 调低后重试"
                )
            _event(
                {
                    "kind": "error",
                    "request_id": str(request.get("request_id", "")),
                    "error": error_message,
                }
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        return _serve(args)
    except Exception as error:
        logging.exception("Persistent CUDA worker failed to start")
        _event(
            {
                "kind": "error",
                "request_id": "",
                "error": f"{type(error).__name__}: {error}",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
