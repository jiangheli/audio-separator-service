import asyncio
import json
import logging

from redis.asyncio import Redis

from app.config import get_settings
from app.dependencies import create_processor


def item_message(task_id: str, item_id: str) -> str:
    return json.dumps({"kind": "item", "task_id": task_id, "item_id": item_id})


def parse_message(message: str) -> dict[str, str]:
    try:
        value = json.loads(message)
    except json.JSONDecodeError:
        return {"kind": "task", "task_id": message}
    if not isinstance(value, dict):
        return {"kind": "task", "task_id": message}
    if value.get("kind") == "item" and value.get("task_id") and value.get("item_id"):
        return {
            "kind": "item",
            "task_id": str(value["task_id"]),
            "item_id": str(value["item_id"]),
        }
    return {"kind": "task", "task_id": str(value.get("task_id") or message)}


async def run_slot(slot: int) -> None:
    settings = get_settings()
    processor = create_processor()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    logging.info("Inference slot %s listening on queue %s", slot, settings.queue_name)
    try:
        while True:
            message = await redis.blpop(settings.queue_name, timeout=5)
            if not message:
                continue
            _, raw = message
            payload = parse_message(raw)
            if payload["kind"] == "item":
                await processor.run_item(payload["task_id"], payload["item_id"])
                continue

            item_ids = await processor.prepare(payload["task_id"])
            if item_ids:
                await redis.rpush(
                    settings.queue_name,
                    *(item_message(payload["task_id"], item_id) for item_id in item_ids),
                )
                logging.info(
                    "Task %s expanded into %s media jobs",
                    payload["task_id"],
                    len(item_ids),
                )
    finally:
        await redis.aclose()


async def run_worker() -> None:
    concurrency = get_settings().worker_concurrency
    logging.info("Starting %s independent inference slots", concurrency)
    await asyncio.gather(*(run_slot(slot) for slot in range(1, concurrency + 1)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
