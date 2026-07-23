import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


class ServiceAPIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.details = details


class ServiceAPIClient:
    """Small standard-library client used by the CLI and AI JSON adapter."""

    def __init__(self, base_url: str, timeout: float = 30, public_base_url: str | None = None) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.public_base_url = (public_base_url or base_url).rstrip("/") + "/"
        self.timeout = timeout

    def absolute_url(self, value: str) -> str:
        return urljoin(self.public_base_url, value.lstrip("/"))

    def service_url(self, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.netloc:
            value = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return urljoin(self.base_url, value.lstrip("/"))

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.service_url(path), data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as error:
            raw = error.read()
            try:
                details = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                details = raw.decode("utf-8", errors="replace")
            message = details.get("detail", str(error)) if isinstance(details, dict) else str(error)
            raise ServiceAPIError(message, status=error.code, details=details) from error
        except (URLError, TimeoutError, OSError) as error:
            raise ServiceAPIError(f"Cannot connect to StemFlow API at {self.base_url}: {error}") from error
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ServiceAPIError("StemFlow API returned invalid JSON") from error

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("POST", path, payload)

    def artifacts(self, task_id: str) -> list[dict[str, Any]]:
        records = self.get(f"/api/tasks/{task_id}/files")
        for record in records:
            record["url"] = self.absolute_url(record["url"])
            record["download_url"] = self.absolute_url(record["download_url"])
        return records

    def download(self, url: str, destination: Path) -> int:
        request = Request(self.service_url(url), headers={"Accept": "application/octet-stream"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as output:
                    size = 0
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        size += len(chunk)
                    return size
        except HTTPError as error:
            raise ServiceAPIError(f"Download failed with HTTP {error.code}", status=error.code) from error
        except (URLError, TimeoutError, OSError) as error:
            raise ServiceAPIError(f"Download failed: {error}") from error
