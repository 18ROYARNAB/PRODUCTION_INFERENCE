import json
from pathlib import Path

INGEST_QUEUE_KEY = "production-inference:ingest:queued"
INGEST_PROCESSING_KEY = "production-inference:ingest:processing"


def job_status_key(job_id: str) -> str:
    return f"production-inference:ingest:job:{job_id}"


async def enqueue_ingestion_job(redis_client, job_id: str, file_path: Path) -> None:
    pipeline = redis_client.pipeline(transaction=True)
    pipeline.hset(
        job_status_key(job_id),
        mapping={
            "status": "queued",
            "ingested": 0,
            "error": "",
            "file": str(file_path),
        },
    )
    pipeline.lpush(
        INGEST_QUEUE_KEY,
        json.dumps({"job_id": job_id, "file": str(file_path)}),
    )
    await pipeline.execute()
