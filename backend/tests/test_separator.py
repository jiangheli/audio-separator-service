from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

from app.services.separator import PythonAudioSeparatorEngine


def test_gpu_tuning_is_passed_through_public_separator_api(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeSeparator:
        def __init__(self, **options: object) -> None:
            captured.update(options)
            self.output_dir = str(options["output_dir"])
            self.model_instance = None

        def load_model(self, *, model_filename: str) -> None:
            captured["model_filename"] = model_filename

    module = ModuleType("audio_separator.separator")
    module.Separator = FakeSeparator  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "audio_separator.separator", module)

    engine = PythonAudioSeparatorEngine(
        tmp_path / "models",
        default_model="model.onnx",
        mdx_segment_size=256,
        batch_size=3,
    )
    engine.load_model("model.onnx")

    assert captured["model_filename"] == "model.onnx"
    assert captured["mdx_params"] == {
        "hop_length": 1024,
        "segment_size": 256,
        "overlap": 0.25,
        "batch_size": 3,
        "enable_denoise": False,
    }
    assert captured["mdxc_params"] == {
        "segment_size": 256,
        "override_model_segment_size": True,
        "batch_size": 3,
        "overlap": 8,
        "pitch_shift": 0,
    }
