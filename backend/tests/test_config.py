import json
from pathlib import Path

import pytest

from app.config import ServiceConfig


def test_load_config_resolves_relative_runtime_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "stemflow.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps({
            "input_dir": "../input",
            "output_dir": "../output",
            "data_dir": "../runtime",
            "schedule_time": "01:30",
        }),
        encoding="utf-8",
    )

    config = ServiceConfig.load(config_path)

    assert config.input_dir == tmp_path / "input"
    assert config.output_dir == tmp_path / "output"
    assert config.database_path == tmp_path / "runtime" / "processing.db"
    assert config.report_path == tmp_path / "runtime" / "processing_status.xlsx"
    assert config.schedule_time == "01:30"


def test_rejects_same_input_and_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be different"):
        ServiceConfig.from_mapping(
            {"input_dir": str(tmp_path), "output_dir": str(tmp_path)},
            base=tmp_path,
        )
