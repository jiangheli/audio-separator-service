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

from app.hardware import NvidiaGpu, detect_nvidia_gpu


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
GPU_RUNTIME_VERSION = "3"
OFFLINE_MANIFEST_NAME = "wheelhouse-manifest.json"
MIN_CUDA12_WINDOWS_DRIVER = (528, 33)
MIN_BLACKWELL_WINDOWS_DRIVER = (570, 65)
MIN_INSTALL_FREE_GB = 12.0

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


def _version_tuple(value: str) -> tuple[int, int]:
    parts = value.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 0, 0


def minimum_driver_for(gpu: NvidiaGpu) -> tuple[int, int]:
    compact_name = gpu.name.upper().replace(" ", "")
    if "RTX50" in compact_name:
        return MIN_BLACKWELL_WINDOWS_DRIVER
    return MIN_CUDA12_WINDOWS_DRIVER


def _disk_free_gb(path: Path) -> float:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free / (1024**3)


def cuda_install_preflight() -> dict[str, Any]:
    gpu = detect_nvidia_gpu()
    if gpu is None:
        raise RuntimeError(
            "未检测到 NVIDIA 显卡或 nvidia-smi。请先安装 NVIDIA 官方驱动并重启。"
        )
    required_driver = minimum_driver_for(gpu)
    if _version_tuple(gpu.driver_version) < required_driver:
        required_text = ".".join(str(part) for part in required_driver)
        raise RuntimeError(
            f"{gpu.name} 当前驱动 {gpu.driver_version}，"
            f"内置 CUDA 12.8 至少需要驱动 {required_text}。"
            "请更新 NVIDIA 驱动并重启，不需要另装 CUDA Toolkit。"
        )
    wheelhouse = offline_wheelhouse()
    if wheelhouse is None:
        raise FileNotFoundError(
            "安装包未包含 CUDA PyTorch 离线资源，请使用完整离线 GPU 套件。"
        )
    free_gb = _disk_free_gb(program_data_dir())
    if free_gb < MIN_INSTALL_FREE_GB:
        raise RuntimeError(
            f"CUDA 运行环境所在磁盘仅剩 {free_gb:.1f} GB，"
            f"至少需要 {MIN_INSTALL_FREE_GB:.0f} GB 空闲空间。"
        )
    return {
        "gpu_name": gpu.name,
        "driver_version": gpu.driver_version,
        "free_disk_gb": free_gb,
        "wheelhouse": str(wheelhouse),
    }


def verify_offline_wheelhouse(
    wheelhouse: Path,
    log: LogCallback | None = None,
) -> dict[str, Any]:
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
    total = len(files)
    for index, entry in enumerate(files, start=1):
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
        if log and (expected_size >= 100 * 1024 * 1024 or index == total):
            log(f"[1/4 校验资源] {index}/{total}：{name}")
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
    root = runtime_root()
    python_dir = root / "python"
    staging_dir = root / "python.installing"
    backup_dir = root / "python.previous"
    root.mkdir(parents=True, exist_ok=True)
    if staging_dir.exists():
        log("[恢复] 正在清理上一次未完成的 CUDA 临时目录…")
        shutil.rmtree(staging_dir)
    if python_dir.exists() and not marker_path().is_file():
        log("[恢复] 正在清理上一次未完成的 CUDA 运行环境…")
        shutil.rmtree(python_dir)

    preflight = cuda_install_preflight()
    log(
        "[预检] "
        f"{preflight['gpu_name']}，驱动 {preflight['driver_version']}，"
        f"运行磁盘可用 {preflight['free_disk_gb']:.1f} GB。"
    )
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

    manifest = verify_offline_wheelhouse(wheelhouse, log)
    log(
        "[1/4 校验资源] CUDA PyTorch 离线资源校验通过："
        f"{len(manifest['files'])} 个 wheel，安装过程无需联网。"
    )
    digest = _sha256_file(python_archive)
    if digest != PYTHON_EMBED_SHA256:
        raise RuntimeError(f"Python 安装资源校验失败：{digest}")
    log(f"[1/4 校验资源] Python 安装资源 SHA256 校验通过：{digest}")

    log("[2/4 解压 Python] 正在准备独立 NVIDIA CUDA 运行环境…")
    staging_dir.mkdir(parents=True)
    with zipfile.ZipFile(python_archive) as archive:
        archive.extractall(staging_dir)
    _enable_site_packages(staging_dir)
    python = staging_dir / "python.exe"
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
    log("[3/4 安装组件] pip 已就绪，正在安装 CUDA PyTorch 和推理依赖…")
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

    site_packages = staging_dir / "Lib" / "site-packages"
    target_app = site_packages / "app"
    if target_app.exists():
        shutil.rmtree(target_app)
    shutil.copytree(app_source, target_app)

    log("[4/4 验证 GPU] 正在验证 PyTorch CUDA 与 ONNX Runtime GPU…")
    verification = (
        "import json, torch; "
        "value={'torch':torch.__version__,'built_cuda':torch.version.cuda,"
        "'cuda':torch.cuda.is_available(),"
        "'device':torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''}; "
        "print(json.dumps(value, ensure_ascii=False)); "
        "raise SystemExit(0 if value['cuda'] else 3)"
    )
    _run_stream([str(python), "-c", verification], log)
    ort_verification = (
        "import json; import onnxruntime as ort; "
        "getattr(ort, 'preload_dlls', lambda: None)(); "
        "providers=ort.get_available_providers(); "
        "value={'onnxruntime':ort.__version__,'providers':providers}; "
        "print(json.dumps(value, ensure_ascii=False)); "
        "raise SystemExit(0 if 'CUDAExecutionProvider' in providers else 3)"
    )
    _run_stream([str(python), "-c", ort_verification], log)

    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    if python_dir.exists():
        os.replace(python_dir, backup_dir)
    try:
        os.replace(staging_dir, python_dir)
    except Exception:
        if backup_dir.exists() and not python_dir.exists():
            os.replace(backup_dir, python_dir)
        raise

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
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
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
