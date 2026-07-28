from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.gpu_runtime import (
    OFFLINE_MANIFEST_NAME,
    verify_offline_wheelhouse,
)


def write_manifest(wheelhouse: Path, wheel: Path) -> None:
    manifest = {
        "format_version": 1,
        "created_at": "2026-07-28T00:00:00Z",
        "files": [
            {
                "name": wheel.name,
                "size": wheel.stat().st_size,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
        ],
    }
    (wheelhouse / OFFLINE_MANIFEST_NAME).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def test_verify_offline_wheelhouse_accepts_matching_manifest(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "torch-test.whl"
    wheel.write_bytes(b"offline-cuda-wheel")
    write_manifest(tmp_path, wheel)

    manifest = verify_offline_wheelhouse(tmp_path)

    assert manifest["files"][0]["name"] == wheel.name


def test_verify_offline_wheelhouse_rejects_modified_wheel(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "torch-test.whl"
    wheel.write_bytes(b"original")
    write_manifest(tmp_path, wheel)
    wheel.write_bytes(b"modified")

    with pytest.raises(RuntimeError, match="大小错误|校验失败"):
        verify_offline_wheelhouse(tmp_path)
