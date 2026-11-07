# AI Engineer Skill-Readiness Checklist

**Owner:** Arnab Roy  
**Target:** AI/ML infrastructure, MLOps, data engineering, or distributed-systems internship  
**Review cadence:** Weekly for active work; monthly for overall readiness

## How to Use This Checklist

Mark an item complete only when you can do it, explain the important tradeoffs, and point to evidence. Watching a course or following a tutorial is useful practice, but it is not evidence of independent readiness.

For each unchecked item, write a next action or link an artifact in your own notes. Evidence can be code, tests, a benchmark, a design document, a deployed service, or a clear interview explanation.

## Current Starting Point

The workspace provides evidence of hands-on work with Python, FastAPI, TEI, Qdrant, Redis, Docker Compose, NVD batch ingestion, and OIDC/JWKS-based search authentication. The project has focused auth and route tests. These are useful starting artifacts, not a claim that every topic below is mastered.

The current project still needs measured hybrid retrieval, Kafka ingestion, operational failure testing, and a decision on custom shard keys based on workload benchmarks. Track implementation in [ROADMAP.md](ROADMAP.md).

## Priority 0: Engineering Foundations

These are expected across AI engineering, data engineering, and MLOps internship interviews.

### Python and Software Design

- [ ] Write typed, readable Python modules with clear boundaries and stable interfaces.
- [ ] Use exceptions deliberately; distinguish invalid input, unavailable dependencies, and unexpected failures.
- [ ] Use async I/O appropriately and explain when CPU-bound work belongs in a process, worker, or separate service.
- [ ] Manage configuration and secrets through environment/configuration systems; never commit credentials.
- [ ] Package and run the project from a clean environment using documented commands.
- [ ] Review unfamiliar code, isolate a bug, and add a regression test before changing unrelated behavior.
- [ ] Explain key design choices without relying on a framework to hide the underlying behavior.

**Evidence:** a clean setup, useful type checks or diagnostics, and tests that run from a documented command.

### APIs, Testing, and Data Structures

- [ ] Design explicit request/response schemas, validation rules, status codes, and backwards-compatibility expectations.
- [ ] Test success, invalid input, authorization failure, dependency outage, and timeout behavior.
- [ ] Use unit tests for logic and integration tests for database, API, and queue contracts.
- [ ] Understand arrays, hash maps, sets, stacks, queues, trees, heaps, graphs, and common sorting/searching patterns.
- [ ] Analyze time and space complexity and communicate the tradeoff clearly.
- [ ] Solve common interview problems under time constraints and explain a tested solution.

**Evidence:** a focused API test suite and a reviewed DSA log that records missed cases and patterns, not just problem counts.

### SQL and Data Modeling

- [ ] Write joins, aggregations, subqueries, CTEs, and window functions.
- [ ] Design keys, constraints, indexes, and schemas around access patterns and integrity requirements.
- [ ] Explain transactions, isolation, idempotent writes, and schema migration risks.
- [ ] Inspect a query plan and identify a plausible indexing or query-shape improvement.
- [ ] Distinguish OLTP workloads, analytical workloads, and search/vector workloads.

**Evidence:** a small schema or query exercise with correctness tests and an explanation of the indexing tradeoffs.

## Priority 1: Applied Machine Learning

### ML and Deep Learning Foundations

- [ ] Explain train/validation/test splits, leakage, overfitting, regularization, and class imbalance.
- [ ] Select metrics appropriate to the problem and explain false-positive/false-negative costs.
- [ ] Understand gradient descent, loss functions, embeddings, and the role of optimization in training.
- [ ] Explain neural-network building blocks and the transformer attention mechanism at a useful systems level.
- [ ] Run a baseline before tuning and use controlled experiments rather than changing many variables at once.
- [ ] Track dataset provenance, preprocessing, model version, and experiment configuration.

**Evidence:** a reproducible experiment with a baseline, metric rationale, and recorded configuration. NPTEL/Udacity coursework can support this, but pair it with an implementation or written analysis.

### LLM and Retrieval Systems

- [ ] Explain embedding model choice, vector dimensions, normalization, and model-version compatibility.
- [ ] Explain dense versus sparse retrieval and when exact lexical matching matters.
- [ ] Build a fixed relevance set and evaluate Recall@k plus MRR or nDCG.
- [ ] Compare retrieval variants on the same dataset and queries; report latency and resource cost with quality.
- [ ] Explain chunking, metadata filtering, reranking, context limits, and failure modes in retrieval-augmented generation.
- [ ] Handle model and embedding-service timeouts, bounded retries, and unavailable dependencies.
- [ ] Explain prompt-injection and retrieved-content risks; treat retrieved text as untrusted input.
- [ ] Record the model, prompt/configuration, and retrieval settings needed to reproduce an answer.

**Evidence:** the cybersecurity RAG benchmark in this workspace, with measured dense-only versus hybrid results and documented limitations.

## Priority 1: Data and Distributed Systems

### Batch and Streaming Data Engineering

- [ ] Define a versioned event schema and compatibility policy.
- [ ] Select Kafka partition keys based on ordering, throughput, and hot-key risk.
- [ ] Explain consumer groups, offsets, rebalances, delivery semantics, and replay.
- [ ] Make downstream writes idempotent using stable event/document IDs and version or content hashes.
- [ ] Commit offsets only after durable processing; explain the crash windows this creates.
- [ ] Bound concurrency and buffering; reason about backpressure instead of allowing unbounded queues.
- [ ] Handle transient retries, poison messages, dead-letter paths, and replay safely.
- [ ] Measure consumer lag, throughput, retry rates, and processing failures.

**Evidence:** a failure-tested ingestion flow demonstrating duplicate delivery, crash recovery, replay, and convergence to the same logical state.

### Distributed Systems and Storage

- [ ] Explain replication, quorum, leader election, consistency tradeoffs, and failure domains at a practical level.
- [ ] Distinguish service availability from data durability and understand the limits of a local multi-container test.
- [ ] Design timeouts, retry budgets, circuit breaking, graceful shutdown, and dependency failure behavior.
- [ ] Explain how sharding and partitioning affect routing, skew, hot tenants, and operational overhead.
- [ ] Benchmark before adopting custom shard keys or changing replica/consistency settings.
- [ ] Document backup, restore, migration, and rollback paths.

**Evidence:** a recorded failure exercise and a benchmark or operational note explaining the chosen design and its limitations.

## Priority 1: MLOps, Cloud, and Operations

### Delivery and Runtime

- [ ] Build and run the service reproducibly from a clean checkout.
- [ ] Use CI for tests, lint/type checks, and build validation.
- [ ] Use containers with explicit runtime versions, dependency management, health checks, and resource limits.
- [ ] Separate development configuration from production secrets and deploy settings.
- [ ] Deploy one small service to one cloud provider and explain networking, identity, storage, and cost.
- [ ] Understand logs, metrics, traces, correlation IDs, and alert thresholds.
- [ ] Define liveness and readiness separately and verify behavior during dependency outages.
- [ ] Maintain a short runbook for deploy, rollback, diagnosis, and recovery.

**Evidence:** reproducible CI, one documented deployment, and a runbook tested by someone other than the author when practical.

### Model and Data Lifecycle

- [ ] Version model, embedding, schema, and dataset changes.
- [ ] Design a safe re-embedding or backfill process that can resume after failure.
- [ ] Track data quality and ingestion failures, not only API uptime.
- [ ] Measure model-serving and embedding latency, throughput, and resource use.
- [ ] Explain when local inference, a managed endpoint, or a dedicated serving system is appropriate.
- [ ] Define rollback behavior when a model or embedding change degrades quality.

**Evidence:** a repeatable model/data change with before-and-after evaluation and a documented rollback plan.

## Priority 0: Security and Responsible Data Handling

- [ ] Authenticate users and derive tenant/role claims from a trusted issuer, never from caller-controlled request fields.
- [ ] Validate JWT signature, issuer, audience, expiry, and required claims.
- [ ] Enforce authorization in application logic and retain database-side tenant filters as defense in depth.
- [ ] Test tenant spoofing, role escalation, missing claims, invalid tokens, and cross-tenant access.
- [ ] Restrict trusted proxy headers to the actual proxy boundary.
- [ ] Apply least privilege to service credentials and network exposure.
- [ ] Avoid logging bearer tokens, secrets, or sensitive incident content.
- [ ] Treat prompts and retrieved documents as untrusted; consider prompt injection and sensitive-data leakage.
- [ ] Document data provenance, retention, and any limits on using public or private datasets.

**Evidence:** a short threat model and automated negative security tests. A tenant filter alone is not proof of secure tenant isolation.

## Communication and Career Readiness

- [ ] Explain one architecture diagram from request entry through retrieval, model calls, storage, and failure handling.
- [ ] Defend design decisions with measurements and state what evidence would change the decision.
- [ ] Present one failure you investigated, the root cause, and the regression test or operational guard added.
- [ ] Keep README setup steps accurate and distinguish implemented, tested, and planned capabilities.
- [ ] Write resume bullets around outcomes and measured evidence, not a list of technologies.
- [ ] Maintain a targeted internship tracker and apply before the flagship project is fully complete.
- [ ] Complete at least one mock coding interview and one system-design discussion monthly during recruiting.
- [ ] Use GDG on Campus, IEEE HIT, CSI HIT, hackathons, or workshops to find collaborators and present concrete technical work.
- [ ] Choose one cloud certification based on target role descriptions; verify current exam details before committing time or money.

**Evidence:** a reproducible public project, a concise demo, an architecture explanation, a targeted resume, and consistent application/interview practice.

## Internship-Ready Gate

Treat yourself as ready to apply when you can check these off and demonstrate them in an interview:

- [ ] Build and test a service from a clean checkout without hand-following an undocumented setup.
- [ ] Explain and test a real authentication and tenant-isolation boundary.
- [ ] Show a relevant ML or retrieval metric, baseline, and reproducible evaluation.
- [ ] Explain failure and replay behavior for at least one data pipeline.
- [ ] Describe one deployment, its observability, and a realistic recovery procedure.
- [ ] Walk through the flagship architecture, tradeoffs, and one limitation honestly.
- [ ] Solve and explain common DSA problems at the level expected by the roles being targeted.
- [ ] Maintain current resume, portfolio links, and an active application pipeline.

Do not wait to satisfy every advanced checklist item before applying. The internship-ready gate is the bar for starting and sustaining applications; the remaining items are a growth backlog.

## Monthly Review

At the end of each month, answer:

1. What can I now demonstrate that I could not demonstrate last month?
2. Which checklist item has the highest impact on the next internship interview?
3. What artifact proves progress: test, benchmark, deployment, design note, or presentation?
4. What should I stop or defer to protect the flagship and recruiting time?
5. What feedback from an interview, mentor, or code review changes next month's priorities?
