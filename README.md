<div align="center">
  <img src="https://img3.pixhost.to/images/6977/800059020_image.png" alt="Auto-Ontology Lab Banner" width="100%" />

  <br />

  <h1>🧪 Auto-Ontology Lab</h1>

  <p>
    <img src="https://img.shields.io/badge/PostgreSQL-16%2B-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL 16+" />
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
    <img src="https://img.shields.io/badge/Synthetic_Data-Privacy_Preserving-F59E0B?style=for-the-badge" alt="Synthetic Data" />
    <img src="https://img.shields.io/badge/Auto--Ontology-Semantic_Compiler-10B981?style=for-the-badge" alt="Auto-Ontology" />
  </p>
</div>

---

An independent, reproducible evaluation of [NVIDIA Auto-Ontology](https://github.com/NVIDIA/auto-ontology) at a pinned commit (`c447003f`): does the ontology it builds find the schema, answer questions correctly, survive schema change, and handle questions and data it should not touch?

## Results

Full tables, every failed question with its SQL, and the freeze are in [`reports/RESULTS.md`](reports/RESULTS.md). Screenshots of the runs are in [`reports/evidence/`](reports/evidence/). Dataset: `edu-public`, a synthetic 15-table LMS database.

| Suite | Result |
|---|---|
| `discover` | 18/18 foreign keys, 0 invented relationships; a term on 14/15 tables, two of them with the wrong meaning; the one undeclared join missed |
| `ask` (25 questions × 3 runs) | Agent 73% (95% CI 57–88) vs 72% for a schema-only prompt and 87% with column comments; about 8× slower per answer |
| `evolve` (8 schema changes) | Re-ingest and compile keep 6/6 human edits but miss 3–4 of 8 changes; reset picks up 7/8 and keeps 1/6 edits, every time |
| `audit` | 0 of 20 unanswerable questions declined; raw emails and phones stay in the catalog with PII detection on |

Running it needed a request-format proxy (upstream issue #440) and a non-default model; both are disclosed in the report. See [`docs/FINDINGS.md`](docs/FINDINGS.md).

## Documentation

- [`PLAN.md`](PLAN.md): the three steps (prepare, measure, report), decisions and milestones.
- [`docs/FINDINGS.md`](docs/FINDINGS.md): upstream behaviour found while running the lab.
- [`docs/UPSTREAM_ARCHITECTURE.md`](docs/UPSTREAM_ARCHITECTURE.md): upstream architecture from source, with `file:line` references.
- [`docs/STEP1_CLONE_PLAN.md`](docs/STEP1_CLONE_PLAN.md): the `lab clone` spec and acceptance checks A1–A13.

## Quick start

```powershell
git clone https://github.com/NVIDIA/auto-ontology.git; git -C auto-ontology checkout c447003f   # the pinned stack
cp .env.example .env                                   # add your own keys; config/freeze.yaml holds the models used here
docker compose -f auto-ontology/docker-compose.yml -f config/compose.llm-proxy.yml up -d   # stack + request-format proxy (see the file's header)
python -m lab doctor
python -m lab import --dataset edu-public
python -m lab measure --dataset edu-public            # discover, ask, evolve, audit
python -m lab report --dataset edu-public             # writes reports/RESULTS.md
```

---

## 🚀 Step 1: `lab clone` — Privacy-Preserving Synthetic DB Generator

`python -m lab` clones a production PostgreSQL schema and statistical shape into a 100% synthetic PostgreSQL database (`lab_synth`) and dataset directory (`datasets/<id>/`) without leaking any free-text or PII values from the source database.

### 1. Prerequisites

```bash
pip install "psycopg[binary]>=3.1" "numpy>=1.26" "faker>=24.0" "pytest>=8.0"
```

 Ensure the local Docker stack (`postgres` container on port `5432`) is running if `pg_dump` is not installed on the host.

> [!IMPORTANT]
> **Password Security (§1.4)**: Never pass a database password on the command line (`sys.argv`). `python -m lab` inspects `sys.argv` on startup and immediately aborts with exit code `1` if any argument contains a password in a URL (`postgresql://user:pass@host/db`) or key-value (`password=...`) format. Always pass credentials via environment variables using `--from-env` and `--to-env`.

---

### 2. Recommended Workflow

#### Step 2.1: Profile Only & Review Categorical Candidates (`--profile-only`)

Connect to the source database using a read-only role (every source connection enforces `SET default_transaction_read_only = on; SET statement_timeout = '60s';`) and generate `profile.json` + sanitized `ddl/` files:

```powershell
$env:SRC_DSN = "postgresql://ro_user:secret@prod-host:5432/source_db"

python -m lab clone `
  --from-env SRC_DSN `
  --out datasets/my_synth `
  --profile-only
```

This command:
1. Introspects `pg_catalog` and `pg_stats` (`null_frac`, `n_distinct`), numeric/date bounds, character lengths, enum labels, `CHECK` constraints, PK/UNIQUE/partial-unique indexes, and FK cardinalities.
2. Automatically skips system/migration tables (`_prisma_migrations`, `schema_migrations`, `alembic_version`, `flyway_schema_history`, `django_migrations`, `knex_migrations`, plus any `--exclude <regex>`).
3. Prints two tables to stdout:
   - **Allowlist Candidates**: Low-cardinality (`1 <= n_distinct <= 50`) text/varchar columns not matched by the PII column-name filter.
   - **Vetoed Candidates (Blocked by PII Filter)**: Low-cardinality columns whose names match the PII pattern (`email|phone|mobile|name|address|street|dob|birth|ssn|tax|passport|iban|card` plus `--pii-extra`).

Inspect `datasets/my_synth/profile.json` and choose which non-PII categorical columns should preserve their exact value frequencies via `--allow-categorical`.

#### Step 2.2: Full Clone (`profile` $\rightarrow$ `generate` $\rightarrow$ `load` $\rightarrow$ `validate`)

```powershell
$env:SRC_DSN = "postgresql://ro_user:secret@prod-host:5432/source_db"
$env:TGT_DSN = "postgresql://postgres:secret@localhost:5432/my_synth_db"

python -m lab clone `
  --from-env SRC_DSN `
  --to-env TGT_DSN `
  --out datasets/my_synth `
  --allow-categorical public.course.delivery_mode,public.enrollment.status `
  --implicit-fk "public.audit_log(actor_code)=public.staff(staff_code)" `
  --scale 0.1 `
  --seed 42 `
  --drop
```

What `lab clone` produces inside `datasets/my_synth/`:
- `profile.json` — Sanitized statistical profile and `pii_veto_overrides` audit log.
- `ddl/01_types.sql`, `ddl/02_tables.sql`, `ddl/03_fkeys.sql`, `ddl/04_views.sql` — Sanitized DDL with RLS (`ENABLE/FORCE ROW LEVEL SECURITY`, `CREATE POLICY`), triggers (`CREATE TRIGGER`, `ENABLE/DISABLE TRIGGER`), `COMMENT ON`, `OWNER TO`, `GRANT`/`REVOKE`, and `TABLESPACE` stripped.
- `data/<schema>.<table>.csv` & `data/_load.sql` — Seed-deterministic synthetic rows in shuffled order (never sorted by PK/date) plus sequence `setval()` calls.
- `manifest.json` & `.lab-dataset` — Dataset metadata read by `lab import`.
- `validate_report.json` — Detailed results for acceptance checks `A1`–`A9` and `A11`–`A13`.

---

### 3. CLI Subcommands & Flags

You can run the full pipeline via `clone` or execute individual stages:

| Subcommand | Description |
|---|---|
| `python -m lab clone` | End-to-end pipeline: `profile` $\rightarrow$ `generate` $\rightarrow$ `load` $\rightarrow$ `validate`. Supports `--profile-only` and `--dry-run`. |
| `python -m lab profile` | Introspect source DB and write `profile.json` and `ddl/*.sql`. |
| `python -m lab generate` | Generate synthetic CSVs, `_load.sql`, and `manifest.json` purely from `profile.json` (**never connects to the source DB**). |
| `python -m lab load` | Create/recreate target DB (guarded by `COMMENT ON DATABASE <db> IS 'lab_synth:<id>'`), apply DDL & CSVs, run `ANALYZE`, and grant `lab_ro`. |
| `python -m lab validate` | Run acceptance checks `A1`–`A9`, `A11`–`A13` against the target DB and write `validate_report.json`. |
| `python -m lab ack-degraded` | Record human reviewer acknowledgment (`degraded_ack.json`) when complex `CHECK` constraints are dropped (`exit code 2`). |

#### Key Flags

- `--from-env VAR` / `--to-env VAR`: Environment variable name containing the source or target PostgreSQL DSN.
- `--allow-categorical schema.table.col[,...]`: Opt-in allowlist for reading `GROUP BY` value frequencies on low-cardinality text columns.
- `--pii-extra REGEX`: Additional column-name regex patterns to treat as PII.
- `--override-pii-veto "schema.table.col=<reason>"`: Override a false-positive PII column name (e.g., `stage_name` or `card_suit`) with a mandatory audit reason recorded in `profile.json["pii_veto_overrides"]`.
- `--implicit-fk "schema.child(col1,col2)=schema.parent(pk1,pk2)"`: Declare an implicit foreign key without a `pg_constraint` entry; enforces target uniqueness and 100% referential integrity.
- `--exclude REGEX`: Additional table-name regex to exclude from profiling and cloning.
- `--scale FLOAT` / `--max-rows INT` / `--seed INT` / `--locale LOCALE`: Control row scaling (`min(max_rows, min(row_count, max(50, round(row_count * scale))))`), RNG seed, and Faker locale.
- `--drop`: Drop and recreate the target database **only** if it already carries the `COMMENT ON DATABASE <db> IS 'lab_synth:<id>'` safety marker.

---

### 4. Exit Codes & Degraded `CHECK` Acknowledgment (§1.5)

| Exit Code | Status | Meaning |
|---|---|---|
| `0` | `passed` | All checks passed and zero `CHECK` constraints were dropped. |
| `2` | `degraded` | Data loaded and validated, but one or more non-constructive `CHECK` constraints fell below `--check-min-repair-rate` (default `0.20`), were dropped on the target, and are listed in `validate_report.json["degraded_checks"]`. |
| `1` | `failed` | Privacy boundary violation, schema/FK/NULL/distribution check failure, unbreakable non-nullable FK cycle, or password in `argv`. |

When `lab clone` exits with code `2`, inspect `degraded_checks` in `datasets/<id>/validate_report.json` and acknowledge the reviewed constraints:

```powershell
python -m lab ack-degraded --out datasets/my_synth --reviewer "your-handle"
```

---

### 5. Importing a Cloned Dataset into Auto-Ontology

```powershell
python -m lab import --dataset my_synth
```

---

### 6. Running the Test Suite

```powershell
python -m pytest lab -q
```

> [!WARNING]
> The acceptance tests in `lab/tests/acceptance/` use the stack's live Postgres and reset the `lab_ro` password. Do not run them while a measurement or demo is in progress.
