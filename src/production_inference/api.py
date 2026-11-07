import asyncio
import os
import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import langfuse
import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, status
from langfuse import observe
from pydantic import BaseModel, ConfigDict

from production_inference.auth import AuthenticatedPrincipal, get_current_principal
from production_inference.config import REDIS_URL
from production_inference.conversation_memory import (
    append_conversation_turn,
    load_conversation_history,
)
from production_inference.embeddings import generate_embedding
from production_inference.ingestion.job_queue import (
    enqueue_ingestion_job,
    job_status_key,
)
from production_inference.llm import generate_text
from production_inference.logging_config import configure_logging, get_logger
from production_inference.qdrant import (
    ROLE_CLEARANCE,
    search_incidents,
)
from production_inference.retrieval_cache import get_or_load_search_context
from production_inference.sparse_embeddings import generate_sparse_embedding

configure_logging()
logger = get_logger(__name__)

app = FastAPI()
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
INGEST_STORAGE_DIR = Path(
    os.getenv("INGEST_STORAGE_DIR", "/tmp/production-inference/uploads")
)
MAX_INGEST_FILE_SIZE_BYTES = 100 * 1024 * 1024
UPLOAD_COPY_CHUNK_SIZE = 1024 * 1024


class PromptRequest(BaseModel):
    prompt: str
    conversation_id: UUID | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


async def get_search_context(
    query: str, principal: AuthenticatedPrincipal, allowed_severities: list[str]
) -> tuple[list[str], bool]:
    async def retrieve() -> list[str]:
        query_vector, sparse_query_vector = await asyncio.gather(
            generate_embedding(query),
            generate_sparse_embedding(query),
        )
        return await search_incidents(
            query_vector,
            sparse_query_vector,
            principal.tenant_id,
            allowed_severities,
            query_text=query,
        )

    try:
        return await get_or_load_search_context(
            redis_client,
            query,
            principal.tenant_id,
            allowed_severities,
            retrieve,
        )
    except redis.RedisError as error:
        logger.warning("Search context cache unavailable: Redis unavailable")
        raise HTTPException(
            status_code=503,
            detail="Search context cache is unavailable because Redis is unreachable.",
        ) from error


@observe
def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


async def rate_limit(request: Request) -> None:
    cache_key = f"rate_limit:{get_client_ip(request)}"
    current_count = await redis_client.incr(cache_key)
    if current_count == 1:
        await redis_client.expire(cache_key, 60)
    if current_count > 15:
        logger.warning("Rate limit exceeded for a client")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: maximum 15 requests per 60 seconds.",
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}

@observe
@app.post("/chat", dependencies=[Depends(rate_limit)])
async def chat(
    prompt_request: PromptRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_current_principal)],
) -> dict[str, object]:
    allowed_severities = ROLE_CLEARANCE[principal.user_role]
    conversation_id = str(prompt_request.conversation_id or uuid4())
    try:
        history = await load_conversation_history(
            redis_client,
            principal.tenant_id,
            principal.subject,
            allowed_severities,
            conversation_id,
        )
        retrieval_query = "\n".join(
            [
                *(message["content"] for message in history if message["role"] == "user"),
                prompt_request.prompt,
            ]
        )
        context, cache_hit = await get_search_context(
            retrieval_query, principal, allowed_severities
        )
    except redis.RedisError as error:
        logger.warning("Chat context retrieval failed: Redis unavailable")
        raise HTTPException(
            status_code=503,
            detail="Conversation memory is unavailable because Redis is unreachable.",
        ) from error
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Chat context retrieval failed: %s", type(error).__name__)
        raise HTTPException(
            status_code=502, detail=f"Search service error: {error}"
        ) from error

    try:
        output = await generate_text(prompt_request.prompt, context, history)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        logger.warning("Chat generation failed: %s", type(error).__name__)
        raise HTTPException(
            status_code=502, detail=f"LLM service error: {error}"
        ) from error

    try:
        await append_conversation_turn(
            redis_client,
            principal.tenant_id,
            principal.subject,
            allowed_severities,
            conversation_id,
            prompt_request.prompt,
            output,
        )
    except redis.RedisError as error:
        logger.warning("Could not save conversation memory: Redis unavailable")
        raise HTTPException(
            status_code=503,
            detail="Could not save conversation memory because Redis is unreachable.",
        ) from error

    logger.info(
        "Chat completed with %d context results (cache_hit=%s)",
        len(context),
        cache_hit,
    )
    return {
        "status": "success",
        "client_ip": get_client_ip(request),
        "conversation_id": conversation_id,
        "role_applied": principal.user_role,
        "hits_found": len(context),
        "cache_hit": cache_hit,
        "safe_context": context,
        "model_output": output,
    }
@observe
@app.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(
    file: UploadFile,
) -> dict[str, str]:
    """Stage an upload and enqueue it for background ingestion."""
    if not file.filename or not file.filename.lower().endswith(".json.gz"):
        logger.warning("Rejected ingestion upload with an unsupported file type")
        raise HTTPException(
            status_code=415,
            detail="Only .json.gz files are supported.",
        )

    staged_file: Path | None = None
    job_id = str(uuid4())
    queued = False
    try:
        INGEST_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f"{job_id}-",
            suffix=".json.gz",
            dir=INGEST_STORAGE_DIR,
            delete=False,
        ) as staged:
            staged_file = Path(staged.name)
            total_bytes = 0
            while chunk := await file.read(UPLOAD_COPY_CHUNK_SIZE):
                total_bytes += len(chunk)
                if total_bytes > MAX_INGEST_FILE_SIZE_BYTES:
                    logger.warning("Rejected ingestion upload exceeding size limit")
                    raise HTTPException(
                        status_code=413,
                        detail="Upload exceeds the 100 MB limit.",
                    )
                staged.write(chunk)

        await enqueue_ingestion_job(redis_client, job_id, staged_file)
        queued = True
    except HTTPException:
        raise
    except (OSError, redis.RedisError) as error:
        logger.warning(
            "Could not store or queue ingestion job %s: %s",
            job_id,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Could not store or queue the ingestion job. Check disk and Redis availability.",
        ) from error
    finally:
        try:
            await file.close()
        finally:
            if staged_file is not None and not queued:
                staged_file.unlink(missing_ok=True)

    logger.info("Queued ingestion job %s (%d bytes)", job_id, total_bytes)
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/ingest/{job_id}",
    }

@observe
@app.get("/ingest/{job_id}")
async def get_ingestion_status(job_id: str) -> dict[str, object]:
    try:
        job = await redis_client.hgetall(job_status_key(job_id))
    except redis.RedisError as error:
        logger.warning("Could not read status for ingestion job %s", job_id)
        raise HTTPException(
            status_code=503,
            detail="Ingestion job status is unavailable because Redis is unreachable.",
        ) from error
    if not job:
        raise HTTPException(status_code=404, detail="Ingestion job not found.")

    response: dict[str, object] = {
        "job_id": job_id,
        "status": job["status"],
        "ingested": int(job.get("ingested", 0)),
    }
    if job["status"] == "failed":
        response["error"] = job.get("error", "Ingestion failed.")
    return response

@observe
@app.post("/search", dependencies=[Depends(rate_limit)])
async def search(
    request: SearchRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_current_principal)],
) -> dict[str, object]:
    allowed_severities = ROLE_CLEARANCE[principal.user_role]

    try:
        context, cache_hit = await get_search_context(
            request.query, principal, allowed_severities
        )
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Search failed: %s", type(error).__name__)
        raise HTTPException(status_code=502, detail=f"Search service error: {error}") from error

    logger.info(
        "Search completed with %d results (cache_hit=%s)",
        len(context),
        cache_hit,
    )
    return {
        "status": "success",
        "role_applied": principal.user_role,
        "authorized_severities": allowed_severities,
        "hits_found": len(context),
        "cache_hit": cache_hit,
        "safe_context": context,
    }