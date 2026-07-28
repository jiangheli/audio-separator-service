from __future__ import annotations

from types import SimpleNamespace

from app import hardware


def test_detect_nvidia_gpu_reads_nvidia_smi(monkeypatch) -> None:
    monkeypatch.setattr(
        hardware.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="NVIDIA RTX 4090, 576.88, 24564, 20000\n",
        ),
    )

    gpu = hardware.detect_nvidia_gpu()

    assert gpu is not None
    assert gpu.name == "NVIDIA RTX 4090"
    assert gpu.driver_version == "576.88"
    assert round(gpu.total_memory_gb, 1) == 24.0
    assert round(gpu.free_memory_gb, 1) == 19.5
    assert hardware.recommended_gpu_workers(gpu) == 4


def test_gpu_memory_estimate_is_conservative() -> None:
    assert hardware.gpu_memory_estimate_gb(1) == 5.5
    assert hardware.gpu_memory_estimate_gb(3) == 13.5
