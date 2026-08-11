import os
import time
from pathlib import Path

from app.services.scanner import (
    file_fingerprint,
    is_file_stable,
    output_path_for,
    scan_videos,
)


def test_scan_only_videos_and_preserve_relative_paths(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = input_dir / "generated"
    (input_dir / "season").mkdir(parents=True)
    output_dir.mkdir()
    (input_dir / "season" / "episode.mp4").write_bytes(b"video")
    (input_dir / "song.wav").write_bytes(b"audio")
    (output_dir / "old.mp4").write_bytes(b"generated")

    videos = scan_videos(input_dir, excluded_roots=(output_dir,))

    assert [video.relative_path.as_posix() for video in videos] == ["season/episode.mp4"]
    assert output_path_for(tmp_path / "result", videos[0], "_vocals_only") == (
        tmp_path / "result" / "season" / "episode_vocals_only.mp4"
    )
    assert len(file_fingerprint(videos[0])) == 64


def test_stability_uses_last_modified_time(tmp_path: Path) -> None:
    source = tmp_path / "demo.mov"
    source.write_bytes(b"video")
    old = time.time() - 180
    os.utime(source, (old, old))
    video = scan_videos(tmp_path)[0]

    assert is_file_stable(video, 120, now=old + 121)
    assert not is_file_stable(video, 120, now=old + 60)
