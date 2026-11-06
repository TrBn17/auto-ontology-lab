# Step 1 — `lab clone`: From Production DB to Privacy-Preserving Synthetic DB

> **Objective:** A single CLI command that connects to any PostgreSQL database using a read-only role and produces a synthetic database with an identical schema, matching statistical distributions, and zero real production rows. PostgreSQL-only in v1.

```powershell
python -m lab clone --from "postgresql://ro_user:***@prod-host:5432/lms" `
                    --to   "postgresql://postgres:***@localhost:5432/lms_synth" `
                    --out  datasets/lms_synth --scale 0.1 --seed 42
```

**Result:** `datasets/lms_synth/` contains sanitized DDL, synthetic CSV/SQL data, a structural ground-truth manifest, and a statistical validation report. The target database `lms_synth` is created, populated, and ready for `import_dataset.py`.

---

## 1. Data Privacy Boundary — What Leaves Production

`lab clone` executes **only 3 categories** of read queries against the production database:

| Category | SQL / Command Executed | Data Leaving Production |
|---|---|---|
| **DDL** | `pg_dump --schema-only` (or fallback introspection via `pg_catalog`) | Table/column names, data types, PK/FK/CHECK/UNIQUE constraints, views, and enum definitions |
| **Aggregates** | `SELECT count(*)`, `count(*) FILTER (WHERE c IS NULL)`, `count(DISTINCT c)`, `min(c)`, `max(c)`, `avg(length(c))` | Summary statistics only |
| **Categorical Distributions** | `SELECT c, count(*) FROM t GROUP BY c` — executed **only** when `count(DISTINCT c) <= 50` **and** the column name does not match the PII regex **and** `max(length(c)) <= 64` | Categorical value list + relative frequencies (e.g., `status: active 70%, dropped 25%`) |

There is **no** `SELECT *`, **no** `LIMIT n` row sampling, and **no** raw record extraction. All extracted metadata is written to `profile.json`, which the operator can inspect and audit **before** synthetic generation begins (`--profile-only` halts right after creating `profile.json`).

- **PII Columns** (matched via column-name regex: `email|phone|mobile|name|address|street|dob|birth|ssn|tax|passport|iban|card`, plus custom `--pii-extra` patterns): only data type, null rate, and min/max character length are collected. Min/max values and `GROUP BY` frequency distributions are **never** queried on PII columns.
- **Excluded Plumbing Tables** (filtered out by default): `schema_migrations|alembic_version|flyway_|audit_|_log$|sessions?$|outbox|celery_|django_migrations|pg_`. Additional exclusions can be passed via `--exclude`.
- **Large Tables** (`count > 1,000,000`): aggregate queries run with `TABLESAMPLE SYSTEM (1)` to avoid taxing the production database. Every query enforces `SET statement_timeout = '60s'`.

---

## 2. Module Architecture

```text
lab/
├── __main__.py        # Entrypoint: python -m lab <cmd>
├── cli.py             # argparse subcommands: clone | profile | generate | load | validate
├── introspect.py      # Production DB → profile.json
├── ddl.py             # pg_dump wrapper + sanitizer + pg_catalog fallback
├── pii.py             # PII column regex detection + Faker provider mapping
├── plan.py            # Topological FK sort + target row count calculation by --scale
├── generate.py        # profile.json → deterministic synthetic rows (column-level generators)
├── writers.py         # Writes data/*.csv, seed.sql, and manifest.json
├── load.py            # Creates target DB, applies DDL, runs COPY from CSV, resets sequences
├── validate.py        # Re-profiles target DB, compares against profile.json → validate_report.json
└── tests/
    ├── fixtures/mini_lms.sql   # 4-table test fixture: student, course, enrollment, payment
    └── test_*.py
```

**Dependencies:** `psycopg[binary]`, `faker`, `numpy` (no heavy frameworks; Python 3.12). `pg_dump` is invoked via the stack's PostgreSQL container (`docker exec postgres pg_dump`) since Windows hosts rarely have `pg_dump` on `PATH`; if Docker is unavailable, `ddl.py` falls back to reconstructing DDL directly from `pg_catalog`.

---

## 3. `profile.json` — The Data Contract

```json
{
  "source": {
    "dialect": "postgresql",
    "server_version": "16.3",
    "profiled_at": "2026-11-10T09:00:00+07:00",
    "scale_note": "TABLESAMPLE 1% on tables > 1M rows"
  },
  "excluded_tables": ["public.schema_migrations"],
  "enums": {
    "public.enrollment_status": ["active", "dropped", "paused"]
  },
  "tables": {
    "public.student": {
      "row_count": 48213,
      "pk": ["std_id"],
      "unique": [["email"]],
      "checks": ["CHECK ((age >= 5))"],
      "columns": {
        "std_id":      {"type": "integer", "nullable": false, "null_rate": 0.0, "distinct": 48213, "min": 1, "max": 51002, "is_identity": true},
        "email":       {"type": "character varying(255)", "nullable": false, "null_rate": 0.0, "pii": "email", "len": {"min": 9, "max": 48, "avg": 23.1}},
        "status":      {"type": "text", "nullable": false, "null_rate": 0.0, "distinct": 3,
                        "categorical": {"active": 0.71, "dropped": 0.24, "paused": 0.05}},
        "enrolled_at": {"type": "date", "nullable": true, "null_rate": 0.02, "min": "2019-01-07", "max": "2026-11-01"},
        "is_deleted":  {"type": "boolean", "nullable": false, "categorical": {"false": 0.95, "true": 0.05}},
        "notes":       {"type": "text", "nullable": true, "null_rate": 0.81, "len": {"min": 0, "max": 2000, "avg": 140.0}}
      }
    }
  },
  "foreign_keys": [
    {
      "from": "public.enrollment.std_id",
      "to": "public.student.std_id",
      "null_rate": 0.0,
      "children_per_parent": {"avg": 3.2, "max": 11},
      "coverage": 0.88
    }
  ],
  "views": ["public.v_active_students"]
}
```

`coverage` denotes the fraction of parent rows referenced by at least one child row (ensuring realistic generation of parent entities—such as newly registered students—that have zero enrollments).

---

## 4. Synthetic Generation Rules — `generate.py`

**Generation Order:** Tables are topologically sorted by foreign key dependencies (`plan.py`); self-referential FKs (`parent_id`) are populated in a second pass after primary keys exist. Target row count per root table is `max(50, round(row_count * scale))`, capped at `--max-rows` (default `50,000`). Child table row counts are scaled according to `children_per_parent` so parent-to-child cardinality ratios remain faithful to production.

| Profile Signal | Generator Strategy |
|---|---|
| `pii` | Mapped to the corresponding `Faker` provider: `email -> email()`, `phone -> phone_number()`, `name -> name()`, `address -> address()`, `dob -> date_of_birth()`, fallback $\rightarrow$ `pystr(len)`. Default locale is `vi_VN` (configurable via `--locale`). |
| `categorical` | `numpy.random.choice(values, p=weights)` — preserves exact empirical frequencies and naturally satisfies the $\le 25$ distinct threshold required by Auto-Ontology's value sampler. |
| `enum` type | Sampled like `categorical`; if no empirical frequency map exists, falls back to a uniform distribution over `pg_enum` labels. |
| `boolean` | Sampled via its `categorical` true/false ratio. |
| `integer` / `bigint` / `numeric` (non-PK/FK) | Uniform over `[min, max]`; if `distinct <= 50`, treated as `categorical`. |
| `is_identity` / `serial` PK | Sequential integers `1..n`; database sequences are updated via `setval` after loading. |
| `uuid` PK | Deterministically seeded `uuid4`. |
| **Foreign Key (FK)** | Sampled from the parent key pool using a Poisson distribution centered on `children_per_parent.avg` (clamped at `max`); preserves `null_rate`; respects `coverage < 1` by leaving `(1 - coverage)` of parent keys unreferenced. |
| `date` / `timestamp` | Uniform over `[min, max]`; when paired temporal columns exist on the same table (`start_*/end_*`, or `*_at` alongside `created_at`), enforces chronological ordering (`end >= start`). |
| Free-form `text` (non-categorical, non-PII) | `faker.text()` truncated to `len.avg ± std`, preserving `null_rate`. |
| `character varying(n)` | Truncated strictly at $n$ characters. |
| `jsonb` | Empty object `{}` in v1; v2 will introspect top-level keys via `jsonb_object_keys` aggregates. |
| `array` | 0–3 elements generated using the base element type's generator. |
| `UNIQUE` constraint | Tracked via an in-memory set; collisions trigger up to 20 regeneration attempts before appending a deterministic disambiguation suffix. |
| Simple `CHECK` (`IN (...)`, `>= x`, `BETWEEN`) | Parsed via regex and enforced during generation; complex multi-column `CHECK` constraints fall back to load-time filtering (see §6). |

All random number generators (`numpy` and `Faker.seed_instance`) are seeded from `--seed`. Identical `profile.json` + `--seed` inputs guarantee **byte-identical output**.

---

## 5. Generated Artifacts — `writers.py`

```text
datasets/lms_synth/
├── profile.json            # Aggregate statistical profile extracted from production
├── ddl/
│   ├── 01_types.sql        # CREATE TYPE / extensions
│   ├── 02_tables.sql       # Tables + PK + CHECK + UNIQUE constraints (prior to FKs)
│   ├── 03_fkeys.sql        # ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY
│   └── 04_views.sql        # Views
├── data/
│   └── public.student.csv  # CSV with header, NULL represented as \N (auto-ontology-eval compatible)
├── seed.sql                # Multi-row INSERT statements (1,000 rows/batch) for import_dataset.py
├── manifest.json           # Structural ground truth (entities = tables, relationships = FKs; semantic section left blank for human annotation)
└── validate_report.json    # Generated after target DB loading & validation
```

**DDL Sanitization:** Strips `OWNER TO`, `GRANT`/`REVOKE`, `ALTER ... SET TABLESPACE`, `CREATE POLICY` (RLS), `\restrict`, and `pg_dump` header comments. Preserves `COMMENT ON` statements. Rewrites the database name in any `\connect` directives.

Both `data/*.csv` and `seed.sql` contain identical records — `data/` serves the `auto-ontology-eval` harness directly, while `seed.sql` maintains backward compatibility with the current `import_dataset.py` until Step 2 refactoring is complete.

---

## 6. Target Database Loader — `load.py`

1. `CREATE DATABASE` (or `DROP DATABASE ... WITH (FORCE)` when `--drop` is passed).
2. Execute `01_types.sql` and `02_tables.sql`.
3. Stream `COPY ... FROM STDIN (FORMAT csv, HEADER, NULL '\N')` for each table in topological order.
4. **Complex `CHECK` constraint fallback:** Each table loads within a transaction; if a batch fails due to an unparsed multi-column `CHECK` constraint, the loader falls back to 1,000-row sub-batches, logs offending rows to `rejected/<table>.csv`, skips them, and records the rejection rate.
5. Execute `03_fkeys.sql` and `04_views.sql`.
6. Advance all sequences via `setval` to `max(id) + 1`.
7. Run `ANALYZE` across the database.
8. Provision a read-only PostgreSQL role `lab_ro` (`GRANT SELECT`, `ALTER ROLE lab_ro SET default_transaction_read_only = on`, `SET statement_timeout = '30s'`) — this read-only DSN is passed to Auto-Ontology and the evaluation scorer.

---

## 7. Statistical & Structural Validation — `validate.py`

Re-runs `introspect` against the newly populated synthetic database (without `TABLESAMPLE`) and compares the results against the source `profile.json`:

| Validation Metric | Pass Threshold |
|---|---|
| **FK Referential Integrity** | 100% (0 orphaned foreign keys) |
| **UNIQUE / PK Violations** | 0 |
| **Scaled Row Count per Table** | Within $\pm 5\%$ of the target scaled row count |
| **Column Null Rate** | Within $\pm 3$ percentage points ($\pm 0.03$) of source |
| **Categorical Distribution ($L_1$ Distance)** | $\sum_v |p_{\text{prod}}(v) - p_{\text{synth}}(v)| \le 0.10$ |
| **Categorical Distinct Value Count** | Exact match |
| **Rejected Row Rate** | $\le 1\%$ per table |

Prints a summary table to the console, writes `validate_report.json`, and exits with a non-zero status code if any assertion fails. Passing `validate.py` is the **completion gate** for Step 1.

---

## 8. Implementation Order & Effort Estimates

| # | Task | Estimate | Definition of Done |
|---|---|---|---|
| 1 | `tests/fixtures/mini_lms.sql` — 4-table fixture with FKs, enum, unique, CHECK, nullable FK, PII column, categorical column, and paired date columns | 1h | Loads cleanly into local PostgreSQL |
| 2 | `introspect.py` + `pii.py` $\rightarrow$ `profile.json` on `mini_lms` | 3h | Output matches §3 schema; unit test verifies `email` has no `categorical` key |
| 3 | `ddl.py` — `pg_dump` via `docker exec` + sanitizer + `pg_catalog` fallback | 2h | Sanitized DDL applies cleanly to an empty database |
| 4 | `plan.py` + `generate.py` — implement all §4 generators (excluding complex jsonb/array) | 5h | Generates `mini_lms` at `scale=1.0` with zero FK/UNIQUE errors |
| 5 | `writers.py` + `load.py` | 3h | Target DB populated with all tables and sequences properly reset |
| 6 | `validate.py` | 2h | All validation assertions pass on `mini_lms` |
| 7 | `cli.py` wiring up `clone`, `--profile-only`, and `--dry-run` | 1h | Single-line `python -m lab clone` runs end-to-end |
| 8 | **Dry run on live `hahlex-edu` (read-only):** manually inspect `profile.json` and tune PII/exclusion regexes | 2h | `validate` passes; manual audit confirms zero raw production values in `profile.json` |
| 9 | Run `import_dataset.py --dataset lms_synth` $\rightarrow$ Auto-Ontology ingest + compile $\rightarrow$ export YAML | 1h | First compiled ontology YAML exported |

**Total estimated effort:** ~2.5 working days. Task 8 is the critical milestone — tasks 1–7 exist to make Task 8 safe and deterministic.

---

## 9. Step-Specific Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Aggregate queries slow down production | Automatic `TABLESAMPLE SYSTEM (1)` on tables $> 1\text{M}$ rows, `statement_timeout = 60s`, off-peak execution, and `--tables` filter |
| PII regex misses an unconventionally named column (e.g., `contact_info`) | Mandatory manual review via `--profile-only` on first run; `--pii-extra` flag; free-text columns with `distinct > 50` are never queried with `GROUP BY` |
| Categorical values themselves are sensitive (e.g., internal branch names) | `max(length) <= 64` guard + `--no-categorical <col>` override; print all extracted categorical columns to console for human review |
| Complex multi-column `CHECK` constraints reject many synthetic rows | Sub-batch fallback isolates rejected rows to `rejected/<table>.csv` and reports the percentage; v2 can add custom constraint solvers |
| `pg_dump` client/server version mismatch | Uses `pg_dump` 17 inside the stack's PostgreSQL container (backward-compatible with PG 12–17) |
| Circular foreign keys ($A \rightarrow B \rightarrow A$) | Topological sort detects cycles, inserts $A$ with `NULL` FK first, and issues an `UPDATE` pass after $B$ is populated |
| Skewed numeric distributions (uniform $\ne$ real production skew) | Acceptable in v1 because Auto-Ontology only inspects `min`, `max`, and top-5 frequent values rather than numeric histograms; v2 can add percentile buckets |

---

## 10. Out of Scope for Step 1

- Non-PostgreSQL dialects (MySQL, SQLite, Snowflake) — PostgreSQL-only in v1.
- Incremental production $\rightarrow$ synthetic synchronization (full regeneration is fast and deterministic).
- Populating the `semantic` section of `manifest.json` (implicit relationships and business metrics are annotated manually in Step 3).
- Question bank and gold SQL generation (covered in Step 4).
- Schema evolution mutations (covered in Step 5 `lab evolve`).
