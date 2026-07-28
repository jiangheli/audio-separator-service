from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services import concatenator as concatenator_module
from app.services.concatenator import ConcatenationResult, FolderVideoConcatenator


class FakeConcatenator(FolderVideoConcatenator):
    def __init__(self) -> None:
        super().__init__("ffmpeg")
        self.calls: list[tuple[Path, list[str], str]] = []

    def _concatenate(
        self,
        folder: Path,
        inputs: list[Path],
        destination: Path,
        *,
        prefer_nvenc: bool,
    ):
        self.calls.append((folder, [path.name for path in inputs], destination.name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"collection")
        return ConcatenationResult(
            folder,
            destination,
            len(inputs),
            "completed",
        )


def test_groups_completed_outputs_by_source_parent_and_natural_order(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "输入"
    output_root = tmp_path / "输出"
    jobs = []
    for relative in (
        Path("第一季") / "第10集.mp4",
        Path("第一季") / "第2集.mp4",
        Path("第二季") / "第1集.mp4",
    ):
        output = output_root / relative.parent / f"{relative.stem}_vocals_only.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")
        jobs.append(
            {
                "status": "completed",
                "relative_path": str(relative),
                "output_path": str(output),
            }
        )
    jobs.append(
        {
            "status": "failed",
            "relative_path": "第一季/第3集.mp4",
            "output_path": str(output_root / "第一季" / "missing.mp4"),
        }
    )

    concatenator = FakeConcatenator()
    concatenator.concatenate_completed(
        jobs,
        input_root=input_root,
        output_root=output_root,
        output_suffix="_vocals_only",
    )

    assert concatenator.calls == [
        (
            Path("第一季"),
            ["第2集_vocals_only.mp4", "第10集_vocals_only.mp4"],
            "第一季_合集_vocals_only.mp4",
        ),
        (
            Path("第二季"),
            ["第1集_vocals_only.mp4"],
            "第二季_合集_vocals_only.mp4",
        ),
    ]


def test_root_videos_use_input_folder_name(tmp_path: Path) -> None:
    input_root = tmp_path / "短剧"
    output_root = tmp_path / "输出"
    output = output_root / "第1集_vocals_only.mp4"
    output_root.mkdir()
    output.write_bytes(b"video")

    concatenator = FakeConcatenator()
    concatenator.concatenate_completed(
        [
            {
                "status": "completed",
                "relative_path": "第1集.mp4",
                "output_path": str(output),
            }
        ],
        input_root=input_root,
        output_root=output_root,
        output_suffix="_vocals_only",
    )

    assert concatenator.calls[0][2] == "短剧_合集_vocals_only.mp4"


def test_unchanged_collection_uses_exact_input_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inputs = [tmp_path / "第1集.mp4", tmp_path / "第2集.mp4"]
    for path in inputs:
        path.write_bytes(path.name.encode())
    destination = tmp_path / "合集.mp4"
    calls = 0

    def fake_run(command, **_options):
        nonlocal calls
        calls += 1
        Path(command[-1]).write_bytes(b"collection")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(concatenator_module.subprocess, "run", fake_run)
    service = FolderVideoConcatenator("ffmpeg")

    first = service._concatenate(
        Path("."),
        inputs,
        destination,
        prefer_nvenc=False,
    )
    second = service._concatenate(
        Path("."),
        inputs,
        destination,
        prefer_nvenc=False,
    )

    assert first.status == "completed"
    assert second.status == "unchanged"
    assert calls == 1
