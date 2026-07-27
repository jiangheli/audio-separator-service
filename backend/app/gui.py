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
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from app.cli.main import make_runner
from app.config import ServiceConfig
from app.repository import ProcessingRepository


APP_NAME = "StemFlow"
TASK_NAME = "StemFlow-Video-BGM-Removal"
DEFAULT_MODEL = "mdx"
MAX_WORKERS = 8
BASE_MEMORY_GB = 2.0
MEMORY_PER_WORKER_GB = 3.0
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
        return min(MAX_WORKERS, by_cpu, 2)
    usable = min(available_memory_gb, max(0.0, total_memory_gb - BASE_MEMORY_GB))
    by_memory = max(1, int(max(0.0, usable - BASE_MEMORY_GB) / MEMORY_PER_WORKER_GB))
    return max(1, min(MAX_WORKERS, by_cpu, by_memory))


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
        task_command = f'"{executable}" --run-scheduled'
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
        self.stop_requested = threading.Event()
        self.settings = load_settings()

        self.input_var = StringVar(value=str(self.settings["input_dir"]))
        self.output_var = StringVar(value=str(self.settings["output_dir"]))
        self.model_var = StringVar(value=str(self.settings.get("model", DEFAULT_MODEL)))
        self.worker_count = StringVar(
            value=str(self.settings.get("worker_count", 1))
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
        self.total_memory_gb, self.available_memory_gb = system_memory_gb()
        self.recommended_workers = recommended_worker_count(
            self.total_memory_gb,
            self.available_memory_gb,
        )

        self._configure_window()
        self._build_ui()
        self._refresh_jobs()
        self.root.after(200, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title("StemFlow 视频纯人声工具")
        self.root.geometry("1040x720")
        self.root.minsize(880, 620)
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
        ttk.Label(heading, text="StemFlow", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            heading,
            text="选择文件夹，自动去除视频 BGM，并把人声重新合成回原画面。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

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

        ttk.Label(settings, text="分离引擎").grid(
            row=2, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Label(
            settings,
            text="MDX 本地人声模型（已内置）",
            style="Subtitle.TLabel",
        ).grid(row=2, column=1, sticky="w", padx=10, pady=(10, 0))

        ttk.Label(settings, text="并发任务数").grid(
            row=3, column=0, sticky="w", pady=(10, 0)
        )
        worker_frame = ttk.Frame(settings)
        worker_frame.grid(row=3, column=1, sticky="w", padx=10, pady=(10, 0))
        worker_picker = ttk.Combobox(
            worker_frame,
            textvariable=self.worker_count,
            values=tuple(str(value) for value in range(1, MAX_WORKERS + 1)),
            state="readonly",
            width=6,
        )
        worker_picker.pack(side="left")
        worker_picker.bind("<<ComboboxSelected>>", self._update_resource_label)
        ttk.Button(
            worker_frame,
            text="使用建议值",
            command=self._use_recommended_workers,
        ).pack(side="left", padx=(10, 0))
        ttk.Label(
            settings,
            textvariable=self.resource_var,
            style="Subtitle.TLabel",
        ).grid(row=4, column=1, sticky="w", padx=10, pady=(6, 0))

        schedule_frame = ttk.Frame(settings)
        schedule_frame.grid(row=5, column=1, sticky="w", padx=10, pady=(10, 0))
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
        ).grid(row=5, column=2, pady=(10, 0))

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
        columns = ("status", "file", "duration", "output")
        self.jobs = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=9,
        )
        self.jobs.heading("status", text="状态")
        self.jobs.heading("file", text="文件")
        self.jobs.heading("duration", text="耗时")
        self.jobs.heading("output", text="输出")
        self.jobs.column("status", width=110, stretch=False)
        self.jobs.column("file", width=300)
        self.jobs.column("duration", width=90, stretch=False)
        self.jobs.column("output", width=390)
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
            height=7,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        self.log.pack(fill="both", expand=True)
        self._update_resource_label()

    def _use_recommended_workers(self) -> None:
        self.worker_count.set(str(self.recommended_workers))
        self._update_resource_label()

    def _update_resource_label(self, _event: object | None = None) -> None:
        selected = max(1, int(self.worker_count.get() or "1"))
        estimate = estimated_memory_gb(selected)
        if self.total_memory_gb > 0:
            self.resource_var.set(
                f"内存：总计 {self.total_memory_gb:.1f} GB，可用 "
                f"{self.available_memory_gb:.1f} GB；预计需要约 {estimate:.1f} GB；"
                f"建议 {self.recommended_workers} 个并发"
            )
        else:
            self.resource_var.set(
                f"预计需要约 {estimate:.1f} GB；建议从 1 个并发开始"
            )

    def _choose_directory(self, target: StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=target.get() or str(Path.home()))
        if chosen:
            target.set(chosen)

    def _current_settings(self) -> dict[str, Any]:
        settings = dict(self.settings)
        data_dir = program_data_dir()
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
                "worker_count": int(self.worker_count.get()),
                "schedule_enabled": self.schedule_enabled.get(),
                "schedule_time": self.schedule_time.get().strip(),
            }
        )
        return settings

    def _validate_and_save(self) -> ServiceConfig:
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

        self.stop_requested.clear()
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
            threads_per_worker = configure_cpu_budget(config.worker_count)
            logger.info(
                "Starting %s concurrent job(s), %s CPU thread(s) per worker",
                config.worker_count,
                threads_per_worker,
            )
            runner.run_once(
                max_workers=config.worker_count,
                stop_event=self.stop_requested,
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
                    self._append_log(str(payload))
                elif kind == "refresh":
                    self._refresh_jobs()
                elif kind == "done":
                    self._set_idle("处理完成" if not self.stop_requested.is_set() else "已停止")
                    self._refresh_jobs()
                elif kind == "error":
                    self._set_idle("运行失败")
                    self._append_log(str(payload))
                    messagebox.showerror("处理失败", str(payload))
        except queue.Empty:
            pass
        self.root.after(200, self._drain_events)

    def _set_idle(self, status: str) -> None:
        self.status_var.set(status)
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

    def _refresh_jobs(self) -> None:
        try:
            config = service_config(self._current_settings())
            repository = ProcessingRepository(config.database_path)
            records = repository.list_jobs(100)
            counts = repository.counts()
        except Exception:
            return

        self.jobs.delete(*self.jobs.get_children())
        for job in records:
            duration = (
                f"{float(job['duration_seconds']):.0f} 秒"
                if job.get("duration_seconds") is not None
                else "—"
            )
            self.jobs.insert(
                "",
                "end",
                values=(
                    STATUS_LABELS.get(str(job["status"]), str(job["status"])),
                    Path(str(job["source_path"])).name,
                    duration,
                    str(job["output_path"]),
                ),
            )
        self.summary_var.set(
            "全部 {total} · 已完成 {completed} · 失败 {failed} · 排队 {pending}".format(
                total=counts.get("total", 0),
                completed=counts.get("completed", 0),
                failed=counts.get("failed", 0),
                pending=counts.get("pending", 0),
            )
        )

    def _on_close(self) -> None:
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
        configure_cpu_budget(config.worker_count)
        runner.run_once(max_workers=config.worker_count)
        return 0
    except Exception:
        logging.exception("Scheduled StemFlow run failed")
        return 1


def main() -> int:
    if "--run-scheduled" in sys.argv:
        return run_scheduled()
    root = Tk()
    StemFlowGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
