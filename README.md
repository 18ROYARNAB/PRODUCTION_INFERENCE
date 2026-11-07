## Web UI

The lightweight UI is served at `http://localhost:8000/` by the Compose Nginx load balancer. Enter an OIDC bearer token (or the explicitly configured local-development token), then use **Ask a question** or **Search records**. Results display retrieved context and Redis cache status. The page uses Tailwind's CDN build and plain JavaScript; API requests stay same-origin through Nginx.

## Application logs

The API, ingestion worker, ingestion utilities, and retrieval benchmark use a shared logger configured to include an ISO-style local date and time, severity, and module name. Logs focus on operational events and failures; request prompts, tokens, and full request bodies are not logged.

## Search authentication

`POST /search` and `POST /chat` require an OIDC bearer token. The token must be signed with RS256 or ES256 and contain valid `iss`, `aud`, `exp`, and `sub` claims, plus a non-empty `tenant_id` and a `role` matching one of the roles configured in `production_inference.qdrant.ROLE_CLEARANCE`.

Configure these environment variables for the API:

- `AUTH_JWKS_URL`: the identity provider's JSON Web Key Set URL.
- `AUTH_ISSUER`: the expected token issuer.
- `AUTH_AUDIENCE`: the expected token audience.

Search requests accept only a `query`; tenant and role values in the request body are rejected. Missing identity-provider configuration fails closed with HTTP 503. Use an identity provider that issues the tenant and role claims from trusted authorization data; do not copy these claims from untrusted client input.

For local development only, enable a fixed bearer token without an identity provider by adding these settings to your local `.env`:

```dotenv
APP_ENV=development
AUTH_DEV_MODE=true
AUTH_DEV_TOKEN=local-dev-token
```

Then rebuild and restart the API with `docker compose up -d --build api`. The development principal defaults to tenant `local-dev-tenant` and role `L1_Helpdesk`; override with `AUTH_DEV_TENANT_ID` or `AUTH_DEV_ROLE` if needed. In `/docs`, click **Authorize** and enter `local-dev-token` (without the `Bearer` prefix). This bypass is enabled only when both development settings are explicit; never use it on an internet-accessible or production deployment.

Search uses hybrid retrieval: TEI dense vectors and FastEmbed BM25 sparse vectors are queried in parallel and combined by Qdrant reciprocal-rank fusion. Both candidate searches enforce the authenticated tenant/shared-corpus and severity filters. The sparse query encoder uses the same `Qdrant/bm25` model as ingestion; the dense embedding dimension and stored collection schema are unchanged.

When a query contains a CVE identifier (for example, `CVE-2026-21437`), retrieval also adds an exact `cve_id` payload condition, while retaining the tenant and severity authorization filters.

Retrieved search context is cached in Redis for 120 seconds. Cache keys are scoped by the exact query, tenant, and allowed severities, so one authorization scope cannot reuse another scope's cached results. Both `/search` and `/chat` share this cache and return `cache_hit: true|false` to make cache reuse visible in Swagger and API responses.

Docker Compose runs Redis as `rate-limiter-cache` with persistent AOF storage and a health check. Compose explicitly sets `REDIS_URL=redis://rate-limiter-cache:6379/0` for both the API (rate limiting and retrieval cache) and ingestion worker (job queue), overriding any host-specific value from `.env`; the API and worker wait for Redis to become healthy before starting.

Search results include records scoped to the authenticated tenant and the shared `global_shared` NVD corpus. Severity access is enforced against each record's `severity` payload field, which is populated from the NVD CVSS data.

`POST /chat` embeds the prompt, retrieves authorized incident context using the same tenant and severity rules as `/search`, and includes that context when asking the LLM to answer. The response includes `model_output`, `hits_found`, and the retrieved `safe_context`. If no matching records are found, the LLM is instructed not to invent incident details.

Chat accepts an optional `conversation_id` UUID and returns one for new conversations. Follow-up requests send that ID to load recent user/assistant turns from Redis; each turn still retrieves fresh authorized context, using prior user questions to resolve short follow-ups. History is isolated by verified tenant, OIDC subject, and severity clearance, limited to the most recent 10 turns, and expires 30 minutes after the last successful turn. The web UI reuses the ID during a conversation; **New conversation** starts a fresh history.

## Retrieval benchmark

Use `scripts/benchmark_retrieval.py` with the same NVD JSON (or `.json.gz`) file used for ingestion to extract an alternating evaluation sample and compare dense-only, BM25-only, and hybrid RRF search against the live Qdrant collection:

```sh
./.venv/bin/python scripts/benchmark_retrieval.py \
  --input /path/to/nvdcve-2.0-2026.json \
  --limit 100 \
  --offset 0 \
  --extract-output /tmp/nvd-eval.jsonl \
  --report-output /tmp/retrieval-benchmark.json
```

The script follows ingestion eligibility (an English CVE description and an ID), uses the same deterministic CVE point IDs, and by default samples every other record whose severity is visible to the highest configured production role. To avoid evaluating an exact self-match, it removes CVE identifiers and every third whitespace-delimited token from the description used as each query. It verifies which sampled point IDs exist in Qdrant and reports missing targets separately. Set `QDRANT_URL` and `TEI_URL` if the local defaults (`127.0.0.1:6333` and `127.0.0.1:7997`) do not apply. Use `--offset 1` for the other half of the alternating split. Metrics include Recall@k, MRR, top-1 accuracy, and Qdrant query latency; the JSON report also contains per-CVE ranks. Compare the `hybrid` metrics with dense and sparse baselines.

On the 2026 NVD JSON file, both offsets were tested with 100 targets each. All 200 unique targets were present in Qdrant. Results below aggregate both disjoint splits; latency is Qdrant query time only and excludes embedding:

| Retrieval | Recall@5 | MRR | Top-1 accuracy | Mean Qdrant latency |
| --- | ---: | ---: | ---: | ---: |
| Dense | 0.970 | 0.9453 | 0.930 | 13.490 ms |
| BM25 sparse | 1.000 | 0.9950 | 0.990 | 10.397 ms |
| Hybrid RRF | 1.000 | 0.9817 | 0.965 | 31.843 ms |

This is a repeatable retrieval sanity check, not an independent natural-language evaluation: queries are mechanically derived from each target description by dropping tokens. The strong scores should not be presented as production user-query quality. In this sample BM25 alone ranked targets better and faster than hybrid, so tune and evaluate RRF with independently authored user-style questions before claiming hybrid is superior.

## RAG question set

`evaluation/rag_questions.jsonl` is a starter gold set of independently worded questions grounded in the 2026 NVD descriptions shipped with this project. Each JSONL row has a stable `id`, `user_input`, `reference`, `reference_contexts`, and `metadata`; it includes factual lookup, comparison, impact/precondition, and abstention cases. The reference contexts mirror the NVD descriptions stored as Qdrant text payloads, so the abstention example expects the system not to invent a CVSS score or vector absent from that context.

Install the optional DeepEval dependency:

```sh
uv sync --extra evaluation
```

The runner calls the authenticated `/chat` endpoint for each question, saves the actual answer and retrieved context, then uses DeepEval metrics for answer relevancy, faithfulness, contextual precision, contextual recall, and reference-based answer correctness. It uses Groq as the judge via the existing `GROP_API_KEY` environment variable; the judge model defaults to `openai/gpt-oss-120b` and can be overridden with `DEEPEVAL_MODEL`. Do not commit API keys or evaluation reports containing model answers.

Start the app and ensure the question set's CVEs are indexed in Qdrant. Then run:

```sh
./.venv/bin/python -m evaluation.run_deepeval \
  --output /tmp/deepeval-report.json
```

For local development auth, the runner can reuse `AUTH_DEV_TOKEN` only when `APP_ENV=development` and `AUTH_DEV_MODE=true`; otherwise set `EVAL_BEARER_TOKEN` to a bearer token authorized to read all test records. The set includes CRITICAL records, so the caller must have `SecOps_Lead` clearance. Set `EVAL_API_URL` or pass `--api-url` when the service is not at `http://127.0.0.1:8000`. Requests are spaced by four seconds by default to stay under the app's 15-request/minute limit; change this with `--request-delay`.

Review individual question results and judge reasons in the report. LLM-as-judge scores are estimates, and this small seed set is not statistically representative; manually review failures and grow the set with independently authored questions before using scores as release criteria.

## File ingestion

`POST /ingest` accepts `.json.gz` files up to 100 MB. Nginx forwards the request without buffering the full body, and the API stages the uploaded file in `INGEST_STORAGE_DIR` (the `ingest_storage` Docker volume in Compose). It returns HTTP 202 with a `job_id` and `status_url` once Redis has queued the file; a separate ingestion worker parses it in batches and writes results to Qdrant without holding the gateway request open.

Poll `GET /ingest/{job_id}` for `queued`, `processing`, `completed`, or `failed` status and the number of records found in the input. Gateway request timeouts do not cancel a queued job. Before embedding each batch, the worker checks its deterministic CVE IDs in Qdrant and skips IDs already present, embedding and upserting only missing records. This makes resubmitting a partially processed file safe and avoids repeating embeddings for records already stored. The worker retries transient embedding/Qdrant network errors, request timeouts, rate limits, and server errors indefinitely with capped backoff; the timeout limits an individual attempt, not the whole ingestion. If a worker stops unexpectedly, it requeues its in-progress job on restart. Redis AOF and its `redis_data` volume preserve queued job data across a Redis container restart. Completed job files are removed; failed job files are retained in the shared upload volume for diagnosis or recovery. Completed/failed job statuses remain in Redis until manually removed.

## Starting the Qdrant cluster

The three Qdrant nodes use fixed Compose DNS peer URIs on a dedicated internal cluster network. Each node has its own persistent `qdrant_data_N` directory; the image is pinned to Qdrant 1.19.0 by default. Override it with `QDRANT_IMAGE` only when deliberately upgrading all peers together.

Start the bootstrap peer and then its two joiners with:

```sh
./scripts/start_qdrant_cluster.sh
```

The script starts the bootstrap node first, starts the two joining nodes after it responds, and waits for Qdrant to report all three stable DNS peer URIs and a leader/follower role. It does not remove or reset Qdrant storage. If it reports stale peer addresses or no elected leader, inspect the displayed Qdrant logs and plan a Qdrant-supported recovery; do not delete storage directories to resolve Raft state.

The first ingestion creates the `nvd_incidents` collection if it does not exist, using the live TEI vector dimension and a sparse BM25 vector. Qdrant must have a healthy cluster leader before ingestion can create the collection or upsert points.

To create or verify the collection separately, run `./scripts/create_qdrant_collection.sh`. It checks for a Qdrant leader before changing cluster state and discovers the dense vector size from TEI. By default it connects to `http://127.0.0.1:6333` and `http://127.0.0.1:7997`; override these with `QDRANT_URL` and `TEI_URL` when needed. Set `EMBEDDING_SIZE` to a positive integer if TEI cannot be queried. The script does not reset existing collections and exits if an existing collection has an incompatible schema.
