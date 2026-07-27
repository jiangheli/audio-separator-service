from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Sequence

from app.runtime import ensure_ffmpeg
from app.services.composer import VideoComposer
from app.services.extractor import AudioExtractor
from app.services.hybrid_pipeline import EVENT_PREFIX
from app.services.separator import PythonAudioSeparatorEngine
from app.services.video_pipeline import VideoBgmRemovalPipeline


def _boolean(value: str) -> bool:
    return value == "1"


def _event(value: dict[str, object]) -> None:
    print(
        EVENT_PREFIX + json.dumps(value, ensure_ascii=False),
        flush=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--audio-bitrate", default="192k")
    parser.add_argument("--video-copy", default="1")
    parser.add_argument("--keep-failed-work", default="0")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    source = Path(args.source)
    output = Path(args.output)
    try:
        import torch
        import onnxruntime as ort

        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA PyTorch cannot access an NVIDIA GPU")
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise RuntimeError("ONNX Runtime CUDAExecutionProvider is unavailable")
        ffmpeg = ensure_ffmpeg()
        pipeline = VideoBgmRemovalPipeline(
            AudioExtractor(ffmpeg),
            PythonAudioSeparatorEngine(
                Path(args.model_dir),
                default_model=args.model,
            ),
            VideoComposer(ffmpeg, audio_bitrate=args.audio_bitrate),
            work_root=Path(args.work_dir),
            keep_failed_work=_boolean(args.keep_failed_work),
        )
        result = pipeline.process(
            source,
            output,
            job_id=args.job_id,
            model=args.model,
            prefer_video_copy=_boolean(args.video_copy),
            device="cuda",
            progress=lambda status, message: _event(
                {
                    "kind": "progress",
                    "status": status,
                    "message": message,
                }
            ),
        )
        _event(
            {
                "kind": "result",
                "output_video": str(result.output_video),
                "duration_seconds": result.duration_seconds,
                "used_video_copy": result.used_video_copy,
            }
        )
        return 0
    except Exception as error:
        logging.exception("CUDA worker failed for %s", source)
        _event(
            {
                "kind": "error",
                "error": f"{type(error).__name__}: {error}",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
