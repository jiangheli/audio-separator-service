from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path, *, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stemflow_video")
    upstream_logger = logging.getLogger("audio_separator")
    old_handlers = set(logger.handlers + upstream_logger.handlers)
    logger.handlers.clear()
    upstream_logger.handlers.clear()
    for handler in old_handlers:
        handler.close()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "stemflow-video.log",
        maxBytes=25 * 1024 * 1024,
        backupCount=8,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    upstream_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in logger.handlers:
        upstream_logger.addHandler(handler)
    upstream_logger.propagate = False
    logger.info(
        "LOGGING | session started; file=%s; rotation=25MB x 8 backups; "
        "level=%s",
        log_dir / "stemflow-video.log",
        logging.getLevelName(logger.level),
    )
    return logger
