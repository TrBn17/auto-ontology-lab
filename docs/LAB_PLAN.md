# Auto-Ontology Lab — Comprehensive Experimental Plan

> This document provides the detailed operational specification for [`../PLAN.md`](../PLAN.md): evaluation metrics, experimental protocols, and risk mitigations. Read `PLAN.md` first; terms like "suite", "arm", and "T0–T4" used here serve as reference identifiers for the codebase.

> **Revision Note (Post-Upstream Source Audit):** See [`UPSTREAM_ARCHITECTURE.md`](UPSTREAM_ARCHITECTURE.md). Whenever a discrepancy arises, the mandatory adjustments in **Section 0a** below **override** the corresponding sections later in this document.

---

## 0a. Mandatory Adjustments from Upstream Source Analysis

| Original Section | Adjustment | Rationale (Ref: `UPSTREAM_ARCHITECTURE.md`) |
|---|---|---|
| §5.2 — 3 arms (A/B/C) | Add **Arm C' = production gateway** (`POST :3000/api/chat/completions`, `x-api-key`, sequential execution). The headline comparison becomes **A vs. B vs. C'**; Arm C (harness) is retained strictly for parity comparison against NVIDIA's published numbers. | §0.3 — The eval harness enables `validate_sql_values` and `calculation_only`, which are unavailable in production HTTP routes. |
| §5.3 — Running the harness | The eval harness pins `dd78d068`, whereas the lab stack pins `c447003f`. Attempt to bump the harness pin first; if incompatible, run Suite 2 (harness) against its dedicated commit and record both hashes in `freeze.yaml`. | §6 |
| §6.1 — Exporting for diff | Export YAML **+ sort** `foreign_keys`, `joins`, and `semantic_fks` **+ GET** `/api/terms`, `/column-attributes`, and `/api/sql-attributes` to retrieve `certified` flags and synonyms. | §0.7 |
| §7.2 — T1 "wait 10 minutes" | Removed. Replaced with: **T1 = document absence of auto-detection** (the 24h scheduler is hardcoded and no public re-ingest API exists). Run a **single** 24h wait test on `m01` for empirical verification. `T2` = `POST :3002/ingest`. `T3` = `POST /api/semantic-compilation/trigger`. `T4` = `POST :3002/semantic/reset?database_name=`. | §0.5, §8 |
| §7.2 — Certify 3 terms | Keep certification (to measure flag retention), **but add** 3 **content** edits (`description`, `sample_values`, and 1 manual SQL attribute) because only content modifications influence query generation. Measure survival of both categories across `T2`–`T4`. | §0.1, §0.6 |
| §7.1 — Vanilla expectations | Now backed by direct source code evidence for `m01`–`m05` (NVIDIA's own unit tests confirm `rename = drop + add`, and tables with an existing `Term` are skipped during compilation). Record exact `file:line` references in `expected.json`. The lab measures the **downstream impact on query answers**, rather than merely "discovering" the bug. | §2.2 |
| §4.1 — Spec traps | Generator constraints: traps must appear within the **first 1,000 rows**; enums must be `text` with $\le 25$ distinct values and length $\le 30$ characters; `semantic_fk` traps require overlapping values and an `is_unique` target column; date columns **never include sample values** $\rightarrow$ `ambiguous_date` is tested strictly via column names and descriptions. | §2.3 implications |
| §9 Suite 6 | Split into two parts: (a) coverage scores from `/api/question-entity-coverage`; (b) whether chat returns SQL or abstains across 30 unanswerable questions — expected to **always return SQL** because `sql_code` is typed as `NonEmptyStr`. Add metric: "executable but semantically meaningless SQL". | §0.2 |
| §9 Suite 8 | Condense into a single test: enable PII detection $\rightarrow$ verify whether raw sample values of email/phone columns appear in `catalog_column.sample_values` and `vdb.semantic_layer` text. Drop tenant isolation testing (zones are not enforced as authorization boundaries). | §0.8, §0.9 |
| §9 Suite 5 | PostgreSQL query ingestion only reads `pg_stat_activity` (24h window) $\rightarrow$ execute the query workload **immediately before** calling `:3002/ingest`. Explicitly note that `_usage_score` always returns `0`, so frequency-based ranking is non-functional. | §2.3 |
| §9 Suite 7 | Token usage and cost are not logged upstream $\rightarrow$ place a logging proxy in front of `*_ENDPOINT`. Extract latency from `GET /api/analytics` filtered by `source=lab-*`. Execute queries **sequentially** (single warm worker). | §0.10 |
| §11 `import_dataset.py` | Route all calls through `:3000` + `x-api-key`; enable compilation via `PUT /api/configurations/semantic-compilation`; leave `CONNECTION_STRINGS` empty. Restrict `:3002/ingest` usage strictly to `eval/suite4_evolution/`. | §1, §7 |
| §3 Phase 0 | Include `frontend.configurations`, `frontend.conversation_analytics`, and `semantic_compilation_history` in catalog snapshots. Provision 1 viewer user + token, plus a read-only PostgreSQL role for the source DB. | §7 |

---

## 0. Positioning & Research Questions

The lab answers three core questions, ordered by priority:

| # | Research Question | Previously Answered? |
|---|---|---|
| **Q1** | How accurate is the **automatically discovered ontology** (entities / relationships / business terms) compared against a known ground truth? | **No** — public Text-to-SQL benchmarks lack ground-truth ontology manifests. |
| **Q2** | When the underlying database schema evolves, does Auto-Ontology **auto-detect, incrementally update, or require a full rebuild**, and do certified human edits survive? How much does the **lab's sidecar patch** improve resilience? | **NVIDIA acknowledges this gap** (open issues #258, #259, #260). |
| **Q3** | How much does the compiled ontology improve **Text-to-SQL accuracy** — and on which specific question categories — within an education domain? | Answered by NVIDIA on BIRD/BEAVER — the lab **reuses their evaluation harness**, swapping in our domain dataset and adding category-level breakdowns + Arm B/C'. |

All other evaluations (latency/cost, PII leakage, query-log learning) serve as **supplementary appendices** rather than primary findings.

### Two Operating Modes

- **Audit Mode (Vanilla):** Unmodified upstream Auto-Ontology at the pinned commit. Zero intervention.
- **Patch Mode:** Vanilla Auto-Ontology + `smart_evolve.py` running as an external sidecar. Every Patch Mode metric **must** be presented side-by-side with its corresponding Audit Mode baseline.

Never publish Patch Mode numbers without the vanilla baseline.

---

## 1. Methodological Principles (Invariants)

1. **Pin everything:** Commit hash of `NVIDIA/auto-ontology`, commit hash of `NVIDIA/auto-ontology-eval`, model names and versions across all 4 roles (`REASONING`, `NON_REASONING`, `EMBED`, `RERANK`), and sanitized `.env` settings. Record all parameters in `config/freeze.yaml` and cite this file in every generated report.
2. **Baseline first, patch second:** Run Suite 4 in vanilla Audit Mode to completion and persist all artifacts before enabling the sidecar patch.
3. **$\ge 3$ runs** for every evaluation involving an LLM. Report `mean ± std` as well as the number of questions that flip between correct and incorrect across runs.
4. **Executable Gold SQL:** Every gold SQL query must execute cleanly against the synthetic database and return a non-empty result set (except for the `unanswerable` test set). Never accept LLM-generated gold SQL without execution verification.
5. **Mandatory public dataset:** Commit at least one spec-first dataset (`edu-public`) to the repository for full reproducibility. Keep internal datasets (`hahlex-edu`, `language-learning-sandbox`) gitignored.
6. **No manual catalog tampering** during evaluation runs. Every state change must be executed via logged, deterministic scripts.
7. **Deterministic reset:** Before starting any suite, restore the catalog database to snapshot `T0` via `pg_dump`/`pg_restore`. Never chain suites on top of a mutated catalog state.

---

## 2. Target Repository Layout

```text
auto-ontology-lab/
├── config/
│   ├── freeze.yaml              # Commit hashes, model names, container names
│   └── lab.env.example          # Backend/ingestion URLs, PG credentials (no real secrets)
├── spec/
│   └── edu_public.yaml          # GROUND TRUTH: entities, relationships, terms, traps
├── generators/
│   ├── from_spec.py             # spec.yaml → schema.sql + seed.sql + manifest.json
│   ├── source_introspector.py   # (Layer 1) Live DB → profile.json (zero PII copied)
│   └── synthetic_generator.py   # (Layer 2) profile.json → DDL + synthetic data
├── datasets/
│   ├── edu-public/              # COMMITTED — generated from spec, fully reproducible
│   │   ├── schema.sql
│   │   ├── seed.sql
│   │   ├── manifest.json        # Ground-truth ontology (flattened from spec)
│   │   ├── evaluation.json      # auto-ontology-eval compatible question set
│   │   ├── metadata.json        # Column descriptions — used for Arm B ("schema+metadata")
│   │   └── mutations/           # m01..m08 (SQL scripts) + expected.json
│   └── hahlex-edu/              # GITIGNORED — cloned from internal DB
├── questions/
│   ├── templates/               # Question templates organized by category
│   └── build_evaluation.py      # Templates × parameters → evaluation.json + gold SQL validation
├── eval/
│   ├── suite1_discovery/        # Export ontology → diff against manifest → P/R/F1
│   ├── suite2_text2sql/         # Wrapper around auto-ontology-eval + Arm B & Arm C'
│   ├── suite4_evolution/        # Mutation runner implementing the T0–T4 protocol
│   ├── suite5_learning/         # Seed query history → measure synthesized SQL attributes
│   ├── suite6_abstention/       # Entity coverage & chat behavior on unanswerable questions
│   └── common/                  # Catalog snapshot/restore, readiness polling, YAML export
├── patch/
│   └── smart_evolve.py          # (Layer 3) Out-of-process sidecar — enabled only in Patch Mode
├── scripts/
│   └── import_dataset.py        # Existing importer — see §11 for refactoring plan
├── reports/
│   ├── runs/<timestamp>/        # Raw artifacts for each execution run
│   └── RESULTS.md               # Aggregated report generated deterministically by script
└── docs/
    └── LAB_PLAN.md              # This document
```

---

## 3. Phase 0 — Environment Freeze (0.5 Days)

**Objective:** Ensure every metric produced by the lab traces back to a single immutable software and model state.

- [ ] Populate `config/freeze.yaml`: commit hashes of both NVIDIA repositories, all 4 model endpoints/names, container names, and freeze date.
- [ ] Pin `auto-ontology/` via git submodule or `scripts/pin_upstream.sh` to check out the exact commit hash. Do not track floating `main`.
- [ ] Implement `eval/common/snapshot.py`: run `pg_dump` on the catalog database (schemas `public`, `frontend`, `vdb`, including `frontend.configurations`, `frontend.conversation_analytics`, and `semantic_compilation_history`) $\rightarrow$ `snapshots/T0_<dataset>.dump`, alongside a corresponding `restore()` function.
- [ ] Implement `eval/common/readiness.py`: poll `GET /api/semantic-compilation/status` and the `semantic_compilation_history` table until compilation finishes. Shared across all evaluation suites.
- [ ] Verify that the running stack (with `hahlex-edu` imported and compiled) can be snapshotted and restored cleanly.

**Exit Criteria:** `restore()` + `readiness()` completes end-to-end in under 5 minutes, and running 5 smoke-test questions before and after restoration yields identical SQL queries.

---

## 4. Phase 1 — Public Spec-First Dataset `edu-public` (2 Days)

### 4.1 The Specification as Ground Truth

`spec/edu_public.yaml` declares:

```yaml
entities:
  - name: student
    table: std_mst            # Intentionally opaque/abbreviated table name
    description: ...
    key: std_id
relationships:
  - from: enrollment.std_id
    to: student.std_id
    kind: fk                  # fk | semantic_fk (no physical FK constraint) | bridge
terms:
  - name: attendance_rate
    sql: "SUM(CASE WHEN att.status='present' THEN 1 ELSE 0 END)::float / COUNT(*)"
    grain: [student, class]
    depends_on: [attendance, enrollment]
traps:
  - type: ambiguous_date
    columns: [enrollment.enrolled_at, enrollment.created_at, class.start_date]
  - type: soft_delete
    column: student.is_deleted
  - type: abbreviation
    mapping: {std_id: student_id, cls_sess: class_session}
  - type: no_physical_fk
    pairs: [[payment.ref_code, invoice.code]]
```

**Trap design principle:** Every schema trap must be grounded in real-world production schemas (e.g., Moodle, Canvas, or patterns observed in `hahlex-edu`). Document the real-world reference in a `rationale` field to preempt any criticism that the benchmark was artificially engineered to fail.

### 4.2 Dataset Generator

`generators/from_spec.py`:
- Generates `schema.sql` (exact DDL matching the spec, omitting physical `FOREIGN KEY` constraints where `kind: semantic_fk` is specified).
- Generates deterministic `seed.sql` / `data/*.csv` (fixed RNG seed), topologically sorted by FK dependencies, with realistic value distributions (e.g., 15–20% `cancelled` class sessions, 5% soft-deleted students) so value anchoring is meaningful. Ensure all traps and enum distributions are represented within the **first 1,000 rows** of each table.
- Generates `manifest.json`: flattens the specification into three canonical lists (`entities`, `relationships`, `terms`) used as the ground truth for Suite 1.
- Generates `metadata.json` formatted for `auto-ontology-eval` (table/column descriptions + sample values).

### 4.3 Target Scale

- 12–18 tables, 1 bridge table, 1–2 views.
- 20,000–100,000 total rows — large enough for 4–6 table JOINs to return non-empty results, yet compact enough for ingestion + compilation to finish in under 15 minutes.

**Exit Criteria:** `python generators/from_spec.py spec/edu_public.yaml` outputs all 4 artifacts; `import_dataset.py --dataset edu-public` completes ingestion and compilation; snapshot `T0_edu-public.dump` is saved.

---

## 5. Phase 2 — Question Bank & Suite 2 Text-to-SQL (2 Days)

### 5.1 Question Bank Construction

Define parameterized templates in `questions/templates/*.yaml` by category; `build_evaluation.py` compiles them into `evaluation.json`:

| Category | Count | Example Question | Evaluation Objective |
|---|---|---|---|
| `simple` | 30 | How many active students are there? | Baseline sanity check |
| `join_2_3` | 30 | Total revenue by class in Q3 | Basic multi-table JOINs |
| `join_4_6` | 25 | Monthly attendance rate by instructor | Deep join path resolution |
| `business_metric` | 25 | Attendance rate, outstanding tuition, churn | Accurate business formula (`sql_attribute`) usage |
| `ambiguous_date` | 20 | "Students enrolled in October" (`enrolled_at` vs. `created_at`) | Temporal column disambiguation |
| `soft_delete` | 10 | Total student count (must filter `is_deleted = false`) | Implicit filter handling |
| `no_fk_join` | 10 | Join `payment` $\leftrightarrow$ `invoice` via `ref_code` | Implicit `semantic_fk` traversal |
| `unanswerable` | 20 | Questions referencing concepts absent from the schema | Suite 6 (Abstention) |
| `ambiguous_intent` | 10 | "Which class is the best?" | Suite 6 (Abstention / Coverage) |

**Total:** ~180 questions (150 answerable with verified gold SQL, 30 unanswerable/ambiguous). Each record includes `question_id`, `category`, `difficulty`, `evidence` (if applicable), and `SQL`.

**Mandatory validation in `build_evaluation.py`:**
- Execute every gold SQL query against the synthetic DB; fail the build if any query errors out or returns an empty result set (excluding `unanswerable`).
- For `ambiguous_date` questions, include a `wrong_sql_variants` field (queries using the wrong date column) and assert that their execution results **differ** from the gold SQL result. If they produce identical results, the trap is ineffective and the question is rejected.

### 5.2 Evaluation Arms

| Arm | Model Input | Built into `auto-ontology-eval`? |
|---|---|---|
| **A. `schema-only`** | Raw DDL | Yes (Control) |
| **B. `schema+metadata`** | Raw DDL + `metadata.json` column descriptions | **Added by Lab** — isolates the benefit of *column descriptions* from the *ontology graph/agent* |
| **C. `ontology` (Harness)** | Auto-Ontology Python agent (`stream_agent_response`) | Yes (Treatment — uses internal non-production flags) |
| **C'. `gateway` (Production)** | Sequential HTTP calls to `POST :3000/api/chat/completions` | **Added by Lab** — measures true production path |

**Methodological contribution of Arm B & C':** If $\text{Arm B} \approx \text{Arm C'}$, then the accuracy gain stems primarily from column descriptions rather than ontology compilation. Comparing $\text{Arm C}$ vs. $\text{Arm C'}$ quantifies the gap between NVIDIA's benchmark harness and the shipped production gateway.

### 5.3 Execution Protocol

- Wrapper `eval/suite2_text2sql/run.py` orchestrates 3 runs $\times$ each arm (`A`, `B`, `C'`, and optionally `C`), saving raw `*_aggregate_metrics.json` and `*_failures.jsonl` under `reports/runs/<ts>/suite2/`.
- Respect the evaluation harness's health gate (>5% infrastructure/health errors suppresses headline numbers). If triggered, reduce concurrency (`--eval-workers`) rather than bypassing the gate.

### 5.4 Metrics

- **Execution Accuracy (EX):** Overall and broken down **by question category**.
- **Accuracy Deltas:** $\Delta(\text{A}\rightarrow\text{C'})$, $\Delta(\text{A}\rightarrow\text{B})$, $\Delta(\text{B}\rightarrow\text{C'})$, and $\Delta(\text{C}\rightarrow\text{C'})$ — reported as `mean ± std`.
- **Flip Rate:** Percentage of questions whose correctness changes across the 3 runs within the same arm.
- **Failure Taxonomy** (classified from `*_failures.jsonl`): `wrong_join`, `wrong_column`, `wrong_filter`, `wrong_aggregation`, `syntax_error`, `timeout`, `abstained`.

**Exit Criteria:** `reports/RESULTS.md` populated with the 3-arm $\times$ 9-category accuracy table across 3 runs.

---

## 6. Phase 3 — Suite 1 Ontology Discovery (1.5 Days)

### 6.1 Extracting the Compiled Ontology

Combine two complementary extraction methods:
1. `POST /api/model/export` $\rightarrow$ YAML export (public interface; sort `foreign_keys`, `joins`, and `semantic_fks` deterministically).
2. `GET /api/terms`, `/api/terms/{id}/column-attributes`, and `/api/sql-attributes` (or direct read-only queries against `term`, `column_attribute`, `column__semantic_fk`) to capture `certified` flags and synonyms omitted by the YAML exporter.

### 6.2 Matching Rules (Fixed Prior to Evaluation)

| Ontology Element | Match Condition | Notes |
|---|---|---|
| **Entity** | Generated `Term` maps to the exact target `table` in `manifest.json` | Term name may differ; underlying table mapping must match |
| **Relationship** | Column pair `(col_A, col_B)` matches (undirected) | Report physical `fk` and implicit `semantic_fk` separately |
| **Business Term** | Synthesized `sql_attribute`, when executed on the synthetic DB, matches the gold expression across $\ge 95\%$ of grain rows | Evaluated by execution equivalence, not SQL string similarity |

Compute Precision, Recall, and F1 for each category. Report breakdowns for `fk` vs. `semantic_fk` and `clean_name` vs. `abbreviated` tables.

### 6.3 Error Analysis

Catalog all **false-positive relationships** (hallucinated JOIN paths) — this is the most damaging failure mode because it produces SQL queries that execute without syntax errors but return corrupted business numbers. Count how many Suite 2 failures trace directly back to false-positive relationships.

**Exit Criteria:** Precision / Recall / F1 table across all 3 element types $\times$ 2 datasets (`edu-public`, `hahlex-edu`), accompanied by the full list of false-positive relationships.

---

## 7. Phase 4 — Suite 4 Schema Evolution, **Audit Mode** (2 Days)

### 7.1 Mutation Catalog (Mapped to Upstream Issue #258 Acceptance Criteria)

| ID | Schema / Data Mutation | Issue #258 Criterion | Expected Vanilla Behavior (Backed by `UPSTREAM_ARCHITECTURE.md` §2.2) |
|---|---|---|---|
| `m01` | `ALTER TABLE enrollment RENAME COLUMN enrolled_at TO enrollment_date` | Column rename | Treated as drop + add; `Term` loses attribute mapping; certified description is lost |
| `m02` | `ALTER TABLE student ADD COLUMN scholarship_pct numeric` | Column addition | Table already has a `Term` $\rightarrow$ skipped during compilation; new column never gets a `ColumnAttribute` |
| `m03` | `ALTER TABLE class DROP COLUMN legacy_code` | Column drop | Orphaned `ColumnAttribute` remains linked to the `Term` |
| `m04` | `ALTER TABLE payment ALTER COLUMN amount TYPE numeric(12,2)` | Column retype | Catalog updates in-place, but `column_attribute.datatype` and samples remain stale |
| `m05` | `COMMENT ON COLUMN ... IS '...'` | Description update | Ignored by catalog diff (which only checks data type and nullability) |
| `m06` | `ALTER TABLE invoice ADD CONSTRAINT fk_...` (promote a `semantic_fk` pair to a physical FK) | Key update | Duplicate relationship edge or unmerged FK representation |
| `m07` | `UPDATE` data so `attendance.status` introduces a new enum value `'excused'` | Data / enum drift | `sample_values` are never refreshed for already-compiled tables |
| `m08` | `CREATE TABLE` + physical FK referencing `student` | New table addition | Compiled normally on next pass (table has no prior `Term`) — serves as the positive control case |

Each mutation directory includes an `expected.json` file specifying the exact target ontology diff relative to `manifest.json`.

### 7.2 Evaluation Protocol (`T0` $\rightarrow$ `T4`) for Each Mutation

```text
restore(T0)
certify 3 terms + apply 3 content edits (description, sample_values, manual sql_attribute) via API
snapshot(T0_certified)
run 20 probe questions → record baseline answers
apply SQL mutation (m01..m08)
T1: document absence of auto-detection (24h hardcoded scheduler; run 1 empirical 24h check on m01)
T2: POST :3002/ingest (catalog refresh)                      → export, diff, run probes, check 6 edits
T3: POST /api/semantic-compilation/trigger (incremental)     → export, diff, run probes, check 6 edits
T4: POST :3002/semantic/reset?database_name= + full rebuild  → export, diff, run probes, check 6 edits
compare each stage against expected.json; record survival of the 6 certified/manual items
```

### 7.3 Metrics

- **Detection Stage:** At which stage (`T2` / `T3` / `T4` / never) the ontology accurately reflects the mutation.
- **Structural Correctness:** Diff against `expected.json` — classified as `correct`, `missing`, `stale/wrong`, or `new_false_positive`.
- **Update Latency:** Wall-clock time from triggering the stage until `readiness` reports completion.
- **Edit Retention:** How many of the 6 certified/manual edits (`3 certified flags + 3 content edits`) survive at each stage (`x/6`).
- **Probe Regression:** Out of the 20 probe questions, how many regress from `correct` $\rightarrow$ `incorrect` after the mutation.

**Exit Criteria:** Complete $8\text{ mutations} \times (\text{T2, T3, T4})$ evaluation matrix across both datasets. This serves as a **central finding** of the lab.

---

## 8. Phase 5 — Sidecar Patch `smart_evolve.py` + Suite 4 **Patch Mode** (3 Days)

### 8.1 Sidecar Architecture (Out-of-Process, No Upstream Fork)

- Runs outside the Auto-Ontology containers, inspecting the catalog database and calling the ingestion/compilation endpoints. Zero modifications to upstream source code $\rightarrow$ preserves strict reproducibility against the pinned commit.
- **Input:** Pre- and post-mutation schema fingerprints stored in `patch/state/`.
- **Four surgical behaviors** (as specified in `PLAN.md` Step 5):
  1. *Auto-rename migration* (`ordinal_position` + `data_type` + name similarity + sample overlap).
  2. *Surgical column compilation* for new columns added to tables that already possess a `Term`.
  3. *Orphan cleanup* for dropped columns/tables across `column_attribute` and `vdb.semantic_layer`.
  4. *Incremental re-profiling* when categorical/enum columns acquire new distinct values.
- Each behavior is modularized behind an independent CLI flag to support **ablation studies**.
- **Output:** Structured diff report (`created`, `updated`, `retained`, `removed`, `unresolved`) aligned with the requirements of upstream issue #258.

### 8.2 Methodological Risks to Disclose in the Report

- Writing directly to `vdb.*` and `term` tables is inherently version-coupled. The sidecar patch explicitly claims compatibility **only** with the pinned upstream commit (`c447003f`).
- Heuristic rename detection can produce false positives when a column is dropped and a different column of the same type is added at the same ordinal position. We measure **rename precision** independently across a dedicated micro-benchmark: 10 true column renames + 5 drop-then-add-unrelated-column cases.

### 8.3 Execution

Re-run the full protocol from §7.2 with `--patch` enabled. Perform an ablation study by toggling individual patch behaviors on `m01`, `m02`, and `m03`.

**Exit Criteria:** Side-by-side **Vanilla vs. Patch** comparison matrix across all 8 mutations; ablation table; rename precision/recall breakdown.

---

## 9. Phase 6 — Supplementary Suites (1.5 Days)

### Suite 6 — Abstention & Unanswerable Queries
- Run the 30 `unanswerable` + `ambiguous_intent` questions through both `POST /api/question-entity-coverage` and `POST /api/chat/completions`.
- **Metrics:** Entity coverage score distribution; chat abstention precision and recall; rate of "executable but semantically meaningless SQL" hallucinated on out-of-schema questions; and **false abstention rate** across the 150 answerable questions.

### Suite 5 — Continuous Learning from Query History (Scoped Down)
- Execute 50 analytical SQL queries against the synthetic PostgreSQL database immediately before triggering `POST :3002/ingest` (so they appear in `pg_stat_activity`), run semantic compilation, count newly generated `sql_attribute` records, and measure the resulting accuracy delta on the `business_metric` question category.

### Suite 3 — Merged into Suite 2
`ambiguous_date`, `business_metric`, and `join_4_6` are evaluated directly as categories within Suite 2 rather than as a standalone suite.

### Suites 7 & 8 — Appendices
- **Suite 7 (Latency & Token Cost):** Extract P50/P95 latency from `GET /api/analytics` and token counts from the LLM logging proxy during Suite 2 runs. Summarize in a single table.
- **Suite 8 (PII Masking Verification):** Enable `pii_detection_enabled`, re-ingest, and check whether raw email/phone sample values remain exposed in `catalog_column.sample_values` and `vdb.semantic_layer`.

---

## 10. Phase 7 — Automated Reporting & Publication (1.5 Days)

### Automated `reports/RESULTS.md` Contents
1. **Environment Freeze Table:** Commit hashes, model endpoints, and runtime flags.
2. **Suite 2 (Text-to-SQL):** Arms A, B, C' (and C) $\times$ question category across 2 datasets (`mean ± std`, flip rate).
3. **Suite 1 (Ontology Discovery):** Precision / Recall / F1 + false-positive relationship catalog.
4. **Suite 4 (Schema Evolution):** Vanilla audit matrix, Patch mode matrix, ablation study, and rename precision.
5. **Suite 6 (Abstention):** Coverage vs. chat hallucination rates on unanswerable questions.
6. **Appendices (Suites 5, 7, 8):** Query-log learning, latency/token cost, and PII exposure verification.
7. **Complete Failure Log:** Every failed question alongside its gold SQL, predicted SQL, and failure classification for independent verification.

### Public Repository Deliverables
- Commit `spec/`, `datasets/edu-public/`, `questions/`, `eval/`, `patch/`, `config/freeze.yaml`, and `reports/RESULTS.md`.
- `hahlex-edu` appears strictly as aggregated metrics in `RESULTS.md` — zero schema definitions or data files published.

---

## 11. Refactoring `scripts/import_dataset.py` (Phase 0–1)

The existing `scripts/import_dataset.py` works for initial bootstrapping but requires the following updates for production-accurate benchmarking:

| Current Implementation | Target Refactored Implementation |
|---|---|
| Hardcodes container names `postgres`, `auto-ontology` | Reads container and endpoint configuration from `config/freeze.yaml` |
| Writes directly to `frontend.configurations` via SQL to enable compilation | Calls `PUT :3000/api/configurations/semantic-compilation` using an admin `x-api-key` |
| Calls `:3001` directly with `x-auto-ontology-user-id` | Routes all requests through the `:3000` production gateway with `x-api-key` |
| `ensure_clean_env` mutates `.env` and runs `docker compose up` | Extracted into a standalone `lab doctor` command so dataset import has zero environment side effects |
| Polls `vdb.data_objects_layer` with a fixed 60s sleep | Uses the shared `eval/common/readiness.py` poller (`GET /api/semantic-compilation/status` + `semantic_compilation_history`) |
| No structured run logging | Writes `reports/runs/<ts>/import.json` recording dataset name, target DB, ingestion duration, and compilation duration |

---

## 12. Risk Register & Mitigations

| Risk | Mitigation |
|---|---|
| Upstream changes `vdb.*` schema mid-evaluation | Commit hash is pinned in `freeze.yaml`; `git pull` is prohibited during the lab lifecycle |
| Suite 2 accuracy delta falls within LLM variance noise | Run 3 iterations and report `mean ± std`; if $\text{std} > \Delta$, report "statistically indistinguishable" — which is itself a valid finding |
| NIM rate limits trigger the >5% health gate | Reduce worker concurrency or run overnight; never disable the health gate |
| Synthetic traps criticized as "contrived" | Every trap in `spec/edu_public.yaml` includes a `rationale` citing real-world LMS schemas (Moodle, Canvas, `hahlex-edu`) |
| Sidecar patch misidentifies column renames | Evaluate rename precision separately on a 15-case micro-benchmark (§8.2) and report false positives transparently |
| Scope creep into Suites 7 & 8 | Strictly capped at a single appendix table each |
| Accidental leakage of internal dataset | `datasets/hahlex-edu/` is gitignored; only aggregate metrics appear in `RESULTS.md`; audit `git status` before pushing |

---

## 13. 3-Week Execution Schedule

| Week | Phases | Verifiable Deliverables |
|---|---|---|
| **Week 1** | Phases 0, 1, 2 | `freeze.yaml` locked; `edu-public` generated & imported; initial 3-arm numbers for Suite 2 |
| **Week 2** | Phases 3, 4 | Suite 1 P/R/F1 tables; complete Suite 4 vanilla evolution matrix |
| **Week 3** | Phases 5, 6, 7 | `smart_evolve.py` sidecar + Patch mode matrix; Suite 6 abstention; `RESULTS.md` generated |

**Contingency cut order:** If behind schedule, drop Suite 5 first, then the patch ablation breakdown, and finally the secondary internal dataset runs (preserving `edu-public`). **Never cut the vanilla baseline.**

---

## 14. Immediate Next Actions (Ordered)

1. Generate `config/freeze.yaml` from the currently running stack (`git -C auto-ontology rev-parse HEAD` and active model endpoints in `.env`).
2. Implement `eval/common/snapshot.py` + `readiness.py` and verify snapshot/restore on the existing `hahlex-edu` database.
3. Draft `spec/edu_public.yaml` — start with 8 core tables and 4 schema traps, then expand to ~15 tables once the pipeline runs end-to-end.
4. Implement a minimal `generators/from_spec.py` (DDL + seed + manifest), import into Auto-Ontology, compile, and capture snapshot `T0`.
5. Author 30 `simple` + `join_2_3` questions first and execute a single smoke run of Suite 2 across Arms A and C' to validate the end-to-end evaluation harness before expanding to all 180 questions.
