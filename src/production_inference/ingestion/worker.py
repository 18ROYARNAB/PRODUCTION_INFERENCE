import asyncio
import json
from pathlib import Path

import redis.asyncio as redis

from production_inference.config import REDIS_URL
from production_inference.ingestion.dataparser import stream_nvd_gz
from production_inference.ingestion.job_queue import (
    INGEST_PROCESSING_KEY,
    INGEST_QUEUE_KEY,
    job_status_key,
)
from production_inference.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


async def process_job(redis_client, job: dict[str, str]) -> None:
    job_id = job["job_id"]
    file_path = Path(job["file"])
    status_key = job_status_key(job_id)
    completed = False

    try:
        await redis_client.hset(
            status_key,
            mapping={"status": "processing", "error": ""},
        )

        async def update_progress(ingested: int) -> None:
            await redis_client.hset(status_key, mapping={"ingested": ingested})

        ingested = await stream_nvd_gz(file_path, progress_callback=update_progress)
        await redis_client.hset(
            status_key,
            mapping={"status": "completed", "ingested": ingested},
        )
        completed = True
        logger.info("Ingestion job %s completed (%d records)", job_id, ingested)
    except Exception as error:
        logger.exception("Ingestion job %s failed", job_id)
        await redis_client.hset(
            status_key,
            mapping={
                "status": "failed",
                "error": str(error)[:1000],
            },
        )
    finally:
        await redis_client.lrem(INGEST_PROCESSING_KEY, 1, json.dumps(job))
        if completed:
            file_path.unlink(missing_ok=True)


async def run_worker() -> None:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        # Requeue work left in progress if stopped unexpectedly.
        while job := await redis_client.rpoplpush(
            INGEST_PROCESSING_KEY, INGEST_QUEUE_KEY
        ):
            logger.warning("Requeued interrupted ingestion job")

        logger.info("Ingestion worker started")
        while True:
            raw_job = await redis_client.rpoplpush(
                INGEST_QUEUE_KEY,
                INGEST_PROCESSING_KEY,
            )
            if raw_job is None:
                await asyncio.sleep(1)
                continue
            await process_job(redis_client, json.loads(raw_job))
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(run_worker())
