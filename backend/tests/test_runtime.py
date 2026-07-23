import os
import sys
from types import SimpleNamespace

from app.runtime import ensure_ffmpeg


def test_ensure_ffmpeg_prefers_system_binary(monkeypatch, tmp_path):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"")
    monkeypatch.setattr("app.runtime.shutil.which", lambda _: str(executable))

    assert ensure_ffmpeg() == str(executable)


def test_ensure_ffmpeg_adds_standard_named_bundled_binary_to_path(monkeypatch, tmp_path):
    executable = tmp_path / "ffmpeg-versioned-binary"
    executable.write_bytes(b"")
    monkeypatch.setattr("app.runtime.shutil.which", lambda _: None)
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: str(executable)),
    )
    monkeypatch.setattr("app.runtime.tempfile.gettempdir", lambda: str(tmp_path / "temp"))
    monkeypatch.setenv("PATH", "existing")

    assert ensure_ffmpeg() == str(executable.resolve())
    shim_dir = tmp_path / "temp" / "stemflow-ffmpeg-bin"
    assert os.environ["PATH"].split(os.pathsep)[0] == str(shim_dir)
    assert (shim_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).exists()
