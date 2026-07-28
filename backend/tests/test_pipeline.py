from pathlib import Path

import pytest

from app.services.video_pipeline import VideoBgmRemovalPipeline


class FakeExtractor:
    def extract_audio(self, _source: Path, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
        return target


class FakeSeparator:
    def separate_vocals(
        self,
        _audio: Path,
        output_dir: Path,
        _model: str,
        *,
        device: str = "auto",
    ) -> Path:
        assert device in {"auto", "cpu", "cuda"}
        output_dir.mkdir(parents=True, exist_ok=True)
        vocals = output_dir / "vocals.wav"
        vocals.write_bytes(b"vocals")
        (output_dir / "instrumental.wav").write_bytes(b"bgm")
        return vocals


class FakeComposer:
    def compose(
        self,
        _source: Path,
        _vocals: Path,
        output: Path,
        *,
        prefer_stream_copy: bool,
        prefer_nvenc: bool = False,
    ) -> bool:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video-with-vocals")
        return prefer_stream_copy


def test_pipeline_outputs_video_and_removes_all_temporary_stems(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    pipeline = VideoBgmRemovalPipeline(
        FakeExtractor(),
        FakeSeparator(),
        FakeComposer(),
        work_root=work_root,
    )
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    output = tmp_path / "result" / "input_vocals_only.mp4"
    statuses: list[str] = []

    result = pipeline.process(
        source,
        output,
        job_id="job-1",
        model="mdx",
        prefer_video_copy=True,
        progress=lambda status, _message: statuses.append(status),
    )

    assert result.output_video == output
    assert output.read_bytes() == b"video-with-vocals"
    assert statuses == [
        "extracting_audio",
        "separating_vocals",
        "composing_video",
        "completed",
    ]
    assert not (work_root / "job-1").exists()
    assert list(tmp_path.rglob("vocals.wav")) == []
    assert list(tmp_path.rglob("instrumental.wav")) == []


def test_pipeline_cleans_work_directory_after_failure(tmp_path: Path) -> None:
    class FailingComposer(FakeComposer):
        def compose(self, *args: object, **kwargs: object) -> bool:
            raise RuntimeError("compose failed")

    pipeline = VideoBgmRemovalPipeline(
        FakeExtractor(),
        FakeSeparator(),
        FailingComposer(),
        work_root=tmp_path / "work",
    )
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")

    with pytest.raises(RuntimeError, match="compose failed"):
        pipeline.process(
            source,
            tmp_path / "output.mp4",
            job_id="job-2",
            model="mdx",
            prefer_video_copy=True,
        )
    assert not (tmp_path / "work" / "job-2").exists()
