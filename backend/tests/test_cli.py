import json
from pathlib import Path

import pytest

from app.cli.client import ServiceAPIClient
from app.cli.main import (
    CLIError,
    EXIT_TASK,
    capabilities_data,
    dispatch_agent,
    envelope,
    normalize_legacy_argv,
    wait_for_task,
)


def test_capabilities_and_envelope_are_machine_readable() -> None:
    result = envelope("capabilities", data=capabilities_data())
    encoded = json.dumps(result)

    assert result["ok"] is True
    assert result["data"]["outputs"] == ["vocals.wav", "instrumental.wav"]
    assert "task.download" in result["data"]["agent_actions"]
    assert json.loads(encoded)["meta"]["cli"] == "stemflow"


def test_legacy_input_flags_are_mapped_to_separate() -> None:
    normalized, legacy = normalize_legacy_argv(["--input", "a.wav", "--output", "results", "--json"])

    assert legacy is True
    assert normalized[0] == "separate"


def test_api_client_builds_absolute_urls() -> None:
    client = ServiceAPIClient("http://backend:8000", public_base_url="http://localhost:8001")

    assert client.absolute_url("/api/health") == "http://localhost:8001/api/health"
    assert client.absolute_url("http://example.test/result") == "http://example.test/result"
    assert client.service_url("http://localhost:8001/api/file?id=1") == "http://backend:8000/api/file?id=1"


class FakeTaskClient:
    def __init__(self, tasks: list[dict]) -> None:
        self.tasks = iter(tasks)

    def get(self, _path: str) -> dict:
        return next(self.tasks)


def test_wait_for_task_returns_completed_task() -> None:
    client = FakeTaskClient(
        [
            {"id": "abc", "status": "running", "progress": 50, "current_file": "a.wav"},
            {"id": "abc", "status": "completed", "progress": 100, "current_file": None},
        ]
    )

    result = wait_for_task(client, "abc", 0.001, 1, report_progress=False)

    assert result["status"] == "completed"


def test_wait_for_task_uses_nonzero_exit_for_failed_task() -> None:
    client = FakeTaskClient(
        [{"id": "abc", "status": "failed", "progress": 100, "current_file": None, "error": "bad audio"}]
    )

    with pytest.raises(CLIError) as raised:
        wait_for_task(client, "abc", 0.001, 1, report_progress=False)

    assert raised.value.code == EXIT_TASK
    assert raised.value.details["task"]["error"] == "bad audio"


@pytest.mark.asyncio
async def test_agent_rejects_missing_action() -> None:
    with pytest.raises(CLIError, match="include action"):
        await dispatch_agent({})


@pytest.mark.asyncio
async def test_agent_rejects_invalid_download_kinds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        pass

    monkeypatch.setattr("app.cli.main.ServiceAPIClient", lambda *_args, **_kwargs: FakeClient())
    with pytest.raises(CLIError, match="kinds must be an array"):
        await dispatch_agent(
            {
                "action": "task.download",
                "task_id": "abc",
                "output": str(tmp_path),
                "kinds": "vocals",
            }
        )
