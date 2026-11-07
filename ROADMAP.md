# Arnab Roy | AI and Data Engineering Roadmap

**Planning horizon:** 6-12 months  
**Starting capacity:** 20+ focused hours per week  
**Career target:** Internship in AI/ML infrastructure, data engineering, MLOps, or distributed systems  
**Flagship project:** Multi-tenant cybersecurity RAG using FastAPI, TEI, Qdrant, and NVD data

## North Star

Build one project that demonstrates production engineering judgment, not just a list of technologies. The project should make tenant access trustworthy, retrieval quality measurable, ingestion replayable, and operations explainable. Use it to support internship applications while continuing interview preparation and one focused cloud-learning track.

The build order matters: **identity and reproducibility -> retrieval evaluation -> event-driven ingestion -> measured shard design -> operations and portfolio polish.**

## Current Baseline

The workspace contains a FastAPI service, TEI embedding calls, Qdrant search, Redis rate limiting, a batch NVD importer, and a multi-node Qdrant Compose configuration.

The first implementation slice is in place:

- The API container now targets Python 3.12, matching `requires-python`.
- `/search` accepts a query and derives tenant and role from a validated OIDC bearer token.
- OIDC verification checks JWKS signature, issuer, audience, expiry, and subject; the trusted role must match the configured clearance map.
- Search fails closed if identity-provider configuration is missing. Configure `AUTH_JWKS_URL`, `AUTH_ISSUER`, and `AUTH_AUDIENCE` for the API.
- Five focused authentication and route tests pass. Docker image build still needs to be checked with Docker Desktop running.

Important gaps remain: there is no hybrid sparse retrieval or Kafka pipeline yet, and custom shard keys have not been benchmarked. The local Compose topology is not proof of production availability or tested Raft recovery.

## Phase Plan

### Phase 1 | Reproducible and Secure Service (Weeks 1-4)

1. Record a baseline: API behavior, collection schema, embedding model and dimensions, dataset provenance, and current search latency.
2. Run a clean Docker build and Compose smoke test after starting Docker Desktop. Confirm Python version, service DNS, and API-to-TEI/Qdrant connectivity.
3. Configure an OIDC issuer and document its exact token claim contract: `iss`, `aud`, `exp`, `sub`, `tenant_id`, and `role`.
4. Add rejection tests for invalid signatures, wrong issuer/audience, expired tokens, missing tenant claims, unsupported roles, and cross-tenant searches.
5. Verify that client-supplied tenant and role values cannot influence any data access path. Keep Qdrant payload filters even though the API now derives identity from a signed token.
6. Check proxy trust and rate-limit behavior. Trust forwarded client IP headers only when the request can reach the API through the known proxy boundary.
7. Separate liveness from readiness. Readiness should reflect whether required dependencies are usable; liveness should not fail merely because a downstream service is briefly unavailable.

**Exit criteria:** a reproducible local run, a documented identity contract, passing negative authorization tests, and no caller-controlled tenant or role in search execution.

### Phase 2 | Hybrid Retrieval With Evidence (Weeks 4-7)

1. Create a small labeled relevance set with exact CVE/CPE identifiers, product and version strings, and natural-language incident questions.
2. Add a sparse BM25 representation using FastEmbed alongside the existing dense embedding. Use Qdrant named vectors and sparse indexing rather than replacing the current dense vector in place.
3. Implement a hybrid query with dense and sparse retrieval, beginning with reciprocal rank fusion. Keep the tenant and clearance filters applied to every retrieval path.
4. Version the collection schema and model configuration. Plan a backfill or blue-green collection migration so existing vectors are not silently interpreted under a new schema.
5. Compare dense-only, sparse-only, and hybrid retrieval using Recall@k plus MRR or nDCG. Record p50/p95 latency and resource use under the same data and query set.
6. Review failures manually. Confirm sparse retrieval improves exact-match queries without materially damaging semantic results.

**Exit criteria:** a repeatable benchmark and a measured reason to keep the hybrid design. No claimed quality gain without the evaluation results.

### Phase 3 | Event-Driven Ingestion With Replay Safety (Weeks 7-11)

1. Define a versioned event envelope containing event ID, tenant ID, source/document ID, source version, event type, timestamps, and payload reference or content.
2. Decide which component owns publishing, validation, embedding, and Qdrant writes. Keep the existing batch importer as a backfill/replay path while establishing one clear source of truth.
3. Choose topic and partition keys based on ordering needs. A stable tenant/document key can preserve per-document order; avoid a single hot partition for a large tenant.
4. Use deterministic Qdrant point IDs and content/version hashes so retrying an event is safe. Define update, delete, and duplicate-event behavior explicitly.
5. Commit Kafka offsets only after the corresponding Qdrant write is durable. Design for at-least-once delivery with idempotent effects; do not claim end-to-end exactly-once semantics.
6. Add bounded consumer concurrency, retry with backoff, poison-message handling, a dead-letter path, and shutdown behavior that does not silently lose in-flight work.
7. Test duplicates, replay, crash-before-offset-commit, malformed input, Qdrant downtime, recovery, and sustained backlog. Measure consumer lag and processing failures.

**Exit criteria:** replaying the same input converges to the same logical Qdrant state, and failure/recovery behavior is demonstrated by tests or a recorded exercise.

### Phase 4 | Benchmark Custom Shard Keys (Weeks 11-14)

1. Capture an unsharded baseline by tenant size: p50/p95 query latency, throughput, shard skew, and resource use.
2. Test custom shard keys using representative small, typical, and large tenants. Pass the same tenant key consistently on writes and searches.
3. Retain authorization checks and tenant payload filters. A shard key is a routing and isolation tool, not an identity or access-control mechanism.
4. Compare tail latency, shard balance, and operational overhead. Small tenants can create excessive shard overhead; a very large tenant can still be a hot tenant.
5. Define schema migration, backfill, and rollback before adopting the key strategy.

**Exit criteria:** adopt custom shard keys only when the workload demonstrates a meaningful latency or isolation benefit that justifies their complexity.

### Phase 5 | Operability and Failure Readiness (Weeks 14-18)

1. Add structured logs with request/correlation IDs and trace the API -> embedding -> Qdrant/Kafka path.
2. Track request rate, errors, duration, dependency failures, Kafka lag, retry counts, and dead-letter volume.
3. Set explicit timeouts, bounded retries, graceful shutdown, resource limits, and secret handling. Avoid logging tokens or sensitive incident content.
4. Exercise Qdrant node loss, API dependency outages, Kafka consumer restarts, and recovery. Record what was actually tested and what remains an assumption.
5. Document backup/restore or rebuild procedures, deploy steps, common failure diagnosis, and operational limits.

**Exit criteria:** a reviewer can follow a short runbook to run the system, identify common failures, and restore or rebuild data.

### Phase 6 | Portfolio and Recruiting (Weeks 18-36; recruiting starts in Week 1)

1. Publish a clear architecture diagram and short threat model.
2. Write concise design records for the identity boundary, hybrid fusion, Kafka delivery semantics, and shard-key decision.
3. Document benchmark data, methodology, versions, and known limitations. Keep measured results separate from goals.
4. Prepare a short demo using realistic NVD queries. Ensure an outside reviewer can reproduce it without private data or credentials.
5. Tailor a one-page resume to AI infrastructure, data engineering, MLOps, and distributed-systems roles. Make each project claim verifiable and outcome-based.
6. Apply before the project is complete. Track roles, referrals, applications, follow-ups, and interview feedback weekly.
7. Use GDG on Campus, IEEE HIT, CSI HIT, workshops, and hackathons to get design feedback, collaborate, and present finished technical work. Favor contributions that leave a public artifact over attendance alone.
8. Add a second project only if it demonstrates a distinct capability not established by this flagship.

**Exit criteria:** the project is reproducible, its tradeoffs are explainable, the resume reflects measured results, and applications/interview practice are ongoing.

## Weekly Operating Rhythm

A starting allocation for 20-23 hours outside coursework:

| Focus | Hours/week | Output |
| --- | ---: | --- |
| Flagship project | 10-12 | One small, tested milestone each week |
| DSA and interviews | 4-5 | Timed problems, review notes, and explanation practice |
| Cloud learning | 2-3 | Hands-on deployment or one certification topic |
| Applications and community | 2-3 | Targeted applications, networking, presentation, or contribution |

During exams, reduce project scope rather than abandoning sleep or interview consistency. Review the weekly allocation monthly against coursework and recruiting activity.

## Interview Preparation

- Keep DSA practice interview-shaped: solve under a time limit, explain complexity, review mistakes, and revisit patterns rather than maximizing problem count.
- Practice system-design topics directly related to the project: tenant authorization, vector schema migration, Kafka partitioning, idempotency, backpressure, retries, Qdrant replication, and failure recovery.
- At least monthly during recruiting, complete one mock coding interview and one system-design discussion. Explain alternatives and failure modes, not only the happy path.
- Be ready to distinguish at-least-once delivery from exactly-once effects, authentication from database filtering, and sharding from authorization.

## Cloud Credential Strategy

Pick one primary cloud provider based on internship descriptions and the platform you can deploy affordably. Build and operate a small real workload before booking a certification. Use the other provider's learning resources selectively; do not pursue two certifications in parallel by default. Verify the current exam name, cost, availability, and prerequisites before scheduling because certification catalogs change.

## Definition of Done

- Tenant and role come from a trusted signed identity, with negative authorization tests.
- Search quality is evaluated on a fixed query set and compared against a baseline.
- Ingestion is replayable and idempotent under retries and consumer restarts.
- Shard-key adoption is supported by workload measurements.
- Operational claims are backed by recorded failure exercises and a runbook.
- A reviewer can reproduce the demo and understand what is production-tested versus experimental.

## Guardrails

- Do not add Kafka, sparse vectors, or shard keys solely to make the stack look modern; each must solve a measured problem.
- Do not treat local multi-container Compose as proof of high availability or Raft fault tolerance.
- Do not expose secrets, tokens, private vulnerability data, or real tenant information in the repository or demo.
- Do not let certification study or a second side project crowd out the flagship, applications, and interview practice.
- Do not claim performance or relevance improvements until benchmarked against a recorded baseline.
