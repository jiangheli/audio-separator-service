import os
import shutil
import tempfile
from pathlib import Path


class RuntimeDependencyError(RuntimeError):
    pass


def ensure_ffmpeg() -> str:
    """Return an FFmpeg executable and expose bundled FFmpeg on PATH when needed."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled_ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    except (ImportError, OSError, RuntimeError) as error:
        raise RuntimeDependencyError(
            "FFmpeg was not found. Install FFmpeg or reinstall audio-separator-service."
        ) from error

    if not bundled_ffmpeg.is_file():
        raise RuntimeDependencyError(f"Bundled FFmpeg is missing: {bundled_ffmpeg}")

    # imageio-ffmpeg uses versioned filenames such as
    # `ffmpeg-macos-aarch64-v7.1`. python-audio-separator checks for the
    # literal command `ffmpeg`, so expose a stable shim name on PATH.
    # A per-process shim avoids races when CPU and CUDA worker processes start
    # at the same time.
    shim_dir = Path(tempfile.gettempdir()) / f"stemflow-ffmpeg-bin-{os.getpid()}"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not shim.exists() or shim.resolve() != bundled_ffmpeg:
        if shim.exists() or shim.is_symlink():
            shim.unlink()
        try:
            shim.symlink_to(bundled_ffmpeg)
        except OSError:
            try:
                os.link(bundled_ffmpeg, shim)
            except OSError:
                shutil.copy2(bundled_ffmpeg, shim)
    if os.name != "nt":
        shim.chmod(shim.stat().st_mode | 0o111)

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    shim_parent = str(shim_dir)
    if shim_parent not in path_entries:
        os.environ["PATH"] = shim_parent + os.pathsep + os.environ.get("PATH", "")
    return str(bundled_ffmpeg)
