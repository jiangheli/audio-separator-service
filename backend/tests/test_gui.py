from __future__ import annotations

import logging
import queue
import sys
from pathlib import Path

from app import gui


def test_gui_settings_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    settings = gui.default_settings()
    settings["input_dir"] = str(tmp_path / "input")
    settings["output_dir"] = str(tmp_path / "output")
    settings["schedule_enabled"] = True
    settings["schedule_time"] = "01:30"
    settings["worker_count"] = 3

    saved = gui.save_settings(settings)
    loaded = gui.load_settings()
    config = gui.service_config(loaded)

    assert saved == tmp_path / "ProgramData" / "StemFlow" / "config.json"
    assert config.input_dir == tmp_path / "input"
    assert config.output_dir == tmp_path / "output"
    assert loaded["schedule_enabled"] is True
    assert loaded["schedule_time"] == "01:30"
    assert config.worker_count == 3


def test_prepare_bundled_assets_copies_model_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    models = bundle / "models"
    models.mkdir(parents=True)
    (models / "model.onnx").write_bytes(b"model-data")
    destination = tmp_path / "data" / "models"
    monkeypatch.setattr(gui, "bundled_root", lambda: bundle)

    gui.prepare_bundled_assets({"model_dir": str(destination)})
    gui.prepare_bundled_assets({"model_dir": str(destination)})

    assert (destination / "model.onnx").read_bytes() == b"model-data"


def test_queue_log_handler_emits_compact_line() -> None:
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    handler = gui.QueueLogHandler(events)
    record = logging.LogRecord(
        "stemflow",
        logging.INFO,
        __file__,
        1,
        "processing %s",
        ("video.mp4",),
        None,
    )

    handler.emit(record)

    kind, line = events.get_nowait()
    assert kind == "log"
    assert "INFO" in str(line)
    assert "processing video.mp4" in str(line)


def test_memory_estimate_and_worker_recommendation() -> None:
    assert gui.estimated_memory_gb(1) == 5.0
    assert gui.estimated_memory_gb(4) == 14.0
    assert gui.recommended_worker_count(16.0, 12.0, cpu_count=8) == 3
    assert gui.recommended_worker_count(64.0, 50.0, cpu_count=16) == 8


def test_windowed_runtime_gets_standard_stream_sinks(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    gui.ensure_standard_streams()

    assert sys.stdout is not None
    assert sys.stderr is not None
    sys.stdout.close()
    sys.stderr.close()


def test_cuda_error_advice_is_actionable() -> None:
    assert "更新 NVIDIA" in gui.StemFlowGUI._cuda_error_advice(
        "driver version is too old"
    )
    assert "12 GB" in gui.StemFlowGUI._cuda_error_advice(
        "磁盘空间不足"
    )
    assert "管理员" in gui.StemFlowGUI._cuda_error_advice(
        "Access is denied"
    )
