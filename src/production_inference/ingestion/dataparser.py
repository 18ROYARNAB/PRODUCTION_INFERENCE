import asyncio
import gzip
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
import ijson
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from production_inference.config import QDRANT_URL, TEI_URL
from production_inference.logging_config import configure_logging, get_logger
from production_inference.qdrant import (
    GLOBAL_SHARED_TENANT_ID,
    ensure_incidents_collection,
)
from production_inference.sparse_embeddings import get_bm25_model

logger = get_logger(__name__)

# Initialize connections
qdrant = AsyncQdrantClient(QDRANT_URL, timeout=60)
bm25_model = get_bm25_model()
BATCH_SIZE = 32
MAX_RETRY_DELAY_SECONDS = 60


ProgressCallback = Callable[[int], Awaitable[None]]


def is_retryable_service_error(error: Exception) -> bool:
    if isinstance(error, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in (408, 425, 429) or error.response.status_code >= 500
    if isinstance(error, UnexpectedResponse):
        status_code = error.status_code
        return status_code is not None and (
            status_code in (408, 425, 429) or status_code >= 500
        )
    if isinstance(error, ResponseHandlingException):
        source = error.source
        return isinstance(source, (httpx.TimeoutException, httpx.TransportError))
    return False


async def process_batch(batch: list):
    """Embed and upsert only records whose deterministic IDs are not in Qdrant."""

    attempt = 0
    while True:
        try:
            collection_exists = await qdrant.collection_exists("nvd_incidents")
            pending_batch = batch
            if collection_exists:
                existing_points = await qdrant.retrieve(
                    collection_name="nvd_incidents",
                    ids=[item["uuid"] for item in batch],
                    with_payload=False,
                    with_vectors=False,
                )
                existing_ids = {str(point.id) for point in existing_points}
                pending_batch = [
                    item for item in batch if item["uuid"] not in existing_ids
                ]

            if not pending_batch:
                logger.info("Skipped batch of %d CVEs already present in Qdrant", len(batch))
                return

            texts = [item["text"] for item in pending_batch]

            # 1. Get Dense vectors via HTTP from TEI
            async with httpx.AsyncClient(timeout=60.0) as client:
                tei_res = await client.post(f"{TEI_URL}/embed", json={"inputs": texts})
                tei_res.raise_for_status()
                dense_vectors = tei_res.json()

            if not dense_vectors or not dense_vectors[0]:
                raise ValueError("Embedding service returned no dense vectors")
            await ensure_incidents_collection(len(dense_vectors[0]))

            # 2. Get Sparse vectors locally from FastEmbed
            sparse_embeddings = list(bm25_model.embed(texts))

            # 3. Prepare Qdrant Points
            points = []
            for i, item in enumerate(pending_batch):
                points.append(
                    models.PointStruct(
                        id=item["uuid"],
                        vector={
                            "dense": dense_vectors[i],
                            "sparse": models.SparseVector(
                                indices=sparse_embeddings[i].indices.tolist(),
                                values=sparse_embeddings[i].values.tolist()
                            )
                        },
                        payload={
                            "cve_id": item["cve_id"],
                            "text": item["text"],
                            "tenant_id": GLOBAL_SHARED_TENANT_ID,
                            "severity": item["severity"]
                        }
                    )
                )

            # 4. Upsert to Qdrant
            await qdrant.upsert(
                collection_name="nvd_incidents",
                points=points,
                wait=True,
                timeout=60,
            )
            logger.info(
                "Upserted %d CVEs; skipped %d already present",
                len(pending_batch),
                len(batch) - len(pending_batch),
            )
            return
        except Exception as error:
            if not is_retryable_service_error(error):
                raise

            delay = min(2 ** min(attempt, 6), MAX_RETRY_DELAY_SECONDS)
            attempt += 1
            logger.warning(
                "Transient embedding/Qdrant failure for batch of %d; "
                "retrying in %ds (attempt %d): %s",
                len(batch),
                delay,
                attempt,
                type(error).__name__,
            )
            await asyncio.sleep(delay)

async def stream_nvd_gz(
    file: str | Path, progress_callback: ProgressCallback | None = None
) -> int:
    """Streams the .json.gz file and extracts CVEs iteratively."""
    logger.info("Starting NVD ingestion from %s", Path(file).name)
    batch = []
    ingested = 0
    
    # Open the GZIP file in binary read 
    with gzip.open(file, "rb") as gz_file:
        
        # NVD JSON 2.0 stores records inside the "vulnerabilities.item" array. 
        # ijson yields these objects one by one, then destroys them from RAM.
        vulnerabilities = ijson.items(gz_file, "vulnerabilities.item")
        
        for record in vulnerabilities:
            cve_data = record.get("cve", {})
            cve_id = cve_data.get("id")
            
            # Extract English description safely
            descriptions = cve_data.get("descriptions", [])
            eng_desc = next((d["value"] for d in descriptions if d["lang"] == "en"), None)
            
            if not cve_id or not eng_desc:
                continue
                
            # Extract CVSS v3.1 severity if available (fallback to None)
            metrics = cve_data.get("metrics", {})
            cvss_v31 = metrics.get("cvssMetricV31", [{}])[0].get("cvssData", {})
            severity = cvss_v31.get("baseSeverity", "UNKNOWN")

            # Append to our current batch
            batch.append({
                "uuid": str(uuid5(NAMESPACE_URL, cve_id)),
                "cve_id": cve_id,
                "text": eng_desc,
                "severity": severity
            })

            # When batch hits 50, process it and clear RAM
            if len(batch) >= BATCH_SIZE:
                await process_batch(batch)
                ingested += len(batch)
                if progress_callback is not None:
                    await progress_callback(ingested)
                batch.clear() # Frees memory immediately
        
        # Process any remaining items in the final incomplete batch
        if batch:
            await process_batch(batch)
            ingested += len(batch)
            if progress_callback is not None:
                await progress_callback(ingested)

    logger.info("Finished NVD ingestion from %s (%d records)", Path(file).name, ingested)
    return ingested

# Run the ingestion
if __name__ == "__main__":
    # Ensure TEI and Qdrant are running before executing this
    configure_logging()
    asyncio.run(stream_nvd_gz("nvdcve-2.0-2026.json.gz"))