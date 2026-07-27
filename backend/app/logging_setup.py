from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path, *, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stemflow_video")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        log_dir / "stemflow-video.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    upstream_logger = logging.getLogger("audio_separator")
    upstream_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    upstream_logger.handlers.clear()
    for handler in logger.handlers:
        upstream_logger.addHandler(handler)
    upstream_logger.propagate = False
    return logger
