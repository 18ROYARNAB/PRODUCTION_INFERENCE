import re
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from langfuse import observe
from qdrant_client import AsyncQdrantClient, models

from production_inference.config import QDRANT_URL

ROLE_CLEARANCE = {
    "L1_Helpdesk": ["LOW", "MEDIUM"],
    "L3_SRE": ["LOW", "MEDIUM", "HIGH"],
    "SecOps_Lead": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
}

INCIDENTS_COLLECTION = "nvd_incidents"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
GLOBAL_SHARED_TENANT_ID = "global_shared"
HYBRID_PREFETCH_LIMIT = 20
SEARCH_RESULT_LIMIT = 5
_CVE_ID_PATTERN = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)
client = AsyncQdrantClient(url=QDRANT_URL)


async def ensure_incidents_collection(vector_size: int) -> None:
    if await client.collection_exists(INCIDENTS_COLLECTION):
        return

    await client.create_collection(
        collection_name=INCIDENTS_COLLECTION,
        vectors_config={
            DENSE_VECTOR_NAME: models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE,
            )
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=False)
            )
        },
    )

@observe
async def search_incidents(
    query_vector: list[float],
    sparse_query_vector: models.SparseVector,
    tenant_id: str,
    allowed_severities: list[str],
    *,
    query_text: str | None = None,
) -> list[str]:
    must_conditions: list[models.Condition] = [
        models.FieldCondition(
            key="tenant_id",
            match=models.MatchAny(any=[tenant_id, GLOBAL_SHARED_TENANT_ID]),
        ),
        models.FieldCondition(
            key="severity",
            match=models.MatchAny(any=allowed_severities),
        ),
    ]
    if query_text is not None:
        cve_match = _CVE_ID_PATTERN.search(query_text)
        if cve_match is not None:
            must_conditions.append(
                models.FieldCondition(
                    key="cve_id",
                    match=models.MatchValue(value=cve_match.group().upper()),
                )
            )

    query_filter = models.Filter(
        must=must_conditions
    )
    prefetch = [
        models.Prefetch(
            query=query_vector,
            using=DENSE_VECTOR_NAME,
            filter=query_filter,
            limit=HYBRID_PREFETCH_LIMIT,
        )
    ]
    if sparse_query_vector.indices:
        prefetch.append(
            models.Prefetch(
                query=sparse_query_vector,
                using=SPARSE_VECTOR_NAME,
                filter=query_filter,
                limit=HYBRID_PREFETCH_LIMIT,
            )
        )

    result = await client.query_points(
        collection_name=INCIDENTS_COLLECTION,
        prefetch=prefetch,
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=query_filter,
        limit=SEARCH_RESULT_LIMIT,
        with_payload=["text"],
    )
    return [
        point.payload["text"]
        for point in result.points
        if point.payload and isinstance(point.payload.get("text"), str)
    ]

@observe
async def upsert_incidents(parsed_data):
    points = []
    for incident in parsed_data:
        vector = incident.get("vector", incident.get("embedding"))
        if not isinstance(vector, list) or not vector:
            raise ValueError("Each incident must include a non-empty vector or embedding")

        raw_id = incident.get("id", incident.get("incident_id"))
        if isinstance(raw_id, int):
            point_id = raw_id
        elif raw_id is None:
            point_id = str(uuid4())
        else:
            try:
                point_id = str(UUID(str(raw_id)))
            except ValueError:
                point_id = str(uuid5(NAMESPACE_URL, str(raw_id)))

        payload = incident.get("payload")
        if payload is None:
            payload = {
                key: value
                for key, value in incident.items()
                if key not in {"id", "incident_id", "vector", "embedding"}
            }

        points.append(
            models.PointStruct(id=point_id, vector=vector, payload=payload)
        )

    if points:
        await client.upsert(
            collection_name=INCIDENTS_COLLECTION,
            points=points,
            wait=True,
        )
    