
"""Benchmark dense, sparse, and hybrid retrieval against alternating NVD records."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import os
import re
import statistics
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
import ijson
from fastembed import SparseTextEmbedding
from qdrant_client import AsyncQdrantClient, models

from production_inference.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

COLLECTION_NAME = "nvd_incidents"
GLOBAL_SHARED_TENANT_ID = "global_shared"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
HYBRID_PREFETCH_LIMIT = 20
DEFAULT_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
CVSS_DESCRIPTION = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)


@dataclass(frozen=True)
class BenchmarkRecord:
    cve_id: str
    text: str
    severity: str

    @property
    def point_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self.cve_id))

    @property
    def query(self) -> str:
        text_without_ids = CVSS_DESCRIPTION.sub("", self.text).strip()
        tokens = text_without_ids.split()
        partial_tokens = [token for index, token in enumerate(tokens) if index % 3 != 0]
        return " ".join(partial_tokens or tokens)


def iter_alternating_records(
    input_path: Path,
    *,
    offset: int,
    limit: int,
    allowed_severities: set[str],
) -> Iterator[BenchmarkRecord]:
    """Yield every other ingest-compatible NVD record after severity filtering."""
    opener = gzip.open if input_path.suffix.lower() == ".gz" else Path.open
    eligible_index = 0
    selected = 0
    with opener(input_path, "rb") as source:
        for item in ijson.items(source, "vulnerabilities.item"):
            cve = item.get("cve") or {}
            cve_id = cve.get("id")
            if not isinstance(cve_id, str) or not cve_id:
                continue

            descriptions = cve.get("descriptions") or []
            description = next(
                (
                    entry.get("value")
                    for entry in descriptions
                    if isinstance(entry, dict)
                    and entry.get("lang") == "en"
                    and isinstance(entry.get("value"), str)
                    and entry["value"]
                ),
                None,
            )
            if description is None:
                continue

            metrics = cve.get("metrics") or {}
            v31_metrics = metrics.get("cvssMetricV31") or []
            severity = "UNKNOWN"
            if v31_metrics and isinstance(v31_metrics[0], dict):
                cvss_data = v31_metrics[0].get("cvssData") or {}
                severity = cvss_data.get("baseSeverity", "UNKNOWN")
            if severity not in allowed_severities:
                continue

            current_index = eligible_index
            eligible_index += 1
            if current_index % 2 != offset:
                continue

            yield BenchmarkRecord(cve_id, description, severity)
            selected += 1
            if selected >= limit:
                return


def build_filter(tenant_id: str, severities: Sequence[str]) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(
                key="tenant_id",
                match=models.MatchAny(
                    any=[tenant_id, GLOBAL_SHARED_TENANT_ID]
                ),
            ),
            models.FieldCondition(
                key="severity",
                match=models.MatchAny(any=list(severities)),
            ),
        ]
    )


def sparse_query_vectors(
    model: SparseTextEmbedding, queries: Sequence[str]
) -> list[models.SparseVector]:
    vectors: list[models.SparseVector] = []
    for query in queries:
        embedding = next(iter(model.query_embed(query)), None)
        if embedding is None:
            raise ValueError(f"BM25 returned no query vector for: {query[:80]!r}")
        indices = embedding.indices.tolist()
        values = embedding.values.tolist()
        if len(indices) != len(values):
            raise ValueError("BM25 returned a malformed sparse query vector")
        vectors.append(models.SparseVector(indices=indices, values=values))
    return vectors


async def embed_dense_queries(
    client: httpx.AsyncClient, tei_url: str, queries: Sequence[str]
) -> list[list[float]]:
    response = await client.post(f"{tei_url.rstrip('/')}/embed", json={"inputs": queries})
    response.raise_for_status()
    vectors = response.json()
    if (
        not isinstance(vectors, list)
        or len(vectors) != len(queries)
        or any(not isinstance(vector, list) or not vector for vector in vectors)
    ):
        raise ValueError("TEI returned an invalid number of dense query vectors")
    return vectors


async def query_variant(
    qdrant: AsyncQdrantClient,
    *,
    variant: str,
    dense_vector: list[float],
    sparse_vector: models.SparseVector,
    query_filter: models.Filter,
    limit: int,
) -> tuple[list[str], float]:
    started = time.perf_counter()
    if variant == "dense":
        result = await qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=dense_vector,
            using=DENSE_VECTOR_NAME,
            query_filter=query_filter,
            limit=limit,
            with_payload=["cve_id"],
        )
    elif variant == "sparse":
        result = await qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=sparse_vector,
            using=SPARSE_VECTOR_NAME,
            query_filter=query_filter,
            limit=limit,
            with_payload=["cve_id"],
        )
    else:
        prefetch_limit = max(HYBRID_PREFETCH_LIMIT, limit)
        result = await qdrant.query_points(
            collection_name=COLLECTION_NAME,
            prefetch=[
                models.Prefetch(
                    query=dense_vector,
                    using=DENSE_VECTOR_NAME,
                    filter=query_filter,
                    limit=prefetch_limit,
                ),
                models.Prefetch(
                    query=sparse_vector,
                    using=SPARSE_VECTOR_NAME,
                    filter=query_filter,
                    limit=prefetch_limit,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=query_filter,
            limit=limit,
            with_payload=["cve_id"],
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    ranked_ids = [
        str(point.payload["cve_id"])
        for point in result.points
        if point.payload and isinstance(point.payload.get("cve_id"), str)
    ]
    return ranked_ids, elapsed_ms


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 3)


def summarize_ranks(
    ranks: Sequence[int | None], latencies_ms: Sequence[float], limit: int
) -> dict[str, float | int | None]:
    if not ranks:
        return {
            f"recall_at_{limit}": None,
            "mrr": None,
            "top1_accuracy": None,
            "mean_latency_ms": None,
            "p50_latency_ms": None,
            "p95_latency_ms": None,
        }
    return {
        f"recall_at_{limit}": round(
            sum(rank is not None for rank in ranks) / len(ranks), 4
        ),
        "mrr": round(
            sum(1 / rank for rank in ranks if rank is not None) / len(ranks), 4
        ),
        "top1_accuracy": round(
            sum(rank == 1 for rank in ranks) / len(ranks), 4
        ),
        "mean_latency_ms": round(statistics.mean(latencies_ms), 3),
        "p50_latency_ms": percentile(latencies_ms, 0.50),
        "p95_latency_ms": percentile(latencies_ms, 0.95),
    }


async def benchmark(
    records: Sequence[BenchmarkRecord],
    *,
    qdrant_url: str,
    tei_url: str,
    tenant_id: str,
    severities: Sequence[str],
    result_limit: int,
    query_batch_size: int,
) -> dict[str, object]:
    if not records:
        raise ValueError("No eligible records were selected from the input file")
    if any(not record.query for record in records):
        raise ValueError("A selected description contains no text after removing CVE IDs")

    qdrant = AsyncQdrantClient(url=qdrant_url, timeout=60)
    try:
        if not await qdrant.collection_exists(COLLECTION_NAME):
            raise RuntimeError(f"Qdrant collection {COLLECTION_NAME!r} does not exist")

        available_ids: set[str] = set()
        for start in range(0, len(records), query_batch_size):
            batch = records[start : start + query_batch_size]
            points = await qdrant.retrieve(
                collection_name=COLLECTION_NAME,
                ids=[record.point_id for record in batch],
                with_payload=False,
                with_vectors=False,
            )
            available_ids.update(str(point.id) for point in points)

        present_records = [record for record in records if record.point_id in available_ids]
        if not present_records:
            raise RuntimeError(
                "None of the selected deterministic CVE point IDs exist in Qdrant. "
                "Check that this exact NVD file was ingested into the configured cluster."
            )

        query_filter = build_filter(tenant_id, severities)
        model = SparseTextEmbedding(model_name="Qdrant/bm25")
        variants = ("dense", "sparse", "hybrid")
        ranks: dict[str, list[int | None]] = {variant: [] for variant in variants}
        latencies: dict[str, list[float]] = {variant: [] for variant in variants}
        per_record: list[dict[str, object]] = []

        async with httpx.AsyncClient(timeout=60.0) as http_client:
            for start in range(0, len(present_records), query_batch_size):
                batch = present_records[start : start + query_batch_size]
                queries = [record.query for record in batch]
                dense_vectors, sparse_vectors = await asyncio.gather(
                    embed_dense_queries(http_client, tei_url, queries),
                    asyncio.to_thread(sparse_query_vectors, model, queries),
                )
                for record, dense_vector, sparse_vector in zip(
                    batch, dense_vectors, sparse_vectors, strict=True
                ):
                    result_row: dict[str, object] = {
                        "cve_id": record.cve_id,
                        "severity": record.severity,
                        "ranks": {},
                    }
                    record_ranks: dict[str, int | None] = {}
                    for variant in variants:
                        ranked_ids, elapsed_ms = await query_variant(
                            qdrant,
                            variant=variant,
                            dense_vector=dense_vector,
                            sparse_vector=sparse_vector,
                            query_filter=query_filter,
                            limit=result_limit,
                        )
                        try:
                            rank = ranked_ids.index(record.cve_id) + 1
                        except ValueError:
                            rank = None
                        ranks[variant].append(rank)
                        latencies[variant].append(elapsed_ms)
                        record_ranks[variant] = rank
                    result_row["ranks"] = record_ranks
                    per_record.append(result_row)

        missing_count = len(records) - len(present_records)
        return {
            "collection": COLLECTION_NAME,
            "sampling": {
                "method": "every_other_ingest-compatible_record",
                "offset": None,
                "selected_from_file": len(records),
                "targets_present_in_qdrant": len(present_records),
                "targets_missing_from_qdrant": missing_count,
                "tenant_id": tenant_id,
                "allowed_severities": list(severities),
                "query_text": (
                    "English NVD description with CVE identifiers removed and "
                    "every third whitespace-delimited token omitted"
                ),
            },
            "metrics": {
                variant: summarize_ranks(ranks[variant], latencies[variant], result_limit)
                for variant in variants
            },
            "results": per_record,
        }
    finally:
        await qdrant.close()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2, ensure_ascii=False)
        output.write("\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="NVD JSON or .json.gz file used for ingestion",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum alternating evaluation records (default: 100)",
    )
    parser.add_argument(
        "--offset",
        type=int,
        choices=(0, 1),
        default=0,
        help="Select even (0) or odd (1) positions in the eligible record stream",
    )
    parser.add_argument("--k", type=int, default=5, help="Retrieval cutoff (default: 5)")
    parser.add_argument(
        "--tenant-id",
        default="benchmark-tenant",
        help="Tenant scope; shared NVD records are included (default: benchmark-tenant)",
    )
    parser.add_argument(
        "--severities",
        nargs="+",
        choices=("LOW", "MEDIUM", "HIGH", "CRITICAL"),
        default=list(DEFAULT_SEVERITIES),
        help="Searchable severity levels (default: all production-supported levels)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Number of query descriptions embedded per batch (default: 32)",
    )
    parser.add_argument(
        "--extract-output",
        type=Path,
        help="Optional JSONL path for the selected alternating evaluation records",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        help="Optional path for the full JSON benchmark report",
    )
    args = parser.parse_args(argv)
    if args.limit < 1 or args.k < 1 or args.batch_size < 1:
        parser.error("--limit, --k, and --batch-size must be positive")
    if not args.input.is_file():
        parser.error(f"Input file does not exist: {args.input}")
    return args


async def async_main(args: argparse.Namespace) -> dict[str, object]:
    severities = set(args.severities)
    records = list(
        iter_alternating_records(
            args.input,
            offset=args.offset,
            limit=args.limit,
            allowed_severities=severities,
        )
    )
    if args.extract_output is not None:
        args.extract_output.parent.mkdir(parents=True, exist_ok=True)
        with args.extract_output.open("w", encoding="utf-8") as output:
            for record in records:
                output.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    result = await benchmark(
        records,
        qdrant_url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
        tei_url=os.getenv("TEI_URL", "http://127.0.0.1:7997"),
        tenant_id=args.tenant_id,
        severities=args.severities,
        result_limit=args.k,
        query_batch_size=args.batch_size,
    )
    sampling = result["sampling"]
    if isinstance(sampling, dict):
        sampling["offset"] = args.offset
    return result


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    logger.info("Starting retrieval benchmark for %s", args.input.name)
    try:
        result = asyncio.run(async_main(args))
    except (httpx.HTTPError, OSError, ValueError, RuntimeError):
        logger.exception("Retrieval benchmark failed")
        return 1

    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.report_output is not None:
        write_json(args.report_output, result)
        logger.info("Full benchmark report written to %s", args.report_output)
        print(json.dumps(result["metrics"], indent=2))
    else:
        print(rendered)
    logger.info("Retrieval benchmark completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
