from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CUDA_INDEX_URL = "https://download.pytorch.org/whl/cu128"
PYTHON_EMBED_URL = (
    "https://www.python.org/ftp/python/3.12.10/"
    "python-3.12.10-embed-amd64.zip"
)
PYTHON_EMBED_SHA256 = (
    "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
)
TORCH_VERSION = "2.7.1"
TORCHVISION_VERSION = "0.22.1"
TORCHAUDIO_VERSION = "2.7.1"
GPU_RUNTIME_VERSION = "2"
OFFLINE_MANIFEST_NAME = "wheelhouse-manifest.json"

LogCallback = Callable[[str], None]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def program_data_dir() -> Path:
    root = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "StemFlow"
    return Path.home() / ".stemflow"


def bundled_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[2]


def runtime_root() -> Path:
    return program_data_dir() / "gpu-runtime"


def marker_path() -> Path:
    return runtime_root() / "installed.json"


def runtime_python(*, windowed: bool = False) -> Path:
    name = "pythonw.exe" if windowed else "python.exe"
    return runtime_root() / "python" / name


def runtime_ready() -> bool:
    marker = marker_path()
    python = runtime_python()
    app_package = runtime_root() / "python" / "Lib" / "site-packages" / "app"
    if not marker.is_file() or not python.is_file() or not app_package.is_dir():
        return False
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        return value.get("runtime_version") == GPU_RUNTIME_VERSION
    except (json.JSONDecodeError, OSError):
        return False


def offline_wheelhouse() -> Path | None:
    wheelhouse = bundled_root() / "gpu-bootstrap" / "wheelhouse"
    manifest = wheelhouse / OFFLINE_MANIFEST_NAME
    if wheelhouse.is_dir() and manifest.is_file():
        return wheelhouse
    return None


def verify_offline_wheelhouse(wheelhouse: Path) -> dict[str, Any]:
    manifest_path = wheelhouse / OFFLINE_MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"CUDA 离线资源清单缺失：{manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"CUDA 离线资源清单无效：{error}") from error
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("CUDA 离线资源清单未包含 wheel 文件")
    for entry in files:
        if not isinstance(entry, dict):
            raise RuntimeError("CUDA 离线资源清单格式无效")
        name = str(entry.get("name", ""))
        expected = str(entry.get("sha256", "")).lower()
        expected_size = int(entry.get("size", 0))
        path = wheelhouse / name
        if (
            not name
            or not expected
            or not path.is_file()
            or path.stat().st_size != expected_size
        ):
            raise RuntimeError(f"CUDA 离线资源缺失或大小错误：{name}")
        actual = _sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"CUDA 离线资源校验失败：{name}")
    return manifest


def read_marker() -> dict[str, Any]:
    if not marker_path().is_file():
        return {}
    try:
        value = json.loads(marker_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _run_stream(command: Sequence[str], log: LogCallback) -> None:
    display = " ".join(str(part) for part in command)
    log(f"> {display}")
    process = subprocess.Popen(
        [str(part) for part in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None
    for line in process.stdout:
        stripped = line.rstrip()
        if stripped:
            log(stripped)
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(
            f"命令执行失败（退出码 {return_code}）：{display}"
        )


def _enable_site_packages(python_dir: Path) -> None:
    candidates = sorted(python_dir.glob("python*._pth"))
    if not candidates:
        raise RuntimeError("嵌入式 Python 缺少 ._pth 配置文件")
    path = candidates[0]
    lines = path.read_text(encoding="utf-8").splitlines()
    normalized = [
        "import site" if line.strip() == "#import site" else line
        for line in lines
    ]
    path.write_text("\n".join(normalized) + "\n", encoding="utf-8")


def install_cuda_runtime(log: LogCallback) -> dict[str, Any]:
    """Install the bundled isolated CUDA runtime without network access."""
    assets = bundled_root() / "gpu-bootstrap"
    python_archive = assets / "python-3.12.10-embed-amd64.zip"
    get_pip = assets / "get-pip.py"
    app_source = assets / "stemflow" / "app"
    wheelhouse = offline_wheelhouse()
    if wheelhouse is None:
        raise FileNotFoundError("安装包未包含 CUDA PyTorch 离线 wheelhouse")
    for required in (python_archive, get_pip, app_source, wheelhouse):
        if not required.exists():
            raise FileNotFoundError(f"安装资源缺失：{required}")
    manifest = verify_offline_wheelhouse(wheelhouse)
    log(
        "CUDA PyTorch 离线资源校验通过："
        f"{len(manifest['files'])} 个 wheel，安装过程无需联网。"
    )
    digest = _sha256_file(python_archive)
    if digest != PYTHON_EMBED_SHA256:
        raise RuntimeError(f"Python 安装资源校验失败：{digest}")
    log(f"Python 安装资源 SHA256 校验通过：{digest}")

    root = runtime_root()
    python_dir = root / "python"
    root.mkdir(parents=True, exist_ok=True)
    marker_path().unlink(missing_ok=True)
    if python_dir.exists():
        shutil.rmtree(python_dir)
    python_dir.mkdir(parents=True)

    log("正在准备独立 NVIDIA CUDA 运行环境…")
    with zipfile.ZipFile(python_archive) as archive:
        archive.extractall(python_dir)
    _enable_site_packages(python_dir)
    python = runtime_python()
    if not python.is_file():
        raise RuntimeError("Python CUDA 运行环境安装后未找到 python.exe")
    offline_args = [
        "--no-index",
        "--find-links",
        str(wheelhouse),
    ]
    _run_stream(
        [
            str(python),
            str(get_pip),
            "--disable-pip-version-check",
            *offline_args,
        ],
        log,
    )
    _run_stream(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *offline_args,
            f"torch=={TORCH_VERSION}+cu128",
            f"torchvision=={TORCHVISION_VERSION}+cu128",
            f"torchaudio=={TORCHAUDIO_VERSION}+cu128",
            "audio-separator>=0.44.5,<0.45",
            "imageio-ffmpeg>=0.6,<1",
            "onnxruntime-gpu>=1.21,<1.23",
            "XlsxWriter>=3.2,<4",
        ],
        log,
    )

    site_packages = python_dir / "Lib" / "site-packages"
    target_app = site_packages / "app"
    if target_app.exists():
        shutil.rmtree(target_app)
    shutil.copytree(app_source, target_app)

    verification = (
        "import json, torch; import onnxruntime as ort; "
        "getattr(ort, 'preload_dlls', lambda: None)(); "
        "providers=ort.get_available_providers(); "
        "value={'cuda':torch.cuda.is_available(),"
        "'device':torch.cuda.get_device_name(0) if torch.cuda.is_available() else '',"
        "'providers':providers}; "
        "print(json.dumps(value, ensure_ascii=False)); "
        "raise SystemExit(0 if value['cuda'] and "
        "'CUDAExecutionProvider' in providers else 3)"
    )
    _run_stream([str(python), "-c", verification], log)

    marker = {
        "runtime_version": GPU_RUNTIME_VERSION,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "cuda_index_url": CUDA_INDEX_URL,
        "torch_version": TORCH_VERSION,
        "install_source": "bundled-offline-wheelhouse",
        "wheelhouse_created_at": manifest.get("created_at", ""),
        "python_source": PYTHON_EMBED_URL,
        "python_sha256": PYTHON_EMBED_SHA256,
    }
    temporary = marker_path().with_suffix(".tmp")
    temporary.write_text(
        json.dumps(marker, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, marker_path())
    log("NVIDIA CUDA 加速组件安装并验证成功。")
    return marker


def cuda_runtime_available() -> bool:
    if not runtime_ready():
        return False
    verification = (
        "import torch; import onnxruntime as ort; "
        "getattr(ort, 'preload_dlls', lambda: None)(); "
        "raise SystemExit(0 if torch.cuda.is_available() and "
        "'CUDAExecutionProvider' in ort.get_available_providers() else 3)"
    )
    try:
        result = subprocess.run(
            [str(runtime_python()), "-c", verification],
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
