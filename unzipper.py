import asyncio
import gzip
import json
import os
import uuid

import httpx
from qdrant_client import AsyncQdrantClient, models

from production_inference.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

TEI_URL = os.getenv("TEI_URL", "http://localhost:7997")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = "nvd_incidents"


async def flush_batch(
    http_client: httpx.AsyncClient,
    qdrant: AsyncQdrantClient,
    texts: list[str],
    metadata: list[dict],
    point_ids: list[str],
) -> int:
    if not texts:
        return 0

    existing = await qdrant.retrieve(
        collection_name=COLLECTION_NAME,
        ids=point_ids,
        with_payload=False,
        with_vectors=False,
    )
    existing_ids = {str(point.id) for point in existing}
    pending = [
        (point_id, text, payload)
        for point_id, text, payload in zip(point_ids, texts, metadata)
        if point_id not in existing_ids
    ]
    if not pending:
        return 0

    pending_ids, pending_texts, pending_metadata = map(list, zip(*pending))
    for attempt in range(4):
        try:
            response = await http_client.post(
                f"{TEI_URL}/embed",
                json={"inputs": pending_texts},
                timeout=180.0,
            )
            response.raise_for_status()
            vectors = response.json()
            break
        except (httpx.TimeoutException, httpx.TransportError) as error:
            if attempt == 3:
                raise
            delay = 2 ** (attempt + 1)
            logger.warning(
                "TEI request failed (%s); retrying in %ds",
                type(error).__name__,
                delay,
            )
            await asyncio.sleep(delay)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 429 and error.response.status_code < 500:
                raise
            if attempt == 3:
                raise
            delay = 2 ** (attempt + 1)
            logger.warning(
                "TEI returned status %d; retrying in %ds",
                error.response.status_code,
                delay,
            )
            await asyncio.sleep(delay)

    if len(vectors) != len(pending_metadata):
        raise ValueError(
            f"TEI returned {len(vectors)} vectors for {len(pending_metadata)} records"
        )

    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            models.PointStruct(id=point_id, vector=vector, payload=payload)
            for point_id, vector, payload in zip(pending_ids, vectors, pending_metadata)
        ],
        wait=True,
    )
    return len(vectors)

async def stream_and_embed_nvd_v2(file_path: str, batch_size: int = 32):
    logger.info("Loading %s in NVD JSON 2.0 format", os.path.basename(file_path))
    with gzip.open(file_path, "rt", encoding="utf-8") as f:
        nvd_data = json.load(f)

    # The 2.0 schema stores the array under 'vulnerabilities' instead of 'CVE_Items'
    vulnerabilities = nvd_data.get("vulnerabilities", [])
    logger.info("Found %d incidents; starting ingestion", len(vulnerabilities))

    batch_texts = []
    batch_metadata = []
    batch_ids = []
    total_upserted = 0

    qdrant = AsyncQdrantClient(url=QDRANT_URL)
    try:
        if not await qdrant.collection_exists(COLLECTION_NAME):
            await qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=models.VectorParams(size=768, distance=models.Distance.COSINE),
                replication_factor=2,
                write_consistency_factor=2,
            )
            await qdrant.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="tenant_id",
                field_schema=models.KeywordIndexParams(
                    type=models.KeywordIndexType.KEYWORD,
                    is_tenant=True,
                ),
            )

        async with httpx.AsyncClient(timeout=60.0) as http_client:
            for item in vulnerabilities:
                cve = item.get("cve", {})
                descriptions = cve.get("descriptions", [])
                text = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")
                if not text:
                    continue

                metrics = cve.get("metrics", {})
                severity = "LOW"
                for metric_name in ("cvssMetricV31", "cvssMetricV30"):
                    metric_values = metrics.get(metric_name, [])
                    if metric_values:
                        severity = metric_values[0].get("cvssData", {}).get("baseSeverity", "LOW")
                        break

                vendor = "general_infrastructure"
                for configuration in cve.get("configurations", []):
                    for node in configuration.get("nodes", []):
                        cpe_matches = node.get("cpeMatch", [])
                        if cpe_matches:
                            parts = cpe_matches[0].get("criteria", "").split(":")
                            if len(parts) > 3:
                                vendor = parts[3]
                            break
                    if vendor != "general_infrastructure":
                        break

                cve_id = cve.get("id")
                if not cve_id:
                    continue
                batch_texts.append(text)
                batch_metadata.append({
                    "cve_id": cve_id,
                    "tenant_id": vendor,
                    "clearance_level": severity,
                    "text": text,
                })
                batch_ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, cve_id)))

                if len(batch_texts) >= batch_size:
                    total_upserted += await flush_batch(
                        http_client, qdrant, batch_texts, batch_metadata, batch_ids
                    )
                    logger.info("Upserted %d records", total_upserted)
                    batch_texts.clear()
                    batch_metadata.clear()
                    batch_ids.clear()

            total_upserted += await flush_batch(
                http_client, qdrant, batch_texts, batch_metadata, batch_ids
            )
        collection_count = await qdrant.count(COLLECTION_NAME, exact=True)
    finally:
        await qdrant.close()

    logger.info(
        "Ingestion complete: added %d records; collection contains %d",
        total_upserted,
        collection_count.count,
    )


if __name__ == "__main__":
    configure_logging()
    asyncio.run(stream_and_embed_nvd_v2("nvdcve-2.0-2026.json.gz", batch_size=32))