import asyncio
import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable

from app.models import ProcessResult
from app.services.extractor import AudioExtractor
from app.services.scanner import MediaFile, media_type_for, reserve_output_subdirectory, scan_media


DEFAULT_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
MODEL_ALIASES = {
    "roformer": DEFAULT_MODEL,
    "mdx": "UVR-MDX-NET-Inst_HQ_3.onnx",
    "uvr": "UVR-MDX-NET-Inst_HQ_3.onnx",
    "clean_vocals": "Kim_Vocal_1.onnx",
    "vocals": "Kim_Vocal_1.onnx",
    "demucs": "htdemucs_ft.yaml",
}

ProgressCallback = Callable[[str, float, str], None]


class PythonAudioSeparatorEngine:
    """Thin adapter around python-audio-separator's public Python API."""

    def __init__(
        self,
        model_dir: Path,
        default_model: str = DEFAULT_MODEL,
        output_format: str = "WAV",
        chunk_duration: float | None = None,
        mdx_segment_size: int = 32,
        mdxc_segment_size: int = 256,
        mdxc_override_model_segment_size: bool = True,
        log_level: int = logging.INFO,
    ) -> None:
        self.model_dir = model_dir
        self.default_model = default_model
        self.output_format = output_format
        self.chunk_duration = chunk_duration
        self.mdx_segment_size = mdx_segment_size
        self.mdxc_segment_size = mdxc_segment_size
        self.mdxc_override_model_segment_size = mdxc_override_model_segment_size
        self.log_level = log_level
        self._separators: dict[str, object] = {}
        self._lock = threading.Lock()

    def resolve_model(self, model: str) -> str:
        normalized = model.strip().lower()
        if normalized == "default":
            return self.default_model
        return MODEL_ALIASES.get(normalized, model.strip())

    def _separator(self, model: str, output_dir: Path):
        model_filename = self.resolve_model(model)
        separator = self._separators.get(model_filename)
        if separator is None:
            from audio_separator.separator import Separator

            separator = Separator(
                log_level=self.log_level,
                model_file_dir=str(self.model_dir),
                output_dir=str(output_dir),
                output_format=self.output_format,
                chunk_duration=self.chunk_duration,
                mdx_params={
                    "hop_length": 1024,
                    "segment_size": self.mdx_segment_size,
                    "overlap": 0.25,
                    "batch_size": 1,
                    "enable_denoise": False,
                },
                mdxc_params={
                    "segment_size": self.mdxc_segment_size,
                    "override_model_segment_size": self.mdxc_override_model_segment_size,
                    "batch_size": 1,
                    "overlap": 8,
                    "pitch_shift": 0,
                },
            )
            separator.load_model(model_filename=model_filename)
            self._separators[model_filename] = separator
        else:
            separator.output_dir = str(output_dir)
            if getattr(separator, "model_instance", None) is not None:
                separator.model_instance.output_dir = str(output_dir)
        return separator

    def separate_vocal(self, audio_path: Path, output_dir: Path, model: str) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            separator = self._separator(model, output_dir)
            output_files = separator.separate(
                str(audio_path),
                custom_output_names={"Vocals": "vocals", "Instrumental": "instrumental"},
            )

        outputs: dict[str, Path] = {}
        for value in output_files:
            path = Path(value)
            if not path.is_absolute():
                path = output_dir / path
            name = path.stem.lower()
            if name == "vocals" or "vocal" in name:
                outputs["vocals"] = path.resolve()
            elif name == "instrumental" or "instrument" in name:
                outputs["instrumental"] = path.resolve()
            else:
                outputs[path.stem.lower()] = path.resolve()
        if "instrumental" not in outputs and "vocals" in outputs:
            accompaniment_stems = [path for kind, path in outputs.items() if kind != "vocals" and path.is_file()]
            if accompaniment_stems:
                instrumental = output_dir / "instrumental.wav"
                command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
                for stem in accompaniment_stems:
                    command.extend(["-i", str(stem)])
                command.extend([
                    "-filter_complex",
                    f"amix=inputs={len(accompaniment_stems)}:duration=longest:normalize=0",
                    str(instrumental),
                ])
                subprocess.run(command, check=True, capture_output=True, text=True)
                outputs["instrumental"] = instrumental.resolve()
        return outputs


class AudioSeparatorService:
    def __init__(self, engine: PythonAudioSeparatorEngine, extractor: AudioExtractor | None = None) -> None:
        self.engine = engine
        self.extractor = extractor or AudioExtractor()

    async def extract_audio(self, source: Path, output_dir: Path) -> Path:
        return await self.extractor.extract_audio(source, output_dir / ".work" / "source.wav")

    async def separate_vocal(self, audio_path: Path, output_dir: Path, model: str) -> dict[str, Path]:
        return await asyncio.to_thread(self.engine.separate_vocal, audio_path, output_dir, model)

    async def process_file(
        self,
        source: Path,
        output_dir: Path,
        model: str = "default",
        progress: ProgressCallback | None = None,
    ) -> ProcessResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        source = source.expanduser().resolve()
        kind = media_type_for(source)
        if kind is None:
            raise ValueError(f"Unsupported media format: {source.suffix}")
        output_dir.mkdir(parents=True, exist_ok=True)

        audio_path = source
        if kind == "video":
            if progress:
                progress("extracting_audio", 15, f"Extracting audio from {source.name}")
            audio_path = await self.extract_audio(source, output_dir)

        if progress:
            progress("separating", 35, f"Separating vocals from {source.name}")
        outputs = await self.separate_vocal(audio_path, output_dir, model)
        work_dir = output_dir / ".work"
        if work_dir.exists():
            for child in work_dir.iterdir():
                child.unlink(missing_ok=True)
            work_dir.rmdir()
        if progress:
            progress("completed", 100, f"Completed {source.name}")
        return ProcessResult(
            source=source,
            media_type=kind,
            output_dir=output_dir,
            outputs=outputs,
            duration_seconds=loop.time() - started,
        )

    async def process_folder(
        self,
        input_path: Path,
        output_root: Path,
        model: str = "default",
        progress: Callable[[MediaFile, str, float, str], None] | None = None,
    ) -> list[ProcessResult]:
        media_files = scan_media(input_path)
        results: list[ProcessResult] = []
        for media in media_files:
            target = reserve_output_subdirectory(output_root, media.relative_path)

            def on_progress(status: str, value: float, message: str, item: MediaFile = media) -> None:
                if progress:
                    progress(item, status, value, message)

            results.append(await self.process_file(media.path, target, model, on_progress))
        return results
