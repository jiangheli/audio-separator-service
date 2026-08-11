from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from app import __version__
from app.cli.main import make_runner
from app.config import ServiceConfig
from app.gpu_runtime import (
    CUDA_INDEX_URL,
    cuda_install_preflight,
    cuda_runtime_available,
    install_cuda_runtime,
    offline_wheelhouse,
    read_marker,
    runtime_ready,
)
from app.hardware import (
    gpu_memory_estimate_gb,
    recommended_gpu_workers,
    runtime_hardware,
)
from app.repository import ProcessingRepository
from app.runner import ConcurrencyController
from app.updater import AvailableUpdate, check_for_update, download_update


APP_NAME = "StemFlow"
TASK_NAME = "StemFlow-Video-BGM-Removal"
DEFAULT_MODEL = "mdx"
RECOMMENDATION_LIMIT = 8
BASE_MEMORY_GB = 2.0
MEMORY_PER_WORKER_GB = 3.0
EXECUTION_MODES = {
    "cpu": "仅 CPU",
    "cuda": "仅 NVIDIA GPU",
    "hybrid": "CPU + NVIDIA GPU",
}
MODE_LABEL_TO_KEY = {label: key for key, label in EXECUTION_MODES.items()}
STATUS_LABELS = {
    "waiting_copy": "等待文件稳定",
    "pending": "排队中",
    "processing": "准备处理",
    "extracting_audio": "提取音轨",
    "separating_vocals": "分离人声",
    "composing_video": "合成视频",
    "completed": "已完成",
    "failed": "失败",
    "superseded": "源文件已更新",
}


def system_memory_gb() -> tuple[float, float]:
    """Return total and currently available physical memory in GiB."""
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("avail_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise OSError("GlobalMemoryStatusEx failed")
            divisor = 1024**3
            return status.total_phys / divisor, status.avail_phys / divisor

        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        divisor = 1024**3
        return (
            page_size * total_pages / divisor,
            page_size * available_pages / divisor,
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return 0.0, 0.0


def estimated_memory_gb(worker_count: int) -> float:
    return BASE_MEMORY_GB + max(1, worker_count) * MEMORY_PER_WORKER_GB


def recommended_worker_count(
    total_memory_gb: float,
    available_memory_gb: float,
    cpu_count: int | None = None,
) -> int:
    processors = max(1, cpu_count or os.cpu_count() or 1)
    by_cpu = max(1, processors // 2)
    if total_memory_gb <= 0 or available_memory_gb <= 0:
        return min(RECOMMENDATION_LIMIT, by_cpu, 2)
    usable = min(available_memory_gb, max(0.0, total_memory_gb - BASE_MEMORY_GB))
    by_memory = max(1, int(max(0.0, usable - BASE_MEMORY_GB) / MEMORY_PER_WORKER_GB))
    return max(1, min(RECOMMENDATION_LIMIT, by_cpu, by_memory))


def configure_cpu_budget(worker_count: int) -> int:
    threads_per_worker = max(1, (os.cpu_count() or 1) // max(1, worker_count))
    value = str(threads_per_worker)
    os.environ["OMP_NUM_THREADS"] = value
    os.environ["MKL_NUM_THREADS"] = value
    os.environ["OPENBLAS_NUM_THREADS"] = value
    try:
        import torch

        torch.set_num_threads(threads_per_worker)
    except (ImportError, RuntimeError):
        pass
    return threads_per_worker


def ensure_standard_streams() -> None:
    """Give console-oriented libraries a valid sink in a windowed executable."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(
                sys,
                name,
                open(os.devnull, "w", encoding="utf-8"),
            )


def program_data_dir() -> Path:
    root = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def bundled_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[2]


def default_settings() -> dict[str, Any]:
    videos = Path.home() / "Videos"
    data_dir = program_data_dir()
    return {
        "input_dir": str(videos / "StemFlowInput"),
        "output_dir": str(videos / "StemFlowOutput"),
        "data_dir": str(data_dir),
        "model_dir": str(data_dir / "models"),
        "work_dir": str(data_dir / "work"),
        "log_dir": str(data_dir / "logs"),
        "database_path": str(data_dir / "processing.db"),
        "report_path": str(data_dir / "processing_status.xlsx"),
        "model": DEFAULT_MODEL,
        "schedule_time": "00:00",
        "schedule_enabled": False,
        "stable_seconds": 10,
        "max_retries": 3,
        "output_suffix": "_vocals_only",
        "audio_bitrate": "192k",
        "recursive": True,
        "keep_failed_work": False,
        "video_copy": True,
        "worker_count": 1,
        "execution_mode": "cpu",
        "cpu_worker_count": 1,
        "gpu_worker_count": 1,
        "gpu_prepare_threads": 2,
        "gpu_compose_threads": 2,
        "gpu_prefetch": 2,
        "gpu_cpu_threads": 4,
        "gpu_batch_size": 2,
        "gpu_segment_size": 256,
        "concatenate_by_folder": False,
        "cuda_index_url": CUDA_INDEX_URL,
    }


def settings_path() -> Path:
    return program_data_dir() / "config.json"


def load_settings(path: Path | None = None) -> dict[str, Any]:
    target = path or settings_path()
    settings = default_settings()
    if target.is_file():
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("配置文件格式无效")
        settings.update(raw)
    return settings


def save_settings(settings: dict[str, Any], path: Path | None = None) -> Path:
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def prepare_bundled_assets(settings: dict[str, Any]) -> None:
    model_dir = Path(str(settings["model_dir"]))
    model_dir.mkdir(parents=True, exist_ok=True)
    bundled_models = bundled_root() / "models"
    if not bundled_models.is_dir():
        return
    for source in bundled_models.iterdir():
        if source.is_file():
            target = model_dir / source.name
            if not target.exists() or target.stat().st_size != source.stat().st_size:
                shutil.copy2(source, target)


def service_config(settings: dict[str, Any]) -> ServiceConfig:
    return ServiceConfig.from_mapping(settings, base=settings_path().parent)


def configure_scheduled_task(enabled: bool, schedule_time: str) -> None:
    if os.name != "nt":
        return
    if enabled:
        executable = Path(sys.executable).resolve()
        if getattr(sys, "frozen", False):
            task_command = f'"{executable}" --run-scheduled'
        else:
            task_command = (
                f'"{executable}" -m app.gui --run-scheduled --gpu-runtime'
            )
        command = [
            "schtasks.exe",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            task_command,
            "/SC",
            "DAILY",
            "/ST",
            schedule_time,
            "/RU",
            "SYSTEM",
            "/RL",
            "HIGHEST",
            "/F",
        ]
    else:
        command = [
            "schtasks.exe",
            "/Delete",
            "/TN",
            TASK_NAME,
            "/F",
        ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 and enabled:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or "Windows 计划任务设置失败")


def open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class QueueLogHandler(logging.Handler):
    def __init__(self, target: queue.Queue[tuple[str, Any]]) -> None:
        super().__init__()
        self.target = target
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        self.target.put(("log", self.format(record)))


class StemFlowGUI:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.runtime_installer: threading.Thread | None = None
        self.update_worker: threading.Thread | None = None
        self.pending_update_installer: Path | None = None
        self.concurrency: ConcurrencyController | None = None
        self.stop_requested = threading.Event()
        self.settings = load_settings()
        prepare_bundled_assets(self.settings)

        self.input_var = StringVar(value=str(self.settings["input_dir"]))
        self.output_var = StringVar(value=str(self.settings["output_dir"]))
        self.model_var = StringVar(value=str(self.settings.get("model", DEFAULT_MODEL)))
        configured_mode = str(self.settings.get("execution_mode", "cpu"))
        self.execution_mode = StringVar(
            value=EXECUTION_MODES.get(configured_mode, EXECUTION_MODES["cpu"])
        )
        self.cpu_worker_count = StringVar(
            value=str(
                self.settings.get(
                    "cpu_worker_count",
                    self.settings.get("worker_count", 1),
                )
            )
        )
        self.gpu_worker_count = StringVar(
            value=str(self.settings.get("gpu_worker_count", 1))
        )
        self.gpu_prepare_threads = StringVar(
            value=str(self.settings.get("gpu_prepare_threads", 2))
        )
        self.gpu_compose_threads = StringVar(
            value=str(self.settings.get("gpu_compose_threads", 2))
        )
        self.gpu_prefetch = StringVar(
            value=str(self.settings.get("gpu_prefetch", 2))
        )
        self.gpu_cpu_threads = StringVar(
            value=str(self.settings.get("gpu_cpu_threads", 4))
        )
        self.gpu_batch_size = StringVar(
            value=str(self.settings.get("gpu_batch_size", 2))
        )
        self.gpu_segment_size = StringVar(
            value=str(self.settings.get("gpu_segment_size", 256))
        )
        self.concatenate_by_folder = BooleanVar(
            value=bool(self.settings.get("concatenate_by_folder", False))
        )
        self.schedule_enabled = BooleanVar(
            value=bool(self.settings.get("schedule_enabled", False))
        )
        self.schedule_time = StringVar(
            value=str(self.settings.get("schedule_time", "00:00"))
        )
        self.status_var = StringVar(value="就绪")
        self.summary_var = StringVar(value="选择输入和输出文件夹后开始处理")
        self.resource_var = StringVar()
        self.device_var = StringVar(value="正在检测硬件…")
        self.cuda_url_var = StringVar(value=CUDA_INDEX_URL)
        self.total_memory_gb, self.available_memory_gb = system_memory_gb()
        self.hardware = runtime_hardware()
        self.cuda_ready = cuda_runtime_available() if runtime_ready() else False
        self.recommended_workers = recommended_worker_count(
            self.total_memory_gb,
            self.available_memory_gb,
        )
        self.recommended_gpu_workers = recommended_gpu_workers(self.hardware.gpu)

        self._configure_window()
        self._build_ui()
        self._refresh_jobs()
        self.root.after(200, self._drain_events)
        self.root.after(750, self._poll_jobs)
        self.root.after(2000, lambda: self._check_for_updates(manual=False))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title("StemFlow 视频纯人声工具")
        self.root.geometry("1180x800")
        self.root.minsize(980, 680)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", foreground="#5d6472")
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", foreground="#6349cf")
        style.configure("Treeview", rowheight=30)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)

        heading = ttk.Frame(outer)
        heading.pack(fill="x", pady=(0, 18))
        heading_left = ttk.Frame(heading)
        heading_left.pack(side="left", fill="x", expand=True)
        ttk.Label(
            heading_left,
            text=f"StemFlow  {__version__}",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            heading_left,
            text="选择文件夹，自动去除视频 BGM，并把人声重新合成回原画面。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        self.update_button = ttk.Button(
            heading,
            text="检查更新",
            command=lambda: self._check_for_updates(manual=True),
        )
        self.update_button.pack(side="right", anchor="n")

        settings = ttk.LabelFrame(outer, text="处理设置", padding=14)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="输入文件夹").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.input_var).grid(
            row=0, column=1, sticky="ew", padx=10
        )
        ttk.Button(
            settings,
            text="选择…",
            command=lambda: self._choose_directory(self.input_var),
        ).grid(row=0, column=2)

        ttk.Label(settings, text="输出文件夹").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Entry(settings, textvariable=self.output_var).grid(
            row=1, column=1, sticky="ew", padx=10, pady=(10, 0)
        )
        ttk.Button(
            settings,
            text="选择…",
            command=lambda: self._choose_directory(self.output_var),
        ).grid(row=1, column=2, pady=(10, 0))

        ttk.Label(settings, text="运行设备").grid(
            row=2, column=0, sticky="w", pady=(10, 0)
        )
        device_frame = ttk.Frame(settings)
        device_frame.grid(row=2, column=1, sticky="w", padx=10, pady=(10, 0))
        mode_picker = ttk.Combobox(
            device_frame,
            textvariable=self.execution_mode,
            values=tuple(EXECUTION_MODES.values()),
            state="readonly",
            width=12,
        )
        mode_picker.pack(side="left")
        mode_picker.bind("<<ComboboxSelected>>", self._apply_live_concurrency)
        ttk.Label(
            device_frame,
            textvariable=self.device_var,
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(12, 0))

        self.install_cuda_button = ttk.Button(
            settings,
            text="启用内置 CUDA",
            command=self._install_cuda,
        )
        self.install_cuda_button.grid(row=2, column=2, pady=(10, 0))

        ttk.Label(settings, text="实时并发").grid(
            row=3, column=0, sticky="w", pady=(10, 0)
        )
        worker_frame = ttk.Frame(settings)
        worker_frame.grid(row=3, column=1, sticky="w", padx=10, pady=(10, 0))
        ttk.Label(worker_frame, text="CPU").pack(side="left")
        self.cpu_worker_picker = ttk.Combobox(
            worker_frame,
            textvariable=self.cpu_worker_count,
            values=tuple(str(value) for value in range(17)),
            state="normal",
            width=5,
        )
        self.cpu_worker_picker.pack(side="left", padx=(5, 14))
        self.cpu_worker_picker.bind(
            "<<ComboboxSelected>>",
            self._apply_live_concurrency,
        )
        self.cpu_worker_picker.bind("<Return>", self._apply_live_concurrency)
        self.cpu_worker_picker.bind("<FocusOut>", self._apply_live_concurrency)
        ttk.Label(worker_frame, text="GPU").pack(side="left")
        self.gpu_worker_picker = ttk.Combobox(
            worker_frame,
            textvariable=self.gpu_worker_count,
            values=tuple(str(value) for value in range(17)),
            state="normal",
            width=5,
        )
        self.gpu_worker_picker.pack(side="left", padx=(5, 14))
        self.gpu_worker_picker.bind(
            "<<ComboboxSelected>>",
            self._apply_live_concurrency,
        )
        self.gpu_worker_picker.bind("<Return>", self._apply_live_concurrency)
        self.gpu_worker_picker.bind("<FocusOut>", self._apply_live_concurrency)
        ttk.Button(
            worker_frame,
            text="使用建议值",
            command=self._use_recommended_workers,
        ).pack(side="left")

        ttk.Label(settings, text="GPU 流水线").grid(
            row=4,
            column=0,
            sticky="w",
            pady=(10, 0),
        )
        pipeline_frame = ttk.Frame(settings)
        pipeline_frame.grid(
            row=4,
            column=1,
            sticky="w",
            padx=10,
            pady=(10, 0),
        )
        for label, variable, values in (
            ("预处理", self.gpu_prepare_threads, range(1, 9)),
            ("合成", self.gpu_compose_threads, range(1, 9)),
            ("预取", self.gpu_prefetch, range(9)),
            ("GPU辅助CPU", self.gpu_cpu_threads, range(1, 9)),
            ("GPU批量", self.gpu_batch_size, range(1, 17)),
            ("GPU分块", self.gpu_segment_size, (32, 64, 128, 256, 512)),
        ):
            ttk.Label(pipeline_frame, text=label).pack(side="left")
            ttk.Combobox(
                pipeline_frame,
                textvariable=variable,
                values=tuple(str(value) for value in values),
                state="normal",
                width=4,
            ).pack(side="left", padx=(4, 12))

        ttk.Label(
            settings,
            textvariable=self.resource_var,
            style="Subtitle.TLabel",
        ).grid(row=5, column=1, sticky="w", padx=10, pady=(6, 0))

        cuda_link = ttk.Frame(settings)
        cuda_link.grid(row=6, column=1, sticky="w", padx=10, pady=(6, 0))
        ttk.Label(cuda_link, text="内置 CUDA 来源：").pack(side="left")
        ttk.Label(
            cuda_link,
            textvariable=self.cuda_url_var,
            style="Subtitle.TLabel",
        ).pack(side="left")
        ttk.Button(
            cuda_link,
            text="复制",
            command=self._copy_cuda_url,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            cuda_link,
            text="打开 CUDA 日志",
            command=self._open_cuda_log,
        ).pack(side="left", padx=(8, 0))

        collection_frame = ttk.Frame(settings)
        collection_frame.grid(row=7, column=1, sticky="w", padx=10, pady=(10, 0))
        ttk.Checkbutton(
            collection_frame,
            text="每个子文件夹处理完成后，各生成一个人声版合集",
            variable=self.concatenate_by_folder,
        ).pack(side="left")

        schedule_frame = ttk.Frame(settings)
        schedule_frame.grid(row=8, column=1, sticky="w", padx=10, pady=(10, 0))
        ttk.Checkbutton(
            schedule_frame,
            text="每天自动运行",
            variable=self.schedule_enabled,
        ).pack(side="left")
        ttk.Entry(
            schedule_frame,
            textvariable=self.schedule_time,
            width=8,
        ).pack(side="left", padx=(10, 5))
        ttk.Label(schedule_frame, text="例如 00:00").pack(side="left")
        ttk.Button(
            settings,
            text="保存定时",
            command=self._save_schedule,
        ).grid(row=8, column=2, pady=(10, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=14)
        self.start_button = ttk.Button(
            actions,
            text="开始处理",
            style="Accent.TButton",
            command=self._start,
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            actions,
            text="处理完当前视频后停止",
            command=self._request_stop,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(
            actions,
            text="打开输出文件夹",
            command=lambda: open_path(Path(self.output_var.get())),
        ).pack(side="left", padx=8)
        ttk.Label(actions, textvariable=self.status_var, style="Status.TLabel").pack(
            side="right"
        )

        ttk.Label(outer, textvariable=self.summary_var).pack(anchor="w", pady=(0, 8))

        table_frame = ttk.LabelFrame(outer, text="处理记录", padding=8)
        table_frame.pack(fill="both", expand=True)
        columns = ("status", "file", "device", "duration", "output", "error")
        self.jobs = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=4,
        )
        self.jobs.heading("status", text="状态")
        self.jobs.heading("file", text="文件")
        self.jobs.heading("device", text="设备")
        self.jobs.heading("duration", text="耗时")
        self.jobs.heading("output", text="输出")
        self.jobs.heading("error", text="失败原因")
        self.jobs.column("status", width=110, stretch=False)
        self.jobs.column("file", width=245)
        self.jobs.column("device", width=70, stretch=False)
        self.jobs.column("duration", width=90, stretch=False)
        self.jobs.column("output", width=300)
        self.jobs.column("error", width=260)
        self.jobs.tag_configure("completed", foreground="#047857")
        self.jobs.tag_configure("failed", foreground="#B91C1C")
        self.jobs.tag_configure("active", foreground="#6D28D9")
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.jobs.yview,
        )
        self.jobs.configure(yscrollcommand=scrollbar.set)
        self.jobs.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="both", pady=(12, 0))
        self.log = ScrolledText(
            log_frame,
            height=3,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        self.log.pack(fill="both", expand=True)
        self._update_mode_controls()
        self._update_resource_label()

    def _use_recommended_workers(self) -> None:
        mode = self._selected_mode()
        if mode == "hybrid":
            self.cpu_worker_count.set(str(self.recommended_workers))
            self.gpu_worker_count.set(str(self.recommended_gpu_workers))
        elif mode == "cuda":
            self.gpu_worker_count.set(str(self.recommended_gpu_workers))
        else:
            self.cpu_worker_count.set(str(self.recommended_workers))
        self._apply_live_concurrency()

    def _selected_mode(self) -> str:
        return MODE_LABEL_TO_KEY.get(self.execution_mode.get(), "cpu")

    def _selected_allocation(self) -> tuple[int, int]:
        mode = self._selected_mode()
        try:
            cpu = int(self.cpu_worker_count.get().strip()) if mode != "cuda" else 0
            gpu = int(self.gpu_worker_count.get().strip()) if mode != "cpu" else 0
        except ValueError as error:
            raise ValueError("CPU 和 GPU 并发必须填写整数") from error
        if cpu < 0 or gpu < 0:
            raise ValueError("CPU 和 GPU 并发不能为负数")
        if cpu + gpu < 1:
            raise ValueError("CPU 和 GPU 可以单独设为 0，但不能同时为 0")
        return cpu, gpu

    def _update_mode_controls(self) -> None:
        mode = self._selected_mode()
        self.cpu_worker_picker.configure(
            state="normal" if mode != "cuda" else "disabled"
        )
        self.gpu_worker_picker.configure(
            state="normal" if mode != "cpu" else "disabled"
        )

    def _apply_live_concurrency(self, _event: object | None = None) -> None:
        self._update_mode_controls()
        try:
            cpu, gpu = self._selected_allocation()
        except ValueError as error:
            messagebox.showerror("并发设置错误", str(error))
            return
        self._update_resource_label()
        if self.concurrency is None:
            return
        if gpu and not self.cuda_ready:
            messagebox.showwarning(
                "CUDA 尚未启用",
                "请先安装并验证 NVIDIA CUDA 加速组件。",
            )
            return
        self.concurrency.set_allocation(
            cpu_workers=cpu,
            gpu_workers=gpu,
        )
        self.settings.update(
            {
                "execution_mode": self._selected_mode(),
                "cpu_worker_count": int(self.cpu_worker_count.get()),
                "gpu_worker_count": int(self.gpu_worker_count.get()),
                "worker_count": cpu + gpu,
            }
        )
        save_settings(self.settings)
        self._append_log(
            f"并发已实时调整：CPU {cpu}，GPU {gpu}；"
            "已在运行的任务会完成，后续任务按新设置调度。"
        )

    def _update_resource_label(self, _event: object | None = None) -> None:
        try:
            cpu, gpu = self._selected_allocation()
        except ValueError:
            cpu, gpu = 1, 0
        estimate = estimated_memory_gb(cpu + gpu)
        try:
            gpu_batch_size = max(1, int(self.gpu_batch_size.get()))
        except ValueError:
            gpu_batch_size = 1
        gpu_estimate = (
            gpu_memory_estimate_gb(gpu, gpu_batch_size) if gpu else 0.0
        )
        if self.total_memory_gb > 0:
            text = (
                f"内存：总计 {self.total_memory_gb:.1f} GB，可用 "
                f"{self.available_memory_gb:.1f} GB；预计需要约 {estimate:.1f} GB；"
                f"CPU 建议 {self.recommended_workers}"
            )
            if self.hardware.gpu:
                text += (
                    f"；显存可用 {self.hardware.gpu.free_memory_gb:.1f} GB"
                    f"，GPU {gpu} 并发预计约 {gpu_estimate:.1f} GB"
                    f"，建议 {self.recommended_gpu_workers}"
                )
            self.resource_var.set(text)
        else:
            self.resource_var.set(
                f"预计需要约 {estimate:.1f} GB；建议从 1 个并发开始"
            )
        if self.cuda_ready:
            gpu_name = self.hardware.gpu.name if self.hardware.gpu else "NVIDIA GPU"
            self.device_var.set(f"CUDA 可用：{gpu_name}")
        else:
            self.device_var.set(self.hardware.detail)
        if runtime_ready():
            marker = read_marker()
            self.cuda_url_var.set(str(marker.get("cuda_index_url", CUDA_INDEX_URL)))
        self.install_cuda_button.configure(
            text=(
                "CUDA 已安装"
                if self.cuda_ready
                else "启用内置 CUDA"
            ),
            state=(
                "disabled"
                if self.cuda_ready
                else "normal"
            ),
        )

    def _copy_cuda_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.cuda_url_var.get())
        self.status_var.set("CUDA 下载地址已复制")

    def _check_for_updates(self, *, manual: bool) -> None:
        if self.pending_update_installer is not None:
            self._launch_update_installer(self.pending_update_installer)
            return
        if self.update_worker and self.update_worker.is_alive():
            return
        if (
            (self.worker and self.worker.is_alive())
            or (self.runtime_installer and self.runtime_installer.is_alive())
        ):
            if manual:
                messagebox.showwarning(
                    "暂时不能更新",
                    "请等待当前视频任务或 CUDA 安装完成后再检查更新。",
                )
            return
        self.update_button.configure(state="disabled", text="正在检查…")
        if manual:
            self.status_var.set("正在检查 GitHub Release 更新…")

        def check() -> None:
            try:
                update = check_for_update(__version__)
                self.events.put(("update-checked", (manual, update)))
            except Exception as error:
                self.events.put(
                    (
                        "update-check-error",
                        (manual, f"{type(error).__name__}: {error}"),
                    )
                )

        self.update_worker = threading.Thread(
            target=check,
            daemon=True,
            name="stemflow-update-check",
        )
        self.update_worker.start()

    def _confirm_update(self, update: AvailableUpdate) -> None:
        size_mb = update.size_bytes / (1024**2) if update.size_bytes else 0
        notes = update.notes.strip()
        if len(notes) > 700:
            notes = notes[:700].rstrip() + "…"
        detail = (
            f"发现 StemFlow {update.version}\n"
            f"核心更新包约 {size_mb:.0f} MB。\n\n"
            "本次只更新软件本体，不会下载、删除或重装已经启用的 "
            "CUDA/PyTorch GPU 环境。"
        )
        if notes:
            detail += f"\n\n更新说明：\n{notes}"
        detail += "\n\n是否下载并安装？"
        if not messagebox.askyesno("发现新版本", detail):
            self.status_var.set(f"已取消更新到 {update.version}")
            return
        self._download_available_update(update)

    def _download_available_update(self, update: AvailableUpdate) -> None:
        self.update_button.configure(state="disabled", text="正在下载…")
        self.status_var.set(f"正在下载 StemFlow {update.version}…")

        def progress(current: int, total: int) -> None:
            percent = int(current * 100 / total) if total > 0 else 0
            self.events.put(("update-progress", (update.version, percent)))

        def download() -> None:
            try:
                target = download_update(
                    update,
                    program_data_dir() / "updates",
                    progress,
                )
                self.events.put(("update-ready", target))
            except Exception as error:
                self.events.put(
                    (
                        "update-download-error",
                        f"{type(error).__name__}: {error}",
                    )
                )

        self.update_worker = threading.Thread(
            target=download,
            daemon=True,
            name="stemflow-update-download",
        )
        self.update_worker.start()

    def _launch_update_installer(self, installer: Path) -> None:
        if self.worker and self.worker.is_alive():
            self.pending_update_installer = installer
            self.update_button.configure(
                state="normal",
                text="安装已下载更新",
            )
            messagebox.showinfo(
                "更新已经下载",
                "请等待当前视频处理完成，再点击“安装已下载更新”。",
            )
            return
        try:
            subprocess.Popen(
                [
                    str(installer),
                    "/SILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/CLOSEAPPLICATIONS",
                ],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            messagebox.showerror("无法启动更新", f"{installer}\n\n{error}")
            self.update_button.configure(state="normal", text="安装已下载更新")
            return
        self.pending_update_installer = None
        self.status_var.set("更新安装程序已启动，StemFlow 即将关闭")
        self.root.after(800, self.root.destroy)

    @staticmethod
    def _cuda_log_path() -> Path:
        return program_data_dir() / "logs" / "cuda-install.log"

    def _open_cuda_log(self) -> None:
        path = self._cuda_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open", str(path)])
        except OSError as error:
            messagebox.showerror("无法打开日志", f"{path}\n\n{error}")

    def _cuda_log_tail(self, line_count: int = 14) -> str:
        path = self._cuda_log_path()
        if not path.is_file():
            return "CUDA 日志尚未生成。"
        try:
            lines = path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
            return "\n".join(lines[-line_count:])
        except OSError as error:
            return f"无法读取 CUDA 日志：{error}"

    @staticmethod
    def _cuda_error_advice(error: str) -> str:
        lowered = error.lower()
        if "驱动" in error or "driver" in lowered:
            return "请更新 NVIDIA 官方驱动、重启 Windows，再重新启用。"
        if "空间" in error or "disk" in lowered or "no space" in lowered:
            return "请释放 CUDA 运行磁盘空间，至少保留 12 GB，再重试。"
        if "access" in lowered or "permission" in lowered or "拒绝访问" in error:
            return "请关闭 StemFlow 后以管理员身份启动一次，再重新启用。"
        if "校验失败" in error or "资源缺失" in error:
            return "离线安装资源不完整，请确认 EXE 与全部 BIN 同目录后重新安装。"
        return "不需要手工解压。可点击“重新启用 CUDA”，程序会清理临时目录后重试。"

    def _install_cuda(self) -> None:
        if self.runtime_installer and self.runtime_installer.is_alive():
            return
        if not self.hardware.gpu:
            messagebox.showwarning(
                "未检测到 NVIDIA GPU",
                "系统没有通过 nvidia-smi 检测到 NVIDIA GPU。"
                "请先正确安装 NVIDIA 驱动。",
            )
            return
        if offline_wheelhouse() is None:
            messagebox.showerror(
                "CUDA 离线资源缺失",
                "当前安装包没有包含 CUDA PyTorch 离线资源。"
                "请使用 StemFlow 完整离线 GPU 安装套件重新安装。",
            )
            return
        try:
            preflight = cuda_install_preflight()
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            messagebox.showerror(
                "CUDA 启用前检查失败",
                f"{detail}\n\n{self._cuda_error_advice(detail)}",
            )
            return
        if not messagebox.askyesno(
            "启用内置 NVIDIA CUDA 加速",
            "安装套件已包含兼容的 CUDA PyTorch 组件，启用过程无需联网。\n\n"
            f"显卡：{preflight['gpu_name']}\n"
            f"驱动：{preflight['driver_version']}\n"
            f"运行磁盘可用：{preflight['free_disk_gb']:.1f} GB\n\n"
            f"组件官方来源：{CUDA_INDEX_URL}\n\n"
            "解压安装后预计占用 7–10 GB 磁盘空间。是否继续？",
        ):
            return
        self.install_cuda_button.configure(state="disabled", text="正在安装…")
        self.status_var.set("正在校验并安装内置 CUDA 加速组件")
        self.runtime_installer = threading.Thread(
            target=self._run_cuda_install,
            daemon=True,
            name="stemflow-cuda-installer",
        )
        self.runtime_installer.start()

    def _run_cuda_install(self) -> None:
        log_path = self._cuda_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def record(line: str) -> None:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"{timestamp} | {line}\n")
            self.events.put(("cuda-progress", line))

        try:
            install_cuda_runtime(record)
            self.events.put(("cuda-installed", None))
        except Exception as error:
            self.events.put(
                ("cuda-error", f"{type(error).__name__}: {error}")
            )

    def _choose_directory(self, target: StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=target.get() or str(Path.home()))
        if chosen:
            target.set(chosen)

    def _current_settings(self) -> dict[str, Any]:
        settings = dict(self.settings)
        data_dir = program_data_dir()
        cpu_workers, gpu_workers = self._selected_allocation()
        settings.update(
            {
                "input_dir": self.input_var.get().strip(),
                "output_dir": self.output_var.get().strip(),
                "data_dir": str(data_dir),
                "model_dir": str(data_dir / "models"),
                "work_dir": str(data_dir / "work"),
                "log_dir": str(data_dir / "logs"),
                "database_path": str(data_dir / "processing.db"),
                "report_path": str(data_dir / "processing_status.xlsx"),
                "model": self.model_var.get(),
                "worker_count": cpu_workers + gpu_workers,
                "execution_mode": self._selected_mode(),
                "cpu_worker_count": int(self.cpu_worker_count.get()),
                "gpu_worker_count": int(self.gpu_worker_count.get()),
                "gpu_prepare_threads": int(self.gpu_prepare_threads.get()),
                "gpu_compose_threads": int(self.gpu_compose_threads.get()),
                "gpu_prefetch": int(self.gpu_prefetch.get()),
                "gpu_cpu_threads": int(self.gpu_cpu_threads.get()),
                "gpu_batch_size": int(self.gpu_batch_size.get()),
                "gpu_segment_size": int(self.gpu_segment_size.get()),
                "concatenate_by_folder": self.concatenate_by_folder.get(),
                "cuda_index_url": CUDA_INDEX_URL,
                "schedule_enabled": self.schedule_enabled.get(),
                "schedule_time": self.schedule_time.get().strip(),
            }
        )
        return settings

    def _validate_and_save(self) -> ServiceConfig:
        _cpu_workers, gpu_workers = self._selected_allocation()
        if gpu_workers and not self.cuda_ready:
            raise ValueError("当前模式需要先安装并验证 NVIDIA CUDA 加速组件")
        settings = self._current_settings()
        config = service_config(settings)
        config.prepare_directories()
        save_settings(settings)
        self.settings = settings
        prepare_bundled_assets(settings)
        return config

    def _save_schedule(self) -> None:
        try:
            self._validate_and_save()
            configure_scheduled_task(
                self.schedule_enabled.get(),
                self.schedule_time.get().strip(),
            )
        except Exception as error:
            messagebox.showerror(
                "定时任务设置失败",
                f"{error}\n\n在 Windows Server 上请右键程序并选择“以管理员身份运行”。",
            )
            return
        messagebox.showinfo("已保存", "处理目录和定时任务已经更新。")

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._validate_and_save()
        except Exception as error:
            messagebox.showerror("配置错误", str(error))
            return
        cpu_workers, gpu_workers = self._selected_allocation()
        if gpu_workers and not self.cuda_ready:
            messagebox.showwarning(
                "CUDA 尚未启用",
                "当前选择需要 NVIDIA GPU。请先点击“启用内置 CUDA”，"
                "安装验证成功后再开始。",
            )
            return
        self.total_memory_gb, self.available_memory_gb = system_memory_gb()
        required_memory = estimated_memory_gb(config.worker_count)
        if (
            self.available_memory_gb > 0
            and required_memory > self.available_memory_gb
            and not messagebox.askyesno(
                "内存可能不足",
                f"{config.worker_count} 个并发预计需要约 {required_memory:.1f} GB，"
                f"当前可用内存约 {self.available_memory_gb:.1f} GB。\n\n"
                "内存不足可能导致处理失败，仍然继续吗？",
            )
        ):
            return
        if (
            gpu_workers
            and self.hardware.gpu
            and gpu_memory_estimate_gb(
                gpu_workers,
                config.gpu_batch_size,
            )
            > self.hardware.gpu.free_memory_gb
            and not messagebox.askyesno(
                "显存可能不足",
                f"{gpu_workers} 个 GPU 并发预计需要约 "
                f"{gpu_memory_estimate_gb(gpu_workers, config.gpu_batch_size):.1f} GB 显存，"
                f"当前可用约 {self.hardware.gpu.free_memory_gb:.1f} GB。\n\n"
                "显存不足可能导致当前集失败，仍然继续吗？",
            )
        ):
            return

        self.stop_requested.clear()
        self.concurrency = ConcurrencyController(1)
        self.concurrency.set_allocation(
            cpu_workers=cpu_workers,
            gpu_workers=gpu_workers,
        )
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("正在启动…")
        self.worker = threading.Thread(
            target=self._run_batches,
            args=(config,),
            daemon=True,
            name="stemflow-gui-worker",
        )
        self.worker.start()

    def _run_batches(self, config: ServiceConfig) -> None:
        try:
            runner, _repository, logger = make_runner(config)
            handler = QueueLogHandler(self.events)
            logger.addHandler(handler)
            cpu_workers, gpu_workers = (
                self.concurrency.get_allocation()
                if self.concurrency
                else (config.worker_count, 0)
            )
            threads_per_worker = configure_cpu_budget(max(1, cpu_workers))
            logger.info(
                "Starting workers: CPU=%s, CUDA=%s; %s CPU thread(s) per worker",
                cpu_workers,
                gpu_workers,
                threads_per_worker,
            )
            runner.run_once(
                max_workers=config.worker_count,
                stop_event=self.stop_requested,
                concurrency=self.concurrency,
            )
            self.events.put(("refresh", None))
            self.events.put(("done", None))
        except Exception as error:
            self.events.put(("error", f"{type(error).__name__}: {error}"))

    def _request_stop(self) -> None:
        self.stop_requested.set()
        self.status_var.set("将在当前并发任务完成后停止")
        self.stop_button.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    line = str(payload)
                    self._append_log(line)
                    if "Scan complete:" in line:
                        self.status_var.set("扫描完成，正在准备任务…")
                    elif "Warming " in line and "CUDA worker" in line:
                        self.status_var.set("正在加载 CUDA 常驻模型…")
                    elif "CUDA worker" in line and " ready;" in line:
                        self.status_var.set("CUDA 模型已加载，正在处理…")
                elif kind == "cuda-progress":
                    line = str(payload)
                    self.status_var.set(line)
                    self._append_log(f"CUDA | {line}")
                elif kind == "refresh":
                    self._refresh_jobs()
                elif kind == "done":
                    self._set_idle("处理完成" if not self.stop_requested.is_set() else "已停止")
                    self._refresh_jobs()
                elif kind == "error":
                    self._set_idle("运行失败")
                    self._append_log(str(payload))
                    messagebox.showerror("处理失败", str(payload))
                elif kind == "cuda-installed":
                    self.cuda_ready = cuda_runtime_available()
                    self._update_resource_label()
                    self.status_var.set("CUDA 安装成功，可选择 GPU 或混合模式")
                    messagebox.showinfo(
                        "CUDA 安装成功",
                        "NVIDIA CUDA 加速已经安装并验证成功。"
                        "现在可以选择“仅 NVIDIA GPU”或“CPU + NVIDIA GPU”。",
                    )
                elif kind == "cuda-error":
                    self.status_var.set("CUDA 安装失败，继续使用 CPU")
                    self.install_cuda_button.configure(
                        state="normal",
                        text="重新启用 CUDA",
                    )
                    detail = str(payload)
                    self._append_log(detail)
                    advice = self._cuda_error_advice(detail)
                    log_tail = self._cuda_log_tail()
                    messagebox.showerror(
                        "CUDA 安装失败",
                        f"{detail}\n\n{advice}\n\n"
                        f"最近日志：\n{log_tail}\n\n"
                        "CPU 模式仍可正常使用。界面可直接打开完整 CUDA 日志。",
                    )
                elif kind == "update-checked":
                    manual, update = payload
                    self.update_button.configure(
                        state="normal",
                        text="检查更新",
                    )
                    if update is None:
                        self.status_var.set(f"当前已是最新版本 {__version__}")
                        if manual:
                            messagebox.showinfo(
                                "没有新版本",
                                f"当前 StemFlow {__version__} 已是最新版本。",
                            )
                    else:
                        self._confirm_update(update)
                elif kind == "update-check-error":
                    manual, detail = payload
                    self.update_button.configure(
                        state="normal",
                        text="检查更新",
                    )
                    self._append_log(f"更新检查失败：{detail}")
                    if manual:
                        self.status_var.set("更新检查失败")
                        messagebox.showerror(
                            "更新检查失败",
                            f"{detail}\n\n请检查网络能否访问 GitHub。",
                        )
                elif kind == "update-progress":
                    version, percent = payload
                    self.status_var.set(
                        f"正在下载 StemFlow {version}：{percent}%"
                    )
                elif kind == "update-ready":
                    installer = Path(str(payload))
                    self.pending_update_installer = installer
                    self.update_button.configure(
                        state="normal",
                        text="安装已下载更新",
                    )
                    self._launch_update_installer(installer)
                elif kind == "update-download-error":
                    self.update_button.configure(
                        state="normal",
                        text="检查更新",
                    )
                    self.status_var.set("更新下载失败")
                    self._append_log(f"更新下载失败：{payload}")
                    messagebox.showerror(
                        "更新下载失败",
                        f"{payload}\n\n现有版本和 GPU 环境未受影响。",
                    )
        except queue.Empty:
            pass
        self.root.after(200, self._drain_events)

    def _set_idle(self, status: str) -> None:
        self.status_var.set(status)
        self.concurrency = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _append_log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 500:
            self.log.delete("1.0", f"{lines - 500}.0")
        self.log.configure(state="disabled")

    def _poll_jobs(self) -> None:
        self._refresh_jobs()
        self.root.after(750, self._poll_jobs)

    def _refresh_jobs(self) -> None:
        try:
            config = service_config(self._current_settings())
            repository = ProcessingRepository(config.database_path)
            records = repository.list_jobs(
                100,
                input_root=config.input_dir,
            )
            counts = repository.counts(config.input_dir)
        except Exception:
            return

        self.jobs.delete(*self.jobs.get_children())
        for job in records:
            duration_value = job.get("duration_seconds")
            if duration_value is None and job.get("started_at") and str(
                job.get("status")
            ) in {
                "processing",
                "extracting_audio",
                "separating_vocals",
                "composing_video",
            }:
                try:
                    started = datetime.fromisoformat(str(job["started_at"]))
                    duration_value = (
                        datetime.now(timezone.utc) - started
                    ).total_seconds()
                except ValueError:
                    duration_value = None
            duration = (
                f"{float(duration_value):.0f} 秒"
                if duration_value is not None
                else "—"
            )
            status = str(job["status"])
            tag = (
                "completed"
                if status == "completed"
                else "failed"
                if status == "failed"
                else "active"
                if status in {
                    "processing",
                    "extracting_audio",
                    "separating_vocals",
                    "composing_video",
                }
                else ""
            )
            self.jobs.insert(
                "",
                "end",
                iid=str(job["id"]),
                values=(
                    STATUS_LABELS.get(status, status),
                    Path(str(job["source_path"])).name,
                    str(job.get("device") or "—").upper(),
                    duration,
                    str(job["output_path"]) if status == "completed" else "—",
                    str(job.get("error") or "").replace("\n", " | ")[:240],
                ),
                tags=(tag,) if tag else (),
            )
        active = sum(
            counts.get(status, 0)
            for status in (
                "processing",
                "extracting_audio",
                "separating_vocals",
                "composing_video",
            )
        )
        self.summary_var.set(
            "全部 {total} · 处理中 {active} · 已完成 {completed} · "
            "失败 {failed} · 排队 {pending}".format(
                total=counts.get("total", 0),
                active=active,
                completed=counts.get("completed", 0),
                failed=counts.get("failed", 0),
                pending=counts.get("pending", 0),
            )
        )

    def _on_close(self) -> None:
        if self.runtime_installer and self.runtime_installer.is_alive():
            messagebox.showwarning(
                "CUDA 正在安装",
                "请等待 CUDA 组件下载和安装完成，避免留下不完整运行环境。",
            )
            return
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
                "任务正在运行",
                "当前视频仍在处理。请等待完成，或点击“处理完当前视频后停止”。",
            )
            return
        self.root.destroy()


def run_scheduled() -> int:
    try:
        settings = load_settings()
        prepare_bundled_assets(settings)
        config = service_config(settings)
        runner, _repository, _logger = make_runner(config)
        mode = str(settings.get("execution_mode", "cpu"))
        cpu_workers = (
            int(settings.get("cpu_worker_count", config.worker_count))
            if mode != "cuda"
            else 0
        )
        gpu_workers = (
            int(settings.get("gpu_worker_count", 1))
            if mode != "cpu"
            else 0
        )
        controller = ConcurrencyController(1)
        controller.set_allocation(
            cpu_workers=cpu_workers,
            gpu_workers=gpu_workers,
        )
        configure_cpu_budget(max(1, cpu_workers))
        runner.run_once(
            max_workers=cpu_workers + gpu_workers,
            concurrency=controller,
        )
        return 0
    except Exception:
        logging.exception("Scheduled StemFlow run failed")
        return 1


def main() -> int:
    ensure_standard_streams()
    if "--run-scheduled" in sys.argv:
        return run_scheduled()
    root = Tk()
    StemFlowGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
