from __future__ import annotations

import logging
import queue
from pathlib import Path

from app import gui


def test_gui_settings_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    settings = gui.default_settings()
    settings["input_dir"] = str(tmp_path / "input")
    settings["output_dir"] = str(tmp_path / "output")
    settings["schedule_enabled"] = True
    settings["schedule_time"] = "01:30"

    saved = gui.save_settings(settings)
    loaded = gui.load_settings()
    config = gui.service_config(loaded)

    assert saved == tmp_path / "ProgramData" / "StemFlow" / "config.json"
    assert config.input_dir == tmp_path / "input"
    assert config.output_dir == tmp_path / "output"
    assert loaded["schedule_enabled"] is True
    assert loaded["schedule_time"] == "01:30"


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
