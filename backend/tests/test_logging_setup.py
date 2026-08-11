from __future__ import annotations

from logging.handlers import RotatingFileHandler

from app.logging_setup import configure_logging


def test_logging_is_size_rotated_and_reconfiguration_does_not_duplicate(
    tmp_path,
) -> None:
    configure_logging(tmp_path)
    logger = configure_logging(tmp_path)

    rotating = [
        handler
        for handler in logger.handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(logger.handlers) == 2
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 25 * 1024 * 1024
    assert rotating[0].backupCount == 8
    assert "session started" in (tmp_path / "stemflow-video.log").read_text(
        encoding="utf-8"
    )
