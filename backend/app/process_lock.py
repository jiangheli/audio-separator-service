from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class AlreadyRunningError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            handle.close()
            raise AlreadyRunningError(
                "Another StemFlow video batch is already running"
            ) from error
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
