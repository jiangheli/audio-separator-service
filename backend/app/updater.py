from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY = "jiangheli/audio-separator-service"
LATEST_RELEASE_URL = (
    f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
)
USER_AGENT = "StemFlow-Windows-Updater"
VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True, slots=True)
class AvailableUpdate:
    version: str
    release_name: str
    release_url: str
    installer_name: str
    installer_url: str
    checksum_url: str
    notes: str
    size_bytes: int


def version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.search(value)
    if match is None:
        raise ValueError(f"无法识别版本号：{value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def parse_release(
    payload: dict[str, Any],
    *,
    current_version: str,
) -> AvailableUpdate | None:
    tag = str(payload.get("tag_name", ""))
    release_version = ".".join(str(part) for part in version_tuple(tag))
    if version_tuple(release_version) <= version_tuple(current_version):
        return None

    installer_name = f"StemFlow-Update-{release_version}-x64.exe"
    checksum_name = f"{installer_name}.sha256"
    assets = {
        str(asset.get("name", "")): asset
        for asset in payload.get("assets", [])
        if isinstance(asset, dict)
    }
    installer = assets.get(installer_name)
    checksum = assets.get(checksum_name)
    if installer is None or checksum is None:
        raise RuntimeError(
            f"Release {tag} 缺少自动更新文件："
            f"{installer_name} 或 {checksum_name}"
        )
    installer_url = str(installer.get("browser_download_url", ""))
    checksum_url = str(checksum.get("browser_download_url", ""))
    if not installer_url or not checksum_url:
        raise RuntimeError(f"Release {tag} 的更新下载地址无效")
    return AvailableUpdate(
        version=release_version,
        release_name=str(payload.get("name", tag)) or tag,
        release_url=str(payload.get("html_url", "")),
        installer_name=installer_name,
        installer_url=installer_url,
        checksum_url=checksum_url,
        notes=str(payload.get("body", "")),
        size_bytes=int(installer.get("size", 0)),
    )


def _request_bytes(url: str, *, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def check_for_update(current_version: str) -> AvailableUpdate | None:
    payload = json.loads(_request_bytes(LATEST_RELEASE_URL).decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub Release 返回格式无效")
    return parse_release(payload, current_version=current_version)


def _expected_checksum(update: AvailableUpdate) -> str:
    content = _request_bytes(update.checksum_url).decode(
        "utf-8",
        errors="replace",
    )
    expected = content.strip().split()[0].lower() if content.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("更新包 SHA-256 文件无效")
    return expected


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_update(
    update: AvailableUpdate,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / update.installer_name
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.unlink(missing_ok=True)
    expected = _expected_checksum(update)
    if target.is_file():
        existing = _sha256_file(target)
        if existing == expected:
            if progress:
                progress(target.stat().st_size, target.stat().st_size)
            return target
        target.unlink()
    request = urllib.request.Request(
        update.installer_url,
        headers={"User-Agent": USER_AGENT},
    )
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or update.size_bytes)
            with temporary.open("wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    downloaded += len(block)
                    if progress:
                        progress(downloaded, total)
        actual = digest.hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"更新包 SHA-256 校验失败：期望 {expected}，实际 {actual}"
            )
        os.replace(temporary, target)
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
