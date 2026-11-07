import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault("LLM_URL", "http://localhost:11434/v1/chat/completions")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("TEI_URL", "http://localhost:8080")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from fastapi.testclient import TestClient

from production_inference import api
from production_inference import conversation_memory
from production_inference import qdrant as qdrant_module
from production_inference import retrieval_cache
from production_inference import sparse_embeddings
from production_inference.ingestion import dataparser


class IngestionUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.temp_dir.name)
        self.client = TestClient(api.app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_upload_is_staged_and_queued_for_background_processing(self) -> None:
        with (
            patch.object(api, "INGEST_STORAGE_DIR", self.storage_dir),
            patch.object(api, "enqueue_ingestion_job", new_callable=AsyncMock) as enqueue,
        ):
            response = self.client.post(
                "/ingest",
                files={"file": ("nvd.json.gz", b"compressed data", "application/gzip")},
            )

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["status_url"], f"/ingest/{body['job_id']}")
        enqueue.assert_awaited_once()
        _, job_id, staged_file = enqueue.await_args.args
        self.assertEqual(job_id, body["job_id"])
        staged_file = Path(staged_file)
        self.assertTrue(staged_file.exists())
        self.assertEqual(staged_file.read_bytes(), b"compressed data")

    def test_upload_above_limit_is_rejected_and_removed(self) -> None:
        with (
            patch.object(api, "INGEST_STORAGE_DIR", self.storage_dir),
            patch.object(api, "MAX_INGEST_FILE_SIZE_BYTES", 3),
            patch.object(api, "enqueue_ingestion_job", new_callable=AsyncMock) as enqueue,
        ):
            response = self.client.post(
                "/ingest",
                files={"file": ("nvd.json.gz", b"four", "application/gzip")},
            )

        self.assertEqual(response.status_code, 413)
        enqueue.assert_not_awaited()
        self.assertEqual(list(self.storage_dir.iterdir()), [])

    def test_redis_queue_error_returns_503_and_removes_upload(self) -> None:
        with (
            patch.object(api, "INGEST_STORAGE_DIR", self.storage_dir),
            patch.object(api, "enqueue_ingestion_job", new_callable=AsyncMock) as enqueue,
        ):
            enqueue.side_effect = api.redis.RedisError("connection refused")
            response = self.client.post(
                "/ingest",
                files={"file": ("nvd.json.gz", b"compressed data", "application/gzip")},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("Could not store or queue", response.json()["detail"])
        self.assertEqual(list(self.storage_dir.iterdir()), [])

    def test_status_endpoint_returns_progress(self) -> None:
        with (
            patch.object(api.redis_client, "hgetall", new_callable=AsyncMock) as hgetall,
        ):
            hgetall.return_value = {
                "status": "processing",
                "ingested": "14000",
            }
            response = self.client.get("/ingest/job-123")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"job_id": "job-123", "status": "processing", "ingested": 14000},
        )
        hgetall.assert_awaited_once_with(api.job_status_key("job-123"))

    def test_status_endpoint_returns_404_for_unknown_job(self) -> None:
        with patch.object(
            api.redis_client, "hgetall", new_callable=AsyncMock, return_value={}
        ):
            response = self.client.get("/ingest/missing")

        self.assertEqual(response.status_code, 404)

    def test_status_endpoint_includes_worker_error(self) -> None:
        with patch.object(
            api.redis_client,
            "hgetall",
            new_callable=AsyncMock,
            return_value={
                "status": "failed",
                "ingested": "14000",
                "error": "embedding service unavailable",
            },
        ):
            response = self.client.get("/ingest/job-failed")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["ingested"], 14000)
        self.assertEqual(response.json()["error"], "embedding service unavailable")


class IngestionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_records_completed_job_and_removes_staged_file(self) -> None:
        from production_inference.ingestion import worker

        with tempfile.NamedTemporaryFile() as staged:
            job = {"job_id": "job-123", "file": staged.name}
            redis_mock = AsyncMock()

            async def finish_ingestion(file_path, progress_callback):
                await progress_callback(32)
                return 40

            with patch.object(worker, "stream_nvd_gz", side_effect=finish_ingestion):
                await worker.process_job(redis_mock, job)

            self.assertFalse(Path(staged.name).exists())

        calls = redis_mock.hset.await_args_list
        self.assertEqual(calls[0].kwargs["mapping"]["status"], "processing")
        self.assertEqual(calls[1].kwargs["mapping"]["ingested"], 32)
        self.assertEqual(calls[2].kwargs["mapping"]["status"], "completed")
        self.assertEqual(calls[2].kwargs["mapping"]["ingested"], 40)
        redis_mock.lrem.assert_awaited_once()

    async def test_worker_retains_staged_file_after_permanent_failure(self) -> None:
        from production_inference.ingestion import worker

        with tempfile.NamedTemporaryFile() as staged:
            job = {"job_id": "job-failed", "file": staged.name}
            redis_mock = AsyncMock()

            with patch.object(
                worker,
                "stream_nvd_gz",
                side_effect=ValueError("invalid gzip"),
            ):
                await worker.process_job(redis_mock, job)

            self.assertTrue(Path(staged.name).exists())

        calls = redis_mock.hset.await_args_list
        self.assertEqual(calls[-1].kwargs["mapping"]["status"], "failed")
        redis_mock.lrem.assert_awaited_once()


class GzipStreamingTests(unittest.IsolatedAsyncioTestCase):
    def _write_records(self, record_count: int, path: str) -> None:
        records = {
            "vulnerabilities": [
                {
                    "cve": {
                        "id": f"CVE-2026-{i:04}",
                        "descriptions": [{"lang": "en", "value": f"Example {i}"}],
                        "metrics": {},
                    }
                }
                for i in range(record_count)
            ]
        }
        with gzip.open(path, "wt") as compressed:
            json.dump(records, compressed)

    async def test_parser_counts_ingested_records_and_uses_distinct_ids(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json.gz") as upload:
            self._write_records(2, upload.name)

            with patch.object(dataparser, "process_batch", new_callable=AsyncMock) as process:
                ingested = await dataparser.stream_nvd_gz(Path(upload.name))

        self.assertEqual(ingested, 2)
        process.assert_awaited_once()
        batch = process.await_args.args[0]
        self.assertEqual(len({item["uuid"] for item in batch}), 2)

    async def test_parser_splits_batches_at_embedding_service_limit(self) -> None:
        record_count = dataparser.BATCH_SIZE + 1
        batch_sizes: list[int] = []

        async def capture_batch(batch: list[dict[str, object]]) -> None:
            batch_sizes.append(len(batch))

        with tempfile.NamedTemporaryFile(suffix=".json.gz") as upload:
            self._write_records(record_count, upload.name)

            with patch.object(
                dataparser,
                "process_batch",
                new_callable=AsyncMock,
                side_effect=capture_batch,
            ):
                ingested = await dataparser.stream_nvd_gz(Path(upload.name))

        self.assertEqual(ingested, record_count)
        self.assertEqual(
            batch_sizes,
            [dataparser.BATCH_SIZE, 1],
        )


class ExistingPointSkipTests(unittest.IsolatedAsyncioTestCase):
    def _record(self, cve_id: str, text: str) -> dict[str, str]:
        from uuid import NAMESPACE_URL, uuid5

        return {
            "uuid": str(uuid5(NAMESPACE_URL, cve_id)),
            "cve_id": cve_id,
            "text": text,
            "severity": "LOW",
        }

    async def test_existing_batch_skips_embedding_and_upsert(self) -> None:
        batch = [self._record("CVE-2026-0001", "Already indexed")]
        qdrant_client = AsyncMock()
        qdrant_client.collection_exists.return_value = True
        qdrant_client.retrieve.return_value = [
            SimpleNamespace(id=batch[0]["uuid"])
        ]

        with (
            patch.object(dataparser, "qdrant", qdrant_client),
            patch.object(dataparser, "bm25_model") as bm25,
            patch.object(dataparser.httpx, "AsyncClient") as async_client,
        ):
            await dataparser.process_batch(batch)

        qdrant_client.retrieve.assert_awaited_once()
        qdrant_client.upsert.assert_not_awaited()
        bm25.embed.assert_not_called()
        async_client.assert_not_called()

    async def test_qdrant_lookup_timeout_retries_instead_of_embedding(self) -> None:
        batch = [self._record("CVE-2026-0001", "Already indexed")]
        qdrant_client = AsyncMock()
        qdrant_client.collection_exists.return_value = True
        qdrant_client.retrieve.side_effect = [
            httpx.ConnectTimeout("temporary Qdrant outage"),
            [SimpleNamespace(id=batch[0]["uuid"])],
        ]

        with (
            patch.object(dataparser, "qdrant", qdrant_client),
            patch.object(dataparser, "bm25_model") as bm25,
            patch.object(dataparser.httpx, "AsyncClient") as async_client,
            patch.object(dataparser.asyncio, "sleep", new_callable=AsyncMock),
        ):
            await dataparser.process_batch(batch)

        self.assertEqual(qdrant_client.retrieve.await_count, 2)
        qdrant_client.upsert.assert_not_awaited()
        bm25.embed.assert_not_called()
        async_client.assert_not_called()

    async def test_partial_batch_embeds_and_upserts_only_missing_points(self) -> None:
        existing = self._record("CVE-2026-0001", "Already indexed")
        missing = self._record("CVE-2026-0002", "Needs indexing")
        qdrant_client = AsyncMock()
        qdrant_client.collection_exists.return_value = True
        qdrant_client.retrieve.return_value = [
            SimpleNamespace(id=existing["uuid"])
        ]

        response = SimpleNamespace(
            raise_for_status=unittest.mock.Mock(),
            json=unittest.mock.Mock(return_value=[[0.1, 0.2, 0.3]]),
        )
        http_client = AsyncMock()
        http_client.post.return_value = response
        async_client_factory = unittest.mock.MagicMock()
        async_client_factory.return_value.__aenter__ = AsyncMock(
            return_value=http_client
        )
        async_client_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        sparse_embedding = SimpleNamespace(
            indices=SimpleNamespace(tolist=lambda: [1]),
            values=SimpleNamespace(tolist=lambda: [0.5]),
        )

        with (
            patch.object(dataparser, "qdrant", qdrant_client),
            patch.object(dataparser, "ensure_incidents_collection", new_callable=AsyncMock),
            patch.object(dataparser.bm25_model, "embed", return_value=[sparse_embedding]) as embed_sparse,
            patch.object(dataparser.httpx, "AsyncClient", async_client_factory),
        ):
            await dataparser.process_batch([existing, missing])

        http_client.post.assert_awaited_once_with(
            f"{dataparser.TEI_URL}/embed",
            json={"inputs": ["Needs indexing"]},
        )
        embed_sparse.assert_called_once_with(["Needs indexing"])
        qdrant_client.upsert.assert_awaited_once()
        points = qdrant_client.upsert.await_args.kwargs["points"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].id, missing["uuid"])
        self.assertEqual(points[0].payload["tenant_id"], "global_shared")
        self.assertEqual(points[0].payload["severity"], "LOW")


class QdrantCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_filters_for_tenant_and_shared_records_by_severity(self) -> None:
        point = SimpleNamespace(payload={"text": "CVE details"})
        sparse_query = qdrant_module.models.SparseVector(
            indices=[10, 20], values=[1.0, 2.0]
        )
        with patch.object(
            qdrant_module.client,
            "query_points",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(points=[point]),
        ) as query_points:
            results = await qdrant_module.search_incidents(
                [0.1, 0.2], sparse_query, "tenant-a", ["LOW", "MEDIUM"]
            )

        self.assertEqual(results, ["CVE details"])
        query_call = query_points.await_args
        if query_call is None:
            self.fail("Qdrant query_points was not called")
        query = query_call.kwargs
        query_filter = query["query_filter"]
        self.assertEqual(query_filter.must[0].key, "tenant_id")
        self.assertEqual(query_filter.must[0].match.any, ["tenant-a", "global_shared"])
        self.assertEqual(query_filter.must[1].key, "severity")
        self.assertEqual(query_filter.must[1].match.any, ["LOW", "MEDIUM"])
        self.assertEqual(len(query_filter.must), 2)
        self.assertEqual(query["query"].fusion, qdrant_module.models.Fusion.RRF)
        self.assertEqual(query["limit"], qdrant_module.SEARCH_RESULT_LIMIT)
        self.assertEqual(len(query["prefetch"]), 2)
        self.assertEqual(query["prefetch"][0].using, qdrant_module.DENSE_VECTOR_NAME)
        self.assertEqual(query["prefetch"][0].query, [0.1, 0.2])
        self.assertEqual(query["prefetch"][1].using, qdrant_module.SPARSE_VECTOR_NAME)
        self.assertEqual(query["prefetch"][1].query, sparse_query)
        for prefetch in query["prefetch"]:
            self.assertIs(prefetch.filter, query_filter)
            self.assertEqual(prefetch.limit, qdrant_module.HYBRID_PREFETCH_LIMIT)
        self.assertEqual(query["with_payload"], ["text"])

    async def test_search_uses_dense_retrieval_when_sparse_query_is_empty(self) -> None:
        sparse_query = qdrant_module.models.SparseVector(indices=[], values=[])
        with patch.object(
            qdrant_module.client,
            "query_points",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(points=[]),
        ) as query_points:
            await qdrant_module.search_incidents(
                [0.1, 0.2], sparse_query, "tenant-a", ["LOW"]
            )

        query_call = query_points.await_args
        if query_call is None:
            self.fail("Qdrant query_points was not called")
        query = query_call.kwargs
        self.assertEqual(len(query["prefetch"]), 1)
        self.assertEqual(query["prefetch"][0].using, qdrant_module.DENSE_VECTOR_NAME)

    async def test_search_adds_exact_cve_payload_filter(self) -> None:
        sparse_query = qdrant_module.models.SparseVector(
            indices=[10], values=[1.0]
        )
        with patch.object(
            qdrant_module.client,
            "query_points",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(points=[]),
        ) as query_points:
            await qdrant_module.search_incidents(
                [0.1, 0.2],
                sparse_query,
                "tenant-a",
                ["LOW", "MEDIUM"],
                query_text="Tell me about cve-2026-21428",
            )

        query_call = query_points.await_args
        if query_call is None:
            self.fail("Qdrant query_points was not called")
        filters = query_call.kwargs["query_filter"].must
        self.assertEqual(len(filters), 3)
        self.assertEqual(filters[2].key, "cve_id")
        self.assertEqual(filters[2].match.value, "CVE-2026-21428")

    async def test_search_does_not_add_cve_filter_without_an_id(self) -> None:
        sparse_query = qdrant_module.models.SparseVector(
            indices=[10], values=[1.0]
        )
        with patch.object(
            qdrant_module.client,
            "query_points",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(points=[]),
        ) as query_points:
            await qdrant_module.search_incidents(
                [0.1, 0.2],
                sparse_query,
                "tenant-a",
                ["LOW", "MEDIUM"],
                query_text="How do I mitigate remote execution?",
            )

        query_call = query_points.await_args
        if query_call is None:
            self.fail("Qdrant query_points was not called")
        filters = query_call.kwargs["query_filter"].must
        self.assertEqual([condition.key for condition in filters], ["tenant_id", "severity"])

    async def test_missing_collection_created_with_dense_and_sparse_vectors(self) -> None:
        with patch.object(qdrant_module, "client") as client:
            client.collection_exists = AsyncMock(return_value=False)
            client.create_collection = AsyncMock(return_value=True)

            await qdrant_module.ensure_incidents_collection(768)

        client.create_collection.assert_awaited_once()
        create_call = client.create_collection.await_args
        if create_call is None:
            self.fail("Qdrant collection creation was not called")
        kwargs = create_call.kwargs
        self.assertEqual(kwargs["collection_name"], "nvd_incidents")
        self.assertEqual(kwargs["vectors_config"]["dense"].size, 768)
        self.assertEqual(kwargs["vectors_config"]["dense"].distance, qdrant_module.models.Distance.COSINE)
        self.assertIn("sparse", kwargs["sparse_vectors_config"])
        self.assertNotIn("wait", kwargs)

    async def test_existing_collection_is_not_recreated(self) -> None:
        with patch.object(qdrant_module, "client") as client:
            client.collection_exists = AsyncMock(return_value=True)
            client.create_collection = AsyncMock()

            await qdrant_module.ensure_incidents_collection(768)

        client.create_collection.assert_not_awaited()


class SparseQueryEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sparse_query_uses_bm25_query_embedding(self) -> None:
        embedding = SimpleNamespace(
            indices=SimpleNamespace(tolist=lambda: [123, 456]),
            values=SimpleNamespace(tolist=lambda: [1.0, 2.0]),
        )
        model = SimpleNamespace(query_embed=lambda text: iter([embedding]))

        with patch.object(sparse_embeddings, "get_bm25_model", return_value=model):
            result = await sparse_embeddings.generate_sparse_embedding(
                "CVE-2026-21437 remote code execution"
            )

        self.assertEqual(result.indices, [123, 456])
        self.assertEqual(result.values, [1.0, 2.0])


class SearchContextCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_miss_sets_two_minute_ttl_and_next_call_hits_cache(self) -> None:
        redis_client = AsyncMock()
        redis_client.get.side_effect = [None, '["CVE details"]']
        loader = AsyncMock(return_value=["CVE details"])

        first_result, first_cache_hit = await retrieval_cache.get_or_load_search_context(
            redis_client, "CVE-2026-21428", "tenant-a", ["LOW", "HIGH"], loader
        )
        second_result, second_cache_hit = await retrieval_cache.get_or_load_search_context(
            redis_client, "CVE-2026-21428", "tenant-a", ["HIGH", "LOW"], loader
        )

        self.assertEqual(first_result, ["CVE details"])
        self.assertEqual(second_result, ["CVE details"])
        self.assertFalse(first_cache_hit)
        self.assertTrue(second_cache_hit)
        loader.assert_awaited_once_with()
        setex_call = redis_client.setex.await_args
        if setex_call is None:
            self.fail("Search context was not written to Redis")
        cache_key, ttl, serialized_context = setex_call.args
        self.assertEqual(ttl, 120)
        self.assertEqual(serialized_context, '["CVE details"]')
        self.assertEqual(
            cache_key,
            retrieval_cache.search_context_cache_key(
                "CVE-2026-21428", "tenant-a", ["LOW", "HIGH"]
            ),
        )

    async def test_cache_key_is_scoped_by_query_tenant_and_clearance(self) -> None:
        base_key = retrieval_cache.search_context_cache_key(
            "CVE-2026-21428", "tenant-a", ["LOW", "HIGH"]
        )
        self.assertNotEqual(
            base_key,
            retrieval_cache.search_context_cache_key(
                "CVE-2026-21428", "tenant-b", ["LOW", "HIGH"]
            ),
        )
        self.assertNotEqual(
            base_key,
            retrieval_cache.search_context_cache_key(
                "CVE-2026-21428", "tenant-a", ["LOW"]
            ),
        )
        self.assertNotEqual(
            base_key,
            retrieval_cache.search_context_cache_key(
                "CVE-2026-21428 ", "tenant-a", ["LOW", "HIGH"]
            ),
        )

    async def test_invalid_cached_value_is_deleted_and_reloaded(self) -> None:
        redis_client = AsyncMock()
        redis_client.get.return_value = '{"not":"a context list"}'
        loader = AsyncMock(return_value=["fresh context"])

        result, cache_hit = await retrieval_cache.get_or_load_search_context(
            redis_client, "query", "tenant-a", ["LOW"], loader
        )

        self.assertEqual(result, ["fresh context"])
        self.assertFalse(cache_hit)
        redis_client.delete.assert_awaited_once()
        redis_client.setex.assert_awaited_once()
        loader.assert_awaited_once_with()


class ConversationMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_key_is_scoped_to_tenant_subject_and_conversation(self) -> None:
        base_key = conversation_memory.conversation_history_key(
            "tenant-a", "user-1", ["LOW"], "conversation-1"
        )
        self.assertNotEqual(
            base_key,
            conversation_memory.conversation_history_key(
                "tenant-a", "user-2", ["LOW"], "conversation-1"
            ),
        )
        self.assertNotEqual(
            base_key,
            conversation_memory.conversation_history_key(
                "tenant-b", "user-1", ["LOW"], "conversation-1"
            ),
        )
        self.assertNotEqual(
            base_key,
            conversation_memory.conversation_history_key(
                "tenant-a", "user-1", ["LOW"], "conversation-2"
            ),
        )
        self.assertNotEqual(
            base_key,
            conversation_memory.conversation_history_key(
                "tenant-a", "user-1", ["LOW", "HIGH"], "conversation-1"
            ),
        )

    async def test_load_history_keeps_only_valid_recent_messages(self) -> None:
        redis_client = AsyncMock()
        redis_client.lrange.return_value = [
            json.dumps({"role": "user", "content": f"turn-{i}"})
            for i in range(conversation_memory.MAX_CONVERSATION_MESSAGES + 2)
        ] + ['{"role":"system","content":"ignore"}', "not-json"]

        history = await conversation_memory.load_conversation_history(
            redis_client, "tenant-a", "user-1", ["LOW"], "conversation-1"
        )

        self.assertEqual(len(history), conversation_memory.MAX_CONVERSATION_MESSAGES)
        self.assertEqual(history[0]["content"], "turn-2")
        self.assertEqual(
            history[-1]["content"],
            f"turn-{conversation_memory.MAX_CONVERSATION_MESSAGES + 1}",
        )

    async def test_append_turn_trims_history_and_refreshes_expiry(self) -> None:
        pipeline = MagicMock()
        pipeline.execute = AsyncMock()
        pipeline.__aenter__ = AsyncMock(return_value=pipeline)
        pipeline.__aexit__ = AsyncMock(return_value=None)
        pipeline_factory = MagicMock(return_value=pipeline)
        redis_client = SimpleNamespace(pipeline=pipeline_factory)

        await conversation_memory.append_conversation_turn(
            redis_client,
            "tenant-a",
            "user-1",
            ["LOW"],
            "conversation-1",
            "follow-up",
            "answer",
        )

        key = conversation_memory.conversation_history_key(
            "tenant-a", "user-1", ["LOW"], "conversation-1"
        )
        pipeline_factory.assert_called_once_with(transaction=True)
        pipeline.rpush.assert_called_once_with(
            key,
            json.dumps({"role": "user", "content": "follow-up"}),
            json.dumps({"role": "assistant", "content": "answer"}),
        )
        pipeline.ltrim.assert_called_once_with(
            key, -conversation_memory.MAX_CONVERSATION_MESSAGES, -1
        )
        pipeline.expire.assert_called_once_with(
            key, conversation_memory.CONVERSATION_TTL_SECONDS
        )
        pipeline.execute.assert_awaited_once_with()


class RetryClassificationTests(unittest.TestCase):
    def test_network_and_server_errors_are_retryable(self) -> None:
        request = httpx.Request("POST", "http://embedding.test/embed")
        response = httpx.Response(503, request=request)
        server_error = httpx.HTTPStatusError(
            "service unavailable", request=request, response=response
        )

        self.assertTrue(dataparser.is_retryable_service_error(httpx.ConnectError("down")))
        self.assertTrue(dataparser.is_retryable_service_error(server_error))
        self.assertTrue(
            dataparser.is_retryable_service_error(
                httpx.ReadTimeout("timed out", request=request)
            )
        )

    def test_client_validation_errors_are_not_retryable(self) -> None:
        request = httpx.Request("POST", "http://embedding.test/embed")
        response = httpx.Response(422, request=request)
        validation_error = httpx.HTTPStatusError(
            "invalid request", request=request, response=response
        )

        self.assertFalse(dataparser.is_retryable_service_error(validation_error))
        self.assertFalse(dataparser.is_retryable_service_error(ValueError("invalid data")))


if __name__ == "__main__":
    unittest.main()
