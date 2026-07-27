from __future__ import annotations

import logging
import threading
from pathlib import Path


DEFAULT_MODEL = "UVR-MDX-NET-Inst_HQ_3.onnx"
MODEL_ALIASES = {
    "default": DEFAULT_MODEL,
    "mdx": DEFAULT_MODEL,
    "uvr": DEFAULT_MODEL,
    "roformer": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
    "clean_vocals": "Kim_Vocal_1.onnx",
    "vocals": "Kim_Vocal_1.onnx",
    "demucs": "htdemucs_ft.yaml",
}


class VocalSeparationError(RuntimeError):
    pass


class PythonAudioSeparatorEngine:
    """Thin adapter around python-audio-separator's public Python API."""

    def __init__(
        self,
        model_dir: Path,
        *,
        default_model: str = DEFAULT_MODEL,
        mdx_segment_size: int = 32,
        mdxc_segment_size: int = 256,
        log_level: int = logging.INFO,
    ) -> None:
        self.model_dir = model_dir
        self.default_model = MODEL_ALIASES.get(
            default_model.strip().lower(),
            default_model,
        )
        self.mdx_segment_size = mdx_segment_size
        self.mdxc_segment_size = mdxc_segment_size
        self.log_level = log_level
        self.logger = logging.getLogger("stemflow_video")
        self._thread_local = threading.local()

    def resolve_model(self, model: str) -> str:
        normalized = model.strip().lower()
        if normalized == "default":
            return self.default_model
        return MODEL_ALIASES.get(normalized, model.strip())

    def separate_vocals(
        self,
        audio_path: Path,
        output_dir: Path,
        model: str,
        *,
        device: str = "auto",
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        model_filename = self.resolve_model(model)
        selected_device = device.strip().lower()
        if selected_device not in {"auto", "cpu", "cuda"}:
            raise ValueError(f"Unsupported inference device: {device}")
        separators = getattr(self._thread_local, "separators", None)
        if separators is None:
            separators = {}
            self._thread_local.separators = separators
        cache_key = (model_filename, selected_device)
        separator = separators.get(cache_key)
        if separator is None:
            from audio_separator.separator import Separator

            separator = Separator(
                log_level=self.log_level,
                model_file_dir=str(self.model_dir),
                output_dir=str(output_dir),
                output_format="WAV",
                output_single_stem="Vocals",
                use_soundfile=True,
                mdx_params={
                    "hop_length": 1024,
                    "segment_size": self.mdx_segment_size,
                    "overlap": 0.25,
                    "batch_size": 1,
                    "enable_denoise": False,
                },
                mdxc_params={
                    "segment_size": self.mdxc_segment_size,
                    "override_model_segment_size": True,
                    "batch_size": 1,
                    "overlap": 8,
                    "pitch_shift": 0,
                },
            )
            self._select_device(separator, selected_device)
            separator.load_model(model_filename=model_filename)
            separators[cache_key] = separator
        else:
            separator.output_dir = str(output_dir)
            model_instance = getattr(separator, "model_instance", None)
            if model_instance is not None:
                model_instance.output_dir = str(output_dir)

        output_files = separator.separate(
            str(audio_path),
            custom_output_names={"Vocals": "vocals"},
        )

        for value in output_files:
            path = Path(value)
            if not path.is_absolute():
                path = output_dir / path
            if "vocal" in path.stem.lower() and path.is_file():
                return path.resolve()
        written_files = sorted(
            str(path.relative_to(output_dir))
            for path in output_dir.rglob("*")
            if path.is_file()
        )
        raise VocalSeparationError(
            f"The model completed but did not produce a vocals stem for "
            f"{audio_path.name}; returned={output_files!r}; "
            f"written_files={written_files!r}"
        )

    def _select_device(self, separator: object, device: str) -> None:
        """Select a public runtime device without changing upstream inference."""
        if device == "auto":
            return
        import onnxruntime as ort
        import torch

        if device == "cpu":
            cpu = torch.device("cpu")
            separator.torch_device = cpu
            separator.torch_device_cpu = cpu
            separator.onnx_execution_provider = ["CPUExecutionProvider"]
            self.logger.info("Inference device selected: CPU")
            return

        providers = ort.get_available_providers()
        if not torch.cuda.is_available():
            raise VocalSeparationError(
                "NVIDIA CUDA was selected, but CUDA PyTorch is unavailable"
            )
        if "CUDAExecutionProvider" not in providers:
            raise VocalSeparationError(
                "NVIDIA CUDA was selected, but ONNX Runtime CUDAExecutionProvider "
                "is unavailable"
            )
        separator.torch_device = torch.device("cuda")
        separator.onnx_execution_provider = ["CUDAExecutionProvider"]
        self.logger.info(
            "Inference device selected: CUDA (%s)",
            torch.cuda.get_device_name(0),
        )
