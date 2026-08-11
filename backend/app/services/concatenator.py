from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.services.extractor import CREATE_NO_WINDOW


@dataclass(frozen=True, slots=True)
class ConcatenationResult:
    folder: Path
    output: Path
    input_count: int
    status: str
    error: str = ""


def _natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def _manifest_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return value.replace("'", "'\\''")


class FolderVideoConcatenator:
    """Create one vocals-only collection for every source folder."""

    def __init__(self, ffmpeg_binary: str, *, audio_bitrate: str = "192k") -> None:
        self.ffmpeg_binary = ffmpeg_binary
        self.audio_bitrate = audio_bitrate

    def concatenate_completed(
        self,
        jobs: Iterable[dict[str, Any]],
        *,
        input_root: Path,
        output_root: Path,
        output_suffix: str,
        prefer_nvenc: bool = False,
    ) -> list[ConcatenationResult]:
        groups: dict[Path, list[tuple[Path, Path]]] = {}
        for job in jobs:
            if str(job.get("status", "")) != "completed":
                continue
            output = Path(str(job.get("output_path", "")))
            if not output.is_file():
                continue
            relative = Path(str(job.get("relative_path", "")))
            groups.setdefault(relative.parent, []).append((relative, output))

        results: list[ConcatenationResult] = []
        for relative_folder, entries in sorted(
            groups.items(),
            key=lambda item: _natural_key(item[0].as_posix()),
        ):
            entries.sort(key=lambda item: _natural_key(item[0].name))
            inputs = [output for _relative, output in entries]
            folder_name = (
                input_root.name
                if relative_folder == Path(".")
                else relative_folder.name
            )
            destination_dir = output_root / relative_folder
            destination = (
                destination_dir
                / f"{folder_name}_合集{output_suffix}.mp4"
            )
            results.append(
                self._concatenate(
                    relative_folder,
                    inputs,
                    destination,
                    prefer_nvenc=prefer_nvenc,
                )
            )
        return results

    def _concatenate(
        self,
        folder: Path,
        inputs: list[Path],
        destination: Path,
        *,
        prefer_nvenc: bool,
    ) -> ConcatenationResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not inputs:
            return ConcatenationResult(
                folder,
                destination,
                0,
                "skipped",
                "No completed videos",
            )
        state_path = destination.with_suffix(destination.suffix + ".stemflow.json")
        expected_state = {
            "inputs": [
                {
                    "path": str(path.resolve()),
                    "size": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                }
                for path in inputs
            ]
        }
        current_state: object = None
        try:
            current_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if (
            destination.is_file()
            and destination.stat().st_size > 0
            and current_state == expected_state
        ):
            return ConcatenationResult(
                folder,
                destination,
                len(inputs),
                "unchanged",
            )

        manifest = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}.concat.txt"
        )
        temporary = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}.tmp.mp4"
        )
        state_temporary = state_path.with_suffix(state_path.suffix + ".tmp")
        manifest.write_text(
            "\n".join(
                f"file '{_manifest_path(path)}'"
                for path in inputs
            )
            + "\n",
            encoding="utf-8",
        )
        attempts = ["copy"]
        if prefer_nvenc:
            attempts.append("nvenc")
        attempts.append("cpu")
        errors: list[str] = []
        try:
            for mode in attempts:
                temporary.unlink(missing_ok=True)
                result = subprocess.run(
                    self._command(manifest, temporary, mode=mode),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                    check=False,
                )
                if (
                    result.returncode == 0
                    and temporary.is_file()
                    and temporary.stat().st_size > 0
                ):
                    os.replace(temporary, destination)
                    try:
                        state_temporary.write_text(
                            json.dumps(
                                expected_state,
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        os.replace(state_temporary, state_path)
                    except OSError:
                        state_temporary.unlink(missing_ok=True)
                    return ConcatenationResult(
                        folder,
                        destination,
                        len(inputs),
                        "completed",
                    )
                errors.append(f"{mode}: {(result.stderr or result.stdout)[-2000:]}")
            return ConcatenationResult(
                folder,
                destination,
                len(inputs),
                "failed",
                " | ".join(errors)[-6000:],
            )
        finally:
            manifest.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            state_temporary.unlink(missing_ok=True)

    def _command(
        self,
        manifest: Path,
        temporary: Path,
        *,
        mode: str,
    ) -> list[str]:
        if mode == "copy":
            codecs = ["-c", "copy"]
        elif mode == "nvenc":
            codecs = [
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p5",
                "-cq",
                "19",
                "-c:a",
                "aac",
                "-b:a",
                self.audio_bitrate,
            ]
        else:
            codecs = [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                self.audio_bitrate,
            ]
        return [
            self.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            *codecs,
            "-movflags",
            "+faststart",
            str(temporary),
        ]
