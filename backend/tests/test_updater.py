from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.updater import AvailableUpdate, download_update, parse_release


def release_payload(version: str = "1.5.0") -> dict[str, object]:
    installer = f"StemFlow-Update-{version}-x64.exe"
    return {
        "tag_name": f"gui-v{version}",
        "name": f"StemFlow {version}",
        "html_url": "https://example.test/release",
        "body": "release notes",
        "assets": [
            {
                "name": installer,
                "browser_download_url": "https://example.test/update.exe",
                "size": 123,
            },
            {
                "name": f"{installer}.sha256",
                "browser_download_url": "https://example.test/update.sha256",
                "size": 90,
            },
        ],
    }


def test_parse_release_returns_only_newer_update() -> None:
    update = parse_release(release_payload(), current_version="1.4.2")

    assert update is not None
    assert update.version == "1.5.0"
    assert update.installer_name == "StemFlow-Update-1.5.0-x64.exe"
    assert parse_release(release_payload(), current_version="1.5.0") is None


def test_parse_release_requires_small_update_assets() -> None:
    payload = release_payload()
    payload["assets"] = []

    with pytest.raises(RuntimeError, match="缺少自动更新文件"):
        parse_release(payload, current_version="1.4.2")


def test_download_update_checks_sha256(
    tmp_path: Path,
    monkeypatch,
) -> None:
    content = b"stemflow-update"
    expected = hashlib.sha256(content).hexdigest()
    update = AvailableUpdate(
        version="1.5.0",
        release_name="StemFlow 1.5.0",
        release_url="https://example.test/release",
        installer_name="StemFlow-Update-1.5.0-x64.exe",
        installer_url="https://example.test/update.exe",
        checksum_url="https://example.test/update.sha256",
        notes="",
        size_bytes=len(content),
    )

    monkeypatch.setattr(
        "app.updater._request_bytes",
        lambda _url: f"{expected}  {update.installer_name}\n".encode(),
    )

    class Response:
        headers = {"Content-Length": str(len(content))}

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            if getattr(self, "sent", False):
                return b""
            self.sent = True
            return content

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    progress: list[tuple[int, int]] = []

    target = download_update(
        update,
        tmp_path,
        lambda current, total: progress.append((current, total)),
    )

    assert target.read_bytes() == content
    assert progress[-1] == (len(content), len(content))
