from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NvidiaGpu:
    name: str
    driver_version: str
    total_memory_gb: float
    free_memory_gb: float


@dataclass(frozen=True, slots=True)
class RuntimeHardware:
    cuda_available: bool
    cuda_provider_available: bool
    gpu: NvidiaGpu | None
    detail: str


def detect_nvidia_gpu() -> NvidiaGpu | None:
    """Read the first NVIDIA GPU without importing the inference runtime."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    first_line = next(
        (line.strip() for line in result.stdout.splitlines() if line.strip()),
        "",
    )
    parts = [part.strip() for part in first_line.split(",")]
    if len(parts) < 4:
        return None
    try:
        divisor = 1024.0
        return NvidiaGpu(
            name=parts[0],
            driver_version=parts[1],
            total_memory_gb=float(parts[2]) / divisor,
            free_memory_gb=float(parts[3]) / divisor,
        )
    except ValueError:
        return None


def runtime_hardware() -> RuntimeHardware:
    gpu = detect_nvidia_gpu()
    try:
        import onnxruntime as ort
        import torch

        cuda_available = bool(torch.cuda.is_available())
        providers = set(ort.get_available_providers())
        provider_available = "CUDAExecutionProvider" in providers
        if cuda_available:
            name = torch.cuda.get_device_name(0)
            detail = f"CUDA 可用：{name}"
        elif gpu:
            detail = (
                f"检测到 {gpu.name}（驱动 {gpu.driver_version}），"
                "CUDA 加速组件尚未启用"
            )
        else:
            detail = "未检测到可用的 NVIDIA CUDA 显卡"
        return RuntimeHardware(
            cuda_available=cuda_available,
            cuda_provider_available=provider_available,
            gpu=gpu,
            detail=detail,
        )
    except (ImportError, OSError, RuntimeError) as error:
        detail = (
            f"检测到 {gpu.name}，CUDA 运行组件不可用"
            if gpu
            else "CPU 模式"
        )
        return RuntimeHardware(False, False, gpu, f"{detail}：{error}")


def cuda_runtime_active() -> bool:
    hardware = runtime_hardware()
    return hardware.cuda_available and hardware.cuda_provider_available


def gpu_memory_estimate_gb(worker_count: int, batch_size: int = 1) -> float:
    workers = max(1, worker_count)
    batches = max(1, batch_size)
    return 1.5 + workers * (4.0 + (batches - 1) * 0.8)


def recommended_gpu_workers(gpu: NvidiaGpu | None) -> int:
    if gpu is None or gpu.free_memory_gb <= 0:
        return 1
    return max(1, min(8, int(max(0.0, gpu.free_memory_gb - 1.5) / 4.0)))


def is_windows() -> bool:
    return os.name == "nt"
