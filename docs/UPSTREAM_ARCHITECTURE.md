# Auto-Ontology Upstream — Ground-Truth Architecture & Lab Design Implications

> Static source-code analysis of `NVIDIA/auto-ontology` @ `c447003f` (v1.2.0, 2026-11-06) and `NVIDIA/auto-ontology-eval` @ `ec41b2c5` (2026-11-03). File paths and line numbers correspond to these exact commits. **Purpose:** Anchor the lab's experimental design in **actual production code behavior** rather than marketing or README claims.

---

## 0. Ten Source Findings That Reshape the Lab Design

| # | Finding | Code Evidence | Lab Design Implication |
|---|---|---|---|
| **1** | **The `certified` flag has zero effect on query answering.** Nowhere in `retrieval/`, `semantic/`, `utils/`, or `vdb/` is `*_certified` read during retrieval or SQL synthesis. It is used strictly for UI badges and search filtering. | Repo-wide grep; `sql_attributes.py:143` selects the column but never filters or ranks by it | Drop the hypothesis that "toggling certified improves accuracy". Testing a "human-reviewed ontology" requires modifying **content** (`description`, `sample_values`, `sql_attribute`), not merely flipping certification booleans. Certified flag retention is still measured in Suite 4, but strictly as a *data preservation* test rather than a *quality* lever. |
| **2** | **The chat pipeline has no proactive abstention step.** `check_answerable` is merely the MCP tool alias for `/api/question-entity-coverage` — a standalone endpoint that the chat graph never calls. Moreover, `sql_code` and `response` are typed as `NonEmptyStr`, preventing the LLM from returning an empty SQL response. Abstention occurs only when SQL generation fails $3 \times 3$ attempts or exhausts all 7 reconstruction retries. | `models.py:239-248`; `sql_unconstructable.py:40-91`; `entity_coverage/graph.py:26-49` | Split Suite 6 into two distinct evaluations: (a) coverage scores returned by `/api/question-entity-coverage` on unanswerable questions; and (b) actual chat behavior — where we expect the agent to **always emit SQL** (hallucinating queries on out-of-schema questions). This is a major finding. |
| **3** | **NVIDIA's official benchmark harness bypasses the production execution path.** `auto-ontology-eval` imports Python internals directly (`stream_agent_response`), bypassing REST and MCP, and enables `validate_sql_values=True`, `calculation_only=True`, and `shorten_answer=True` — internal knobs that **HTTP routes do not expose**. Furthermore, it pins commit `dd78d068` rather than `main`. | Eval `auto_ontology_sql/app.py:70-76, 349-424`; upstream `text_to_sql_graph.py:225-239` (`validate_sql_values` defaults to off in the worker) | NVIDIA's published benchmark numbers measure a configuration unavailable to production API consumers. The lab introduces **Arm C' = production gateway** (`POST :3000/api/chat/completions` + `x-api-key`) alongside Arm C (harness). The delta between C and C' is an independent methodological contribution. |
| **4** | **Column rename = drop + add, confirmed by NVIDIA's own unit tests.** Tables that already possess a `Term` are **never** re-compiled. Columns added to an already-compiled table never receive a `ColumnAttribute`. Dropped tables leave behind orphaned `Term` records and stale vector embeddings. | `catalog/tests/test_incremental_ingest.py:214-226`; `dal/datasources.py:852-882` (`fetch_all_tables_without_term`); `column_attribute.table_id` is an unconstrained `TEXT` column (`schema.py:497-501`) | Suite 4 vanilla runs for `m01`–`m05` are guaranteed to fail by design. The lab's goal is not to "discover" this known limitation, but to **quantify its downstream impact on query accuracy** and demonstrate how the sidecar patch resolves it. |
| **5** | **No public endpoint exists to trigger re-ingestion.** `PUT /api/connections/{db}` only re-ingests if the schema allowlist changes. The background scheduler is hardcoded to 24 hours (`INGEST_INTERVAL_HOURS` is ignored). On-demand re-ingestion is only possible via the internal unauthenticated `:3002/ingest` route. | `server/connections/service.py:286`; `data_scheduler.py:36`; `ingestion_service/router.py:74-78` | Waiting 10 minutes at stage `T1` is meaningless because no background job runs. Replace `T1` with: document absence of auto-detection (citing code + a single empirical 24h wait test on `m01`). Stage `T2` must explicitly invoke `:3002/ingest`. |
| **6** | **`/semantic/reset` wipes out all certified flags and manual edits.** `delete_semantic_layer` performs an unconditional delete without checking certification flags. Worse, the public reset route (`/api/semantic-compilation/reset`) wipes **every** database in the instance. | `dal/reset.py:66-184`; `server/semantic_compilation/router.py:45-55` | Stage `T4` (full rebuild) destroys 100% of human curation. |
| **7** | **Model export omits `certified`, `synonyms`, `owner`, `tag`, and `zone`.** Furthermore, `foreign_keys`, `joins`, and `semantic_fks` are queried via `SELECT DISTINCT` without an `ORDER BY` clause. | `dal/model_interchange.py:195-251, 568`; `schemas.py:48-159` | To diff ontologies deterministically: export YAML (and sort those 3 relationship lists) **+** call `GET /api/terms`, `/api/terms/{id}/column-attributes`, and `/api/sql-attributes` to capture `certified` flags and synonyms. |
| **8** | **PII detection does not mask or redact values.** It merely attaches a metadata tag. Raw sample values from PII columns are still stored in `catalog_column.sample_values`, injected into LLM prompts, and embedded into both vector indexes. | Grep for `pii` across `semantic/`, `retrieval/`, and `embedding_rows` returns nothing; `pii_detection/service.py:150` | Condense Suite 8 into a sharp, deterministic check: enable `pii_detection_enabled`, re-ingest, and verify whether raw email/phone sample values appear inside `catalog_column.sample_values` and `vdb.semantic_layer` text. |
| **9** | **Zones are no longer enforced as authorization boundaries.** Backend routes pass `zone_ids=None`, and retrieval queries contain zero zone filtering logic. | `frontend/auth/proxy-backend.ts:95-97`; `terms/router.py:81,103` | Remove multi-tenant zone isolation tests from the lab plan and record this finding as an architectural fact. |
| **10** | **Token usage and cost are never logged.** Latency tracking is limited to two timestamps in `frontend.conversation_analytics`. The configured `RERANK` model is never invoked during query answering. Only 1 warm worker subprocess is maintained, causing cold-start penalties under concurrent load. | `schema.prisma:88-102`; `worker.py:5-25, 366` | Place an OpenAI-compatible logging proxy in front of `REASONING_`, `NON_REASONING_`, and `EMBED_ENDPOINT` to measure token consumption. Execute all evaluation queries **sequentially** and tag traffic with header `x-auto-ontology-source: lab`. |

---

## 1. Production Topology

```text
client ──x-api-key──▶ Next.js :3000 (auth, RBAC) ──x-auto-ontology-user-id──▶ FastAPI :3001
                                                                                   │ best-effort HTTP
                                                                                   ▼
                                                                         ingestion-service :3002 (no auth)
                                                                                   │
                                         Postgres 17 + pgvector ◀──────────────────┘
                                         schemas: public (catalog + semantic, Alembic)
                                                  frontend (users, tokens, configurations, Prisma)
                                                  vdb (data_objects_layer, semantic_layer)
```

- **Official deployment target = Helm** (`DEPLOYMENT.md`); Docker Compose runs the identical multi-service stack on a single host. Helm restricts `:3001`, `:3002`, and `:5432` to internal `ClusterIP` services, whereas Docker Compose publishes them to the host.
- **Only `:3000` is public-facing.** `:3001` blindly trusts the `x-auto-ontology-user-id` header without verification (`server/identity.py:127-146`); if the header is omitted, `enforce_edit` checks are bypassed (`owners.py:98-103`). `:3002` has no authentication at all and exposes `/semantic/reset`.
- **Authentication resolution order:** session cookie $\rightarrow$ API token (`x-api-key` or `Bearer auto_ontology_...`) $\rightarrow$ OAuth MCP $\rightarrow$ SSO. API tokens inherit the permissions of their creator. Roles are strictly binary: `admin` or `viewer`.
- **Single-replica ingestion:** `ingestion-service` must run as a single replica because its scheduler runs in-process without leader election.
- **Helm model configuration gap:** The Helm chart only wires `DEFAULT_MODELS_*` and `EMBED_*`; role-specific triplets (`REASONING_*`, `RERANK_*`) and `DB_PROBE_*` flags are omitted from the chart templates, meaning default Helm deployments run every LLM task through a single default model.
- **Vault fallback:** HashiCorp Vault integration activates only when all 4 Vault environment variables are present; if any single variable is missing, credentials silently fall back to plaintext storage in PostgreSQL without raising an error.
- **Feature flags:** Both `semantic_compilation_enabled` and `pii_detection_enabled` default to **`false`**.

### Lab Implications
- Running Docker Compose on Windows is architecturally faithful **provided all lab clients communicate exclusively via `:3000` with `x-api-key`**. The existing `import_dataset.py` calls `:3001` directly with `x-auto-ontology-user-id` and must be refactored; direct calls to `:3002/ingest` are restricted strictly to Suite 4 (where no public API exists) and documented explicitly.
- Provision 1 `admin` user and 1 `viewer` user: execute all question-answering benchmarks with the `viewer` token, and perform certification/export/configuration operations with the `admin` token.
- Hard-pin `EMBED_MODEL` — changing the embedding model requires wiping and re-ingesting the entire vector database.

---

## 2. Ingestion $\rightarrow$ Catalog $\rightarrow$ Compilation Pipeline (Actual Code Path)

### 2.1 Catalog Ingestion (`ingestion_service/ingest.py:run_ingest` L82–164)
1. `catalog/extract.py:create_dataframe` (L80–102): extracts tables, columns, views, query history, PKs, and FKs $\rightarrow$ filters table names against allow/deny regexes.
2. `catalog/write.py:populate_tabular_data` (L34–60): calls `populate_db` (diffs strictly by **object name**, without structural fingerprinting) $\rightarrow$ `populate_fks` (tracks `last_seen` and deletes unseen FKs) $\rightarrow$ `populate_pks` $\rightarrow$ `populate_queries`.
3. **PII Detection** (if enabled): runs regex rules + an LLM classifier over **table/column names and descriptions only** (ignoring sample values). Attaches a `PII` tag when confidence $\ge 0.9$. Performs zero masking or redaction.
4. **Data Objects Vector Layer:** **deletes all existing embeddings** for the database in `vdb.data_objects_layer` and re-embeds from scratch (`vdb/postgres.py:157-186`). Not incremental.
5. Ingestion **does not** automatically trigger semantic compilation. There is no ingestion status endpoint; completion is observable only via container logs and `catalog_database.pulled`.

### 2.2 Re-Ingesting Existing Tables & Columns (`catalog/diff.py:update_diff_from_existing_schema` L76–250)

| Schema / Data Change | Catalog Layer Behavior | Semantic Layer Behavior |
|---|---|---|
| **Add Table** | Inserted into catalog | Compiled into a new `Term` on the next compilation pass |
| **Drop Table** | Cascades delete to `catalog_column` | Leaves an **orphaned `Term`** (`table__term` cascades, but `term` remains), orphaned `column_attribute` rows (`table_id` is unconstrained `TEXT`), and **stale vectors** in `vdb.semantic_layer` |
| **Add Column to Compiled Table** | Inserted into `catalog_column` | **Never** receives a `ColumnAttribute` (table already has a `Term`); repeatedly falls into `find_unlinked_fk_columns` on every pass |
| **Drop Column** | Deleted from `catalog_column` | Orphaned `ColumnAttribute` remains linked to the parent `Term` |
| **Rename Column** | Treated as **Drop + Add** (assigned a new column ID) | Combines the Drop Column and Add Column bugs above; existing descriptions and `certified` flags are **lost** |
| **Retype Column** | Updated in-place (preserves column ID) | `column_attribute.datatype` and `sample_values` remain stale |
| **Modify Column `COMMENT`** | **Ignored** (catalog diff only compares `data_type` and `is_nullable`) | Unchanged |
| **Data Distribution / Enum Drift** | Unchanged | Table profile, `sample_values`, and `is_unique` are **never** refreshed for already-compiled tables |

### 2.3 Semantic Compilation (`semantic/compile.py` $\rightarrow$ `pipeline.py:compile_semantic_layer` L253–366)
- **Table selection:** `fetch_all_tables_without_term` selects **only** tables that do not yet have an associated `Term`. Processed across 3 worker threads and committed in canonical order.
- **Per-table pipeline (`visit_enter.py:process_table` L753):**
  - **Data Profiling:** Executes `SELECT * LIMIT 1000` (without `ORDER BY`, reading heap order) with a 120s timeout. Extracts top-5 frequent values. **Excludes** `date`, `time`, and `uuid` columns from sampling; drops string values longer than 30 characters. Runs a `SELECT DISTINCT` probe on text columns with $< 5$ distinct values in the 1,000-row sample, retaining the full list if total distinct values $\le 25$.
  - **FK Suggester (LLM):** Used exclusively to exclude likely foreign-key columns from becoming `ColumnAttribute` candidates; **does not write relationship edges**. Flags junction/bridge tables.
  - **Term Extractor:** Prompts the LLM to *"prefer ONE Term per table"*, passing at most 40 columns into the prompt. Falls back to a deterministic 1:1 table-to-term mapping if the LLM call fails. Pure junction/bridge tables (consisting entirely of FKs) **receive no `Term`** $\rightarrow$ causing them to be re-processed on **every subsequent compilation pass**.
  - **Vector Embedding (`vdb.semantic_layer`):** Embeds `"Term: <name>. <desc>"` and `"ColumnAttribute: <attr> of Term <term> (<synonyms>). <desc><samples>"`.
- **Global post-table stages (re-executed on every compilation pass across the entire DB):** `resolve_semantic_fks` $\rightarrow$ `suggest_sql_attributes` $\rightarrow$ `build_bridge_tables_sql_attributes`.
- **Semantic FK Resolution (`semantic/semantic_fk.py`):** For each unlinked column, retrieves top $3+3$ k-NN candidates from `ColumnAttribute` records marked `is_unique=True`, asks the LLM to pick a target (or `null`), and if `null`, probes whether **100%** of distinct sample values in the source column exist in the target column. Finally, `prune_disjoint_semantic_fks` deletes any edge whose source and target columns share zero overlapping values. **No numeric confidence score is stored** (strictly binary).
- **SQL Attributes from Query History:** On PostgreSQL, the extractor queries `pg_stat_activity` (capturing only currently active or recently cached sessions within 24h, not a persistent query log). Worse, `_usage_score` **always returns `0`** due to a known bug (`sql_attribute_suggester.py:69-110`), rendering frequency-based ranking non-functional.
- **Compilation History:** Tracked in `public.semantic_compilation_history` (1 row per pass across all databases). If a pass crashes or is stopped, its `status` remains `NULL` indefinitely.

### Lab Implications — Constraints for the Synthetic Data Generator
- **Schema traps must reside within the first 1,000 rows** of every table (since profiling runs `SELECT * LIMIT 1000` without `ORDER BY`). Synthetic data generation must ensure the first 1,000 rows reflect the target distributions.
- Categorical/status columns must use **`text` data types with $\le 25$ distinct values** and string lengths $\le 30$ characters so they are captured in `sample_values`.
- **Semantic FK traps** (implicit joins without physical FK constraints): the source and target columns **must share overlapping values**, and the target column must evaluate to `is_unique=True` within the first 1,000 rows. Otherwise, the edge is pruned or never proposed, invalidating the test case.
- **Date columns never have `sample_values`:** consequently, `ambiguous_date` traps can only be disambiguated via **column names and descriptions** — motivating the inclusion of **Arm B (`schema+metadata`)**.
- **Bridge tables have no `Term`:** queries joining across a many-to-many bridge table specifically exercise the `bridgeTable` `sql_attribute` synthesis path.
- Every re-ingest wipes and re-embeds the entire data objects vector layer, and every compilation pass re-runs the 3 global stages across the entire database — plan LLM token budgets accordingly.

---

## 3. Query Answering Pipeline (`POST /api/chat/completions`)

- Returns `HTTP 409` if the database has zero `Term` records with `source='semantic'` (`router.py:566`), or if a turn is already active on the conversation. Executes inside a subprocess worker pool maintaining **only 1 warm standby worker**.
- **LangGraph Execution Pipeline (`text_to_sql_graph.py:329-612`, `recursion_limit=45`):**
  1. `question_intent` (`NON_REASONING`) $\rightarrow$ classifies intent as `information` / `prediction` / `calculation` (defaults to `calculation`).
  2. `question_extraction` (`NON_REASONING`) $\rightarrow$ extracts candidate entities and subject.
  3. `retrieve_candidates`: runs k-NN searches against `vdb.semantic_layer` — `CustomAnalysis` ($k=3$), `SqlAttribute` ($k=3$), `ColumnAttribute` ($k=3$ for evidence + $k=2$ per entity), and `Term` ($k=1$ for subject). An LLM "intent filter" acts as the reranker; **the configured `RERANK` model is never called here.**
  4. `prepare_candidates`: LLM selects anchor columns, traverses join paths over physical FKs + `semantic_fk` edges, augments with up to 20 tables from `vdb.data_objects_layer`, and applies an LLM table-relevance filter (skipped if $\le 2$ tables).
  5. `construct_sql_from_candidates` (**`REASONING`**): produces structured output `{thought, sql_code, response}` with $3 \times 3$ retry attempts.
  6. `validate_sql_query`: runs `sqlglot` syntax parsing + LLM intent validation; if the validation LLM call errors out, the query is assumed valid.
  7. `execute_sql_query` (default 30s timeout) $\rightarrow$ on execution error, transitions to `reconstruct_sql` (LLM error classification + rewrite) for up to **7 retries**.
  8. On empty result set $\rightarrow$ invokes `check_empty_like_result` and `check_value_repair` (probing actual DB values).
  9. If all retries fail $\rightarrow$ transitions to `unconstructable_sql_response`; re-runs the entire pipeline a second time (`_MAX_FULL_PIPELINE_ATTEMPTS=2`). If the second attempt also fails, it **returns the last SQL query rejected by intent validation** as a silent fallback (`returned_deterministic_fallback`, unmarked in the client response payload).
- Internal flags `validate_sql_values`, `calculation_only`, `shorten_answer`, `sql_examples`, and `value_anchors` are internal payload fields **not exposed by the production HTTP route**.
- The natural-language `response` prose is generated by the LLM **before** the SQL query is executed against the database; no post-execution LLM summarization call occurs.

### What Enters the Final SQL Generation Prompt (`sql_from_semantic.py:207-348`)
- **Included:** custom prompts, evidence, calculation templates, custom analyses (`name`, `description`, `sql`), SQL attributes (`name`, `description`, `expression`, `full_query`, `term`), **term synonyms**, acronyms, selected table schemas (table description, PK, columns with type, nullability, `semantic_fk` target, **column description**, **`sample_values`**, format), join paths, and example SQL from custom analyses.
- **Excluded:** `ColumnAttribute` descriptions (used only during vector retrieval), `Term` descriptions (retrieval only), tags, zones, PII flags, and **all `certified` flags**.

### Classifying SSE Stream Responses
- **True Abstention:** `sql_code == ""` and `sql_response_from_db == null`.
- **Information-Only Response:** Also produces `sql_code == ""`, distinguishable via the SSE `step` event where `node == "information_agent"`.
- **Silent Deterministic Fallback:** `sql_code != ""` accompanied by empty rows; only identifiable via backend `WARNING` logs.

### Lab Implications
- Execute **Arm C' (production gateway)** sequentially with client timeout $\ge 900\text{s}$, header `x-auto-ontology-source: lab-<suite>`, and a fresh conversation per question.
- Extract server-side latency via `GET /api/analytics` (admin token) as `response_timestamp - question_timestamp`, filtered by `source`.
- Estimate ~7–8 `NON_REASONING` calls + 1 `REASONING` call + $3 + N$ `EMBED` calls on the happy path (plus 2 `NON_REASONING` calls per SQL repair iteration).
- In Suite 6 (30 unanswerable questions), measure: (a) coverage scores from `/api/question-entity-coverage`; (b) whether chat returns SQL or abstains; and (c) when it returns SQL, whether that SQL executes without error to produce semantically meaningless results.

---

## 4. Certification, Write APIs, and Model Interchange

- **Certification Columns:** `term.name_certified`, `term.description_certified`, `column_attribute.certified`, `sql_attribute.certified`, and `catalog_*.description_certified`. Term-level rollup status (`certified` / `partial` / `pending`) is computed dynamically (`dal/terms.py:156-239`). There is no approval workflow or audit trail.
- **Write Endpoints (via `:3000`, requiring `catalog:edit` = admin or owner group):** `PATCH /api/terms/{id}`, `PATCH /api/terms/{id}/column-attributes/{attr}` (passing `sample_values` writes directly to `catalog_column`), `PATCH|PUT /api/sql-attributes/{id}`, and `PATCH /api/nodes/{id}`. Only `PUT /api/sql-attributes/{id}` re-parses the underlying SQL expression. There is no `POST` endpoint to create an empty `Term`.
- **Edits that genuinely alter query generation:** modifying `description` or `name` (triggers re-embedding), updating `sample_values` (injected into the SQL prompt), and creating/updating `sql_attribute`, `custom_analysis`, `acronyms`, or `custom_prompts`. Changes take effect immediately on the next query.
- **Model Export (`POST /api/model/export {databases:[], format:"auto_ontology"|"ossie", include_ontology}`):** Returns YAML containing catalog schema, `sample_values`, `foreign_keys`, `joins`, `semantic_fks`, `sql_attributes`, and `custom_analyses`. **Omits** `certified` flags, synonyms, owners, tags, and zones. The 3 relationship lists are returned in non-deterministic order.
- **Model Import:** Defaults to `replace=true` (see open upstream issues #250, #265, #267).
- **Official Verification Protocol (`skills/nvidia-ontology-management/references/publication.md`):** Export YAML + SHA-256 $\rightarrow$ apply edits $\rightarrow$ re-export $\rightarrow$ verify exact IDs/structures/counts $\rightarrow$ run probe questions. **The lab adopts this exact verification protocol** for Suites 1 and 4.

---

## 5. Official Evaluation Harness (`auto-ontology-eval`)

- Pins `auto-ontology-server @ dd78d068` (older than `main`), `nemo-gym == 0.7.0`, and Python 3.13.
- **Runs ingestion and compilation in-process** rather than through HTTP APIs. Expects PostgreSQL dataset layout: `datasets/<db>/{evaluation.json, metadata.json?, custom_analyses.json?, ddl/, data/}`, where the folder name matches `current_database()` and `db_id` (case-sensitive).
- **Control Arm (`schema-only`):** Single LLM call with prompt = question + evidence + **all files in `ddl/` concatenated** (raw DDL without samples or metadata). Extracts SQL via the `bird_sql` regex — notably, inline `--` comments **truncate the remainder of the query** because newlines are collapsed prior to stripping comments.
- **Treatment Arm (`ontology`):** Calls `stream_agent_response` directly in Python with `validate_sql_values=True`, `calculation_only=True`, and `shorten_answer=True`, extracting raw `sql_code`. Receives `metadata.json` descriptions, `value_examples`, and `custom_analyses` that are withheld from the Control arm. Any abstention counts as `no_model_output` against the 5% health gate.
- `evidence` hints are provided to **both** arms.
- **Scorer:** Compares `set(gold_rows) == set(pred_rows)` — ignoring duplicate rows and row ordering, with **zero floating-point tolerance**, and failing any query that returns extra columns. If a gold SQL query fails to execute, the task is silently dropped from both arms. The scorer connects using the first DSN in `CONNECTION_STRINGS` **without setting a read-only transaction or server-side `statement_timeout`**.
- Control (`GYM_MODEL`) and Treatment (`REASONING_MODEL`) models are configured via separate variables and **are not enforced to match**. Temperature and random seeds are unset, running 1 rollout per task.
- **Adding Arm B (`schema+metadata`) to the harness:** Requires ~100–150 lines touching `tasks.py` (`Arm` literal, `SERVER_FOR_ARM`, `_schema_for`), `ddl.py` (`metadata_description_block`), copying `resources_servers/schema_only_sql/` $\rightarrow$ `schema_metadata_sql/`, and updating `main.py` (`RUN_LABEL` and pairwise comparison).

### Asymmetries That Must Be Disclosed When Citing Harness Numbers
1. Control and Treatment models are not automatically locked to the same model endpoint, and Treatment additionally invokes `EMBED` and `NON_REASONING` models.
2. Control makes 1 LLM call with zero DB access; Treatment makes ~19–23 LLM calls and **executes candidate SQL + value probes directly against the live evaluation database** during generation.
3. Treatment receives `metadata.json` descriptions, sample values, and custom analyses that Control never sees.
4. Control's regex SQL extractor truncates queries containing `--` comments, whereas Treatment uses structured output.
5. Treatment abstentions trip the 5% health gate, whereas Control extraction failures fall back to `SELECT 1` and count as ordinary misses.
6. Treatment enables `validate_sql_values` and `calculation_only`, which **cannot be enabled via the production HTTP API**.

---

## 6. Commit Pinning Strategy

| Option | Pros | Cons |
|---|---|---|
| **Pin `dd78d068`** (matching `auto-ontology-eval`) | Runs the upstream evaluation harness out-of-the-box for Suite 2 | ~1 month behind `main`; Suite 4 would evaluate an older codebase |
| **Pin `c447003f`** (current `main`) | Evaluates the latest v1.2.0 architecture and current schema evolution behavior | Harness may require minor adapter fixes (connector naming / payload fields) |

**Decision:** Pin `c447003f` for the live stack (used across Suites 1, 4, 5, 6, and Arm C'). Attempt to run the Suite 2 harness against `c447003f` by updating `pyproject.toml` in the eval repo and running a 5-question smoke test. If incompatible, run Suite 2 (Arm C harness) in an isolated environment pinned to `dd78d068` and explicitly document both commit hashes in `config/freeze.yaml`.

---

## 7. Production-Equivalent Environment Checklist

- [ ] Do not call `:3001` or `:3002` from normal lab clients; route all traffic through `:3000` + `x-api-key`. The sole exception is calling `:3002/ingest`, `:3002/semantic/status`, and `:3002/semantic/reset` inside Suite 4 (since no public routes exist), which must be explicitly noted in the report.
- [ ] Provision 1 `admin` user and 1 `viewer` user; use the `viewer` token for question answering and the `admin` token for certification, export, and analytics.
- [ ] Enable `semantic_compilation_enabled=true` and `pii_detection_enabled=true` via `PUT /api/configurations/...` rather than direct SQL writes.
- [ ] Pin all model triplets (`REASONING`, `NON_REASONING`, `EMBED`, `RERANK`); ensure `EMBED_*` is identical between `ingestion-service` and `auto-ontology`; set `LLM_MAX_INFLIGHT <= 6`.
- [ ] Leave `CONNECTION_STRINGS` **empty** in `.env` and register all database connections via the API (avoiding split-brain precedence where ingestion prefers `.env` while chat prefers UI-registered connections).
- [ ] Enforce a read-only PostgreSQL role (`lab_ro`) for the target synthetic database with `default_transaction_read_only = on` and `statement_timeout = '30s'`.
- [ ] Place an OpenAI-compatible logging proxy in front of model endpoints to record exact prompt and completion token counts.
- [ ] Snapshot the catalog database before every suite: `public.*` (catalog + semantic + `semantic_compilation_history`), `frontend.configurations`, `frontend.conversation_analytics`, and `vdb.*`. Always restore via `pg_restore` prior to each run.
- [ ] Never run `git pull` on upstream repositories once frozen.

---

## 8. Quick Reference: Endpoints Required by the Lab

| Operation | Endpoint / Method | Required Auth |
|---|---|---|
| Test & register DB connection (triggers initial ingest) | `POST /api/connections/test`, `POST /api/connections` | `admin` (`x-api-key`) |
| Re-ingest an existing DB (Suite 4 only) | `POST :3002/ingest {"database": "<db>"}` | **None** (internal service port) |
| Trigger semantic compilation | `POST /api/semantic-compilation/trigger` | `admin` |
| Poll compilation status | `GET /api/semantic-compilation/status` + `GET :3002/semantic/status` + SQL table `semantic_compilation_history` | `viewer` / internal / SQL |
| Full semantic reset for a single DB | `POST :3002/semantic/reset?database_name=<db>` *(Note: public `/api/semantic-compilation/reset` wipes **all** DBs)* | **None** (internal service port) |
| Ask a question (Production Arm C') | `POST /api/chat/completions` (SSE, header `x-auto-ontology-source`) | `viewer` |
| Check entity coverage / answerability | `POST /api/question-entity-coverage {question, max_distance}` | `viewer` |
| Toggle `certified` flags | `PATCH /api/terms/{id}`, `/column-attributes/{attr}`, `/api/sql-attributes/{id}`, `/api/nodes/{id}` | `admin` |
| Export compiled ontology | `POST /api/model/export {"databases": [], "format": "auto_ontology"}` | `admin` |
| Read `certified` flags & synonyms | `GET /api/terms`, `/api/terms/{id}`, `/api/terms/{id}/column-attributes`, `/api/sql-attributes` | `viewer` |
| Retrieve query latency metrics | `GET /api/analytics?limit=500` | `admin` |
| Apply human content curation | `PATCH` `description` / `sample_values`, `POST /api/sql-attributes`, `POST /api/custom-analyses` | `admin` |
