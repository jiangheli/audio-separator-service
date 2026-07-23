from app.worker.main import item_message, parse_message


def test_worker_parses_legacy_task_message() -> None:
    assert parse_message("task-123") == {"kind": "task", "task_id": "task-123"}


def test_worker_round_trips_item_message() -> None:
    assert parse_message(item_message("task-123", "item-456")) == {
        "kind": "item",
        "task_id": "task-123",
        "item_id": "item-456",
    }
