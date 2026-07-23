from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


@dataclass(frozen=True)
class MediaFile:
    path: Path
    relative_path: Path
    media_type: str


def media_type_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return None


def scan_media(input_path: Path) -> list[MediaFile]:
    input_path = input_path.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    paths = [input_path] if input_path.is_file() else sorted(path for path in input_path.rglob("*") if path.is_file())
    root = input_path.parent if input_path.is_file() else input_path
    media: list[MediaFile] = []
    for path in paths:
        kind = media_type_for(path)
        if kind:
            media.append(MediaFile(path=path, relative_path=path.relative_to(root), media_type=kind))
    return media


def output_subdirectory(output_root: Path, relative_path: Path) -> Path:
    parent = relative_path.parent
    candidate = output_root / parent / relative_path.stem
    if candidate.exists() and any(candidate.iterdir()):
        candidate = output_root / parent / f"{relative_path.stem}_{relative_path.suffix.lower().lstrip('.')}"
    return candidate


def reserve_output_subdirectory(output_root: Path, relative_path: Path) -> Path:
    """Atomically reserve a unique result directory across worker processes."""
    parent = output_root / relative_path.parent
    stem = relative_path.stem
    extension = relative_path.suffix.lower().lstrip(".")
    index = 0
    while True:
        if index == 0:
            name = stem
        elif index == 1:
            name = f"{stem}_{extension}" if extension else f"{stem}_2"
        else:
            suffix = f"{extension}_{index}" if extension else str(index + 1)
            name = f"{stem}_{suffix}"
        candidate = parent / name
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            index += 1
