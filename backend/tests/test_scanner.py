from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from app.services.scanner import output_subdirectory, reserve_output_subdirectory, scan_media


def test_scans_supported_media_case_insensitively(tmp_path: Path) -> None:
    for name in ["clip.MP4", "song.mp3", "voice.WAV", "ignore.txt"]:
        (tmp_path / name).touch()

    found = scan_media(tmp_path)

    assert [(item.path.name, item.media_type) for item in found] == [
        ("clip.MP4", "video"),
        ("song.mp3", "audio"),
        ("voice.WAV", "audio"),
    ]


def test_output_directory_preserves_relative_parent(tmp_path: Path) -> None:
    assert output_subdirectory(tmp_path, Path("album/demo.mp3")) == tmp_path / "album" / "demo"


def test_output_directory_reservation_is_unique_under_concurrency(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(reserve_output_subdirectory, tmp_path, Path("album/demo.mp3"))
            for _ in range(2)
        ]
    reserved = {future.result() for future in futures}

    assert len(reserved) == 2
    assert all(path.is_dir() for path in reserved)
