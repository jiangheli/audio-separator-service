from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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
    }


def _serve(args: argparse.Namespace) -> int:
    torch, ort = _configure_runtime(args.cpu_threads)
    ensure_ffmpeg()
    engine = PythonAudioSeparatorEngine(
        Path(args.model_dir),
        default_model=args.model,
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
            vocals = engine.separate_vocals(
                audio,
                output_dir,
                model,
                device="cuda",
            )
            _event(
                {
                    "kind": "result",
                    "request_id": request_id,
                    "vocals": str(vocals),
                    "inference_seconds": time.monotonic() - inference_started,
                    **_vram_metrics(torch),
                }
            )
        except Exception as error:
            logging.exception("Persistent CUDA request failed")
            _event(
                {
                    "kind": "error",
                    "request_id": str(request.get("request_id", "")),
                    "error": f"{type(error).__name__}: {error}",
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
