from pathlib import Path

import pytest

from app.services.separator import AudioSeparatorService


class FakeEngine:
    def separate_vocal(self, audio_path: Path, output_dir: Path, model: str):
        assert audio_path.exists()
        assert model == "default"
        outputs = {}
        for kind in ("vocals", "instrumental"):
            path = output_dir / f"{kind}.wav"
            path.write_bytes(kind.encode())
            outputs[kind] = path
        return outputs


class FakeExtractor:
    async def extract_audio(self, video_path: Path, target_path: Path):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"wave")
        return target_path


@pytest.mark.asyncio
async def test_process_video_extracts_and_creates_canonical_outputs(tmp_path: Path) -> None:
    source = tmp_path / "demo.mp4"
    source.write_bytes(b"video")
    output = tmp_path / "result" / "demo"
    service = AudioSeparatorService(FakeEngine(), FakeExtractor())
    events = []

    result = await service.process_file(source, output, progress=lambda *event: events.append(event))

    assert result.media_type == "video"
    assert result.outputs["vocals"].name == "vocals.wav"
    assert result.outputs["instrumental"].name == "instrumental.wav"
    assert [event[0] for event in events] == ["extracting_audio", "separating", "completed"]
    assert not (output / ".work").exists()

