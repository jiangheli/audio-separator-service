from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.gpu_runtime import (
    OFFLINE_MANIFEST_NAME,
    cuda_install_preflight,
    minimum_driver_for,
    sync_runtime_app,
    verify_offline_wheelhouse,
)
from app.hardware import NvidiaGpu


def write_manifest(wheelhouse: Path, wheel: Path) -> None:
    manifest = {
        "format_version": 1,
        "created_at": "2026-07-28T00:00:00Z",
        "files": [
            {
                "name": wheel.name,
                "size": wheel.stat().st_size,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
        ],
    }
    (wheelhouse / OFFLINE_MANIFEST_NAME).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def test_verify_offline_wheelhouse_accepts_matching_manifest(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "torch-test.whl"
    wheel.write_bytes(b"offline-cuda-wheel")
    write_manifest(tmp_path, wheel)

    manifest = verify_offline_wheelhouse(tmp_path)

    assert manifest["files"][0]["name"] == wheel.name


def test_verify_offline_wheelhouse_rejects_modified_wheel(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "torch-test.whl"
    wheel.write_bytes(b"original")
    write_manifest(tmp_path, wheel)
    wheel.write_bytes(b"modified")

    with pytest.raises(RuntimeError, match="大小错误|校验失败"):
        verify_offline_wheelhouse(tmp_path)


def test_blackwell_gpu_requires_cuda_128_driver() -> None:
    gpu = NvidiaGpu("NVIDIA GeForce RTX 5060 Ti", "566.36", 8.0, 6.0)

    assert minimum_driver_for(gpu) == (570, 65)


def test_cuda_preflight_reports_outdated_driver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gpu = NvidiaGpu("NVIDIA GeForce RTX 5060 Ti", "566.36", 8.0, 6.0)
    monkeypatch.setattr(
        "app.gpu_runtime.detect_nvidia_gpu",
        lambda: gpu,
    )
    monkeypatch.setattr(
        "app.gpu_runtime.offline_wheelhouse",
        lambda: tmp_path,
    )

    with pytest.raises(RuntimeError, match="570.65"):
        cuda_install_preflight()


def test_cuda_preflight_reports_disk_and_driver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gpu = NvidiaGpu("NVIDIA GeForce RTX 5060 Ti", "576.88", 8.0, 6.0)
    monkeypatch.setattr(
        "app.gpu_runtime.detect_nvidia_gpu",
        lambda: gpu,
    )
    monkeypatch.setattr(
        "app.gpu_runtime.offline_wheelhouse",
        lambda: tmp_path,
    )
    monkeypatch.setattr("app.gpu_runtime._disk_free_gb", lambda _path: 50.0)

    result = cuda_install_preflight()

    assert result["driver_version"] == "576.88"
    assert result["free_disk_gb"] == 50.0


def test_sync_runtime_app_updates_code_without_reinstalling_gpu(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    source = bundle / "gpu-bootstrap" / "stemflow" / "app"
    source.mkdir(parents=True)
    (source / "gpu_worker.py").write_text("new-code", encoding="utf-8")
    runtime = tmp_path / "runtime"
    target = runtime / "python" / "Lib" / "site-packages" / "app"
    target.mkdir(parents=True)
    (target / "gpu_worker.py").write_text("old-code", encoding="utf-8")
    monkeypatch.setattr("app.gpu_runtime.runtime_ready", lambda: True)
    monkeypatch.setattr("app.gpu_runtime.bundled_root", lambda: bundle)
    monkeypatch.setattr("app.gpu_runtime.runtime_root", lambda: runtime)

    assert sync_runtime_app() is True
    assert (target / "gpu_worker.py").read_text(encoding="utf-8") == "new-code"
    assert (runtime / "app-version.txt").is_file()
