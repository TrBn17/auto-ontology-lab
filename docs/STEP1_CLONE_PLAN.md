# Step 1 — `lab clone`: From a Production DB to a Synthetic DB

> **Objective:** one command that reads any PostgreSQL database through a read-only role and produces a synthetic database with the same schema, comparable distributions, and no production values. PostgreSQL only in v1.
>
> **How this plan is organised:** §1 is the output contract and §2 the acceptance suite. Both stay the same whichever generation engine is used. §3 chooses the engine by running candidates through §2. §4 is written only for the engine §3 picks. The appendix holds the Python generator design, used only if §3 lands on "write our own".

```powershell
python -m lab clone --from-env LAB_SOURCE_DSN --to-env LAB_TARGET_DSN `
                    --out datasets/<name> --scale 0.1 --seed 42
```

---

## 1. Output contract (engine-independent)

### 1.1 Privacy boundary

| Rule | Detail |
|---|---|
| **No text values by default** | Text, varchar and char columns contribute only type, null rate and length statistics. Not even min or max is read. |
| **No min/max on text** | Min and max are read only for numeric, date and timestamp columns. |
| **Categorical values come from three sources only** | (a) `pg_enum` labels; (b) literals in `CHECK (col IN (...))`; (c) an explicit `--allow-categorical schema.table.col` allowlist that a human has reviewed. `GROUP BY` frequencies are read **only** for columns in (c). Columns in (a) and (b) get frequencies only if they are also in (c); otherwise labels are sampled uniformly. |
| **The candidate list is printed, not applied** | `--profile-only` prints *candidates* for the allowlist (text columns with ≤ 25 distinct values, max length ≤ 30, no PII name match) together with their distinct count. It never prints the values. The operator copies the columns they approve into `--allow-categorical`. This matters: Auto-Ontology puts sample values of low-cardinality text columns straight into the SQL prompt (`UPSTREAM_ARCHITECTURE.md` §2.3). Leave `status`-like columns out and the agent filters on made-up literals, which is a dataset defect and not a finding about the product. |
| **PII name match is a hard veto** | Columns whose names match the PII pattern (`email|phone|mobile|name|address|street|dob|birth|ssn|tax|passport|iban|card` plus `--pii-extra`) cannot be allowlisted. The CLI refuses. |
| **Generation never connects to the source** | Profiling writes `profile.json`. Generation reads only `profile.json`. The source DSN is not passed to any generation code path. |

### 1.2 Output layout

```text
datasets/<name>/
├── profile.json            # the only artefact derived from the source
├── ddl/01_types.sql 02_tables.sql 03_fkeys.sql 04_views.sql
├── data/<schema>.<table>.csv   # header row, NULL = \N (matches auto-ontology-eval)
├── manifest.json           # see 1.3
├── validate_report.json
└── .lab-dataset            # marker file: dataset id, seed, profile hash
```

No `seed.sql`. Step 2 (`lab import`) loads `data/`. Until Step 2 lands, `manifest.json.load_order` lets the existing `scripts/import_dataset.py` work, because it already honours that field.

### 1.3 `manifest.json`

```json
{
  "dataset": "<name>",
  "load_order": ["ddl/01_types.sql", "ddl/02_tables.sql", "data/_load.sql", "ddl/03_fkeys.sql", "ddl/04_views.sql"],
  "entities": [{"table": "public.enrollment", "pk": ["student_id", "course_id"]}],
  "relationships": [
    {"kind": "fk", "from": {"table": "public.grade", "columns": ["student_id", "course_id"]},
                   "to":   {"table": "public.enrollment", "columns": ["student_id", "course_id"]}}
  ],
  "degraded": [{"table": "public.payment", "constraint": "chk_amount_matches_plan", "reason": "unsatisfiable by generator; dropped on target"}],
  "semantic": {}
}
```

- Relationships are column **lists**, so composite FKs are first-class.
- `data/_load.sql` is a generated file of `\copy` lines, so `import_dataset.py`'s psql loader can run it.
- `semantic` is left empty for a human to fill in (Step 3).

### 1.4 Safety

- **The source is read-only.** Every session runs `SET default_transaction_read_only = on` and `SET statement_timeout = '60s'`. Tables over 1M rows are profiled with `TABLESAMPLE SYSTEM (1)`.
- **No passwords in argv.** DSNs are read from environment variables (`--from-env` / `--to-env`) or from `~/.pgpass`. A DSN that contains a password and is passed directly gets rejected.
- **`--drop` deletes only lab databases.** It drops the target only if the target contains the table `lab_meta.marker` that `lab clone` created. Otherwise it refuses.
- **RLS is removed from the target DDL entirely.** That covers `CREATE POLICY` and `ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY`. It also strips `OWNER TO`, `GRANT/REVOKE`, `SET TABLESPACE`, `\restrict` and `\connect`.
- **A read-only role is created on the target.** `lab_ro` gets `SELECT` only, `default_transaction_read_only=on` and `statement_timeout=30s`. This role is what Auto-Ontology and the scorer use.

### 1.5 Exit codes

| Code | Meaning |
|---|---|
| 0 | Every acceptance check passed |
| 2 | **Degraded.** Finished, but one or more CHECK constraints could not be satisfied. They were dropped on the target and listed in `manifest.degraded` and `validate_report.json`. Everything else passed. |
| 1 | Failed. An acceptance check other than CHECK failed, or the run aborted. |

---

## 2. Acceptance suite (engine-independent)

This suite is how an engine gets chosen, and it is also the gate that says Step 1 is done. It lives in `lab/tests/acceptance/` and runs against any engine through a thin adapter.

### 2.1 Fixtures

- **`fixtures/accept.sql`**: committed to the repo. About 10 tables, built to exercise every hard case:
  - a composite PK and a composite FK
  - a self-referencing FK
  - an FK cycle broken by a nullable column
  - a nullable FK
  - an enum type
  - `CHECK (col IN (...))`
  - a single-column range CHECK
  - a multi-column CHECK (`end_date >= start_date`)
  - one deliberately unsatisfiable CHECK, which must come out as degraded
  - UNIQUE on two columns
  - identity and uuid PKs
  - a table with 2 rows and a table with 5 rows
  - PII-named columns
  - a free-text column
  - a low-cardinality text column that is allowlisted, and one that is not
- **`fixtures/accept_seed.sql`**: fills it with data. Every text value contains the canary `CANARY_7f3a`.

### 2.2 Checks

| # | Check | Pass when |
|---|---|---|
| A1 | **Canary** | The string `CANARY_7f3a` does not appear in `profile.json`, `data/`, `ddl/`, `manifest.json`, any log, or any row of the target DB. Searched with `grep -r` plus `SELECT` over every text column. Applies to every table, including the 2-row and 5-row ones. The one exception is allowlisted categorical columns, whose values are expected to appear. |
| A2 | **Schema fidelity** | Every PK, UNIQUE, FK (composite FKs included), enum and view is present on the target. Each CHECK is either present or listed in `degraded`. A CHECK missing from both is a failure. |
| A3 | **Integrity** | Zero orphan FK rows, zero PK or UNIQUE violations, every remaining CHECK holds. |
| A4 | **Row counts** | Each table has `min(row_count, max(50, round(row_count × scale)))` rows, capped by `--max-rows`. Tolerance is ±5% or ±2 rows, whichever is larger. Child tables follow their parents' cardinality, as in A5. |
| A5 | **Cardinality** | For each FK, children per parent averages within ±20% of the source, and the share of parents that have children is within ±0.10. |
| A6 | **Null rate** | Per column, \|Δ\| ≤ max(0.03, 1/n), where n is the target row count. |
| A7 | **Categorical L1** | For allowlisted columns, Σ\|p_src − p_tgt\| ≤ max(0.10, k/n), where k is the number of distinct values. |
| A8 | **Usability** | `lab_ro` can `SELECT` from every table and cannot `INSERT`. |
| A9 | **Determinism** | Two runs with the same profile and seed produce byte-identical `data/`. |
| A10 | **Importable** | `scripts/import_dataset.py --dataset <name>` loads the dataset, Auto-Ontology ingests it, and compilation produces at least one Term. |

A10 needs the Auto-Ontology stack. A1–A9 need only a scratch Postgres.

---

## 3. Engine decision (do this first, about half a day)

Run each candidate through §2 on `fixtures/accept.sql`. Record the results in `docs/STEP1_ENGINE_DECISION.md`: one row per check, pass, fail or degraded, with a note.

**Candidate: seedgen, used as is.** The lab ships Python only and takes on no Rust toolchain. A prebuilt seedgen binary may be called as an external tool through an adapter (`lab/engines/seedgen.py`), with its version pinned in `freeze.yaml`. Contributors never build it.

| Result | Direction | Extra estimate |
|---|---|---|
| Passes A1–A10, or the gaps can be closed by the adapter (pre- or post-processing of its input or output, no change to the engine) | **Wrap it.** `lab clone` = our profiler + privacy boundary + seedgen + our validator. | 1–2 days |
| Fails on composite FKs, CHECKs, privacy or determinism, and the adapter cannot fix it | **Write it in Python** using the reviewed design in the appendix. A Rust PR or fork is out of scope. | about 5 days |

Whatever wins, `introspect.py` (privacy boundary), `ddl.py`, `validate.py` and the acceptance suite are ours. The engine only turns `profile.json` into rows.

---

## 4. Implementation (written after §3)

Left empty on purpose. Once the decision is in, this section gets the module list, work order and estimates for the chosen direction only.

The modules shared by both directions can start before the decision, because neither path depends on them:

| # | Task | Done when |
|---|---|---|
| 0a | `fixtures/accept.sql` + `accept_seed.sql` | They load into a scratch Postgres |
| 0b | `introspect.py` + `pii.py` → `profile.json`; `--profile-only` prints the allowlist candidates | Canary absent from `profile.json`; enum and CHECK-IN labels captured; composite keys captured |
| 0c | `ddl.py` (pg_dump through the stack container, with sanitiser; `pg_catalog` fallback for tables, PKs and FKs, composite included) | Sanitised DDL applies to an empty DB and contains no RLS |
| 0d | `validate.py` implementing A2–A9 | Runs against the fixture loaded directly as a "perfect clone" and passes |

### Milestones

1. **Rehearsal.** `lab clone` against `fixtures/accept.sql` in the repo. A1–A9 pass.
2. **Real run.** `lab clone` against a real internal database through a read-only role. A1–A10 pass, and a human reviews `profile.json` plus the allowlist before generation. How a particular internal database is handled is kept outside this repo. Public docs describe the procedure for any database.

---

## 5. Out of scope for Step 1

- Non-PostgreSQL sources.
- Incremental source-to-synthetic sync. A full regeneration is deterministic and cheap.
- The `semantic` section of the manifest (Step 3), questions and gold SQL (Step 4), and mutations (Step 5).
- Realistic numeric histograms. v1 draws numerics uniformly within min and max. Auto-Ontology looks only at min, max and the top 5 values (`UPSTREAM_ARCHITECTURE.md` §2.3).
- JSONB contents beyond `{}`.

---

## Appendix A — Python generator design (only if §3 picks "write our own")

Changes from the first draft, following review:

- **Row count**: `min(row_count, max(50, round(row_count × scale)))`. A 2-row table stays at 2 rows.
- **Composite FKs**: a child picks one whole parent key tuple. It never picks columns one at a time.
- **CHECKs**: no regex parsing. A CHECK is satisfied *by construction* when the generator can see what it means: enum, `IN` list, a single-column range on a numeric or date column, or `colA >= colB` on two date or numeric columns. Any other CHECK is tested against the generated rows inside a transaction on a scratch schema. If it fails, it is **dropped on the target and reported as degraded** (exit code 2). No `rejected/` files, no rows silently thrown away.
- **FK cycles**: a cycle can only be broken at a nullable FK column. The table at that edge is generated first with the column NULL. Once the other table exists, the column is filled in memory, before anything is written, so the load never needs an `UPDATE` pass. A cycle with no nullable edge is reported as a hard failure (exit code 1).
- **Text**: free text comes from `faker.text()` trimmed to the length statistics. PII-named columns get the matching Faker provider. Locale is a flag, `--locale`, defaulting to `en_US`. Locale does not change what Auto-Ontology sees: it infers meaning from column names and the low-cardinality samples, never from names of people.
- **Categorical**: allowlisted columns use the profiled frequencies. Enum and CHECK-IN columns that are not allowlisted are sampled uniformly over their labels.
- **Unique**: track values in a set. On a collision, redraw up to 20 times, then add a deterministic suffix.
- **Determinism**: one `numpy.random.Generator` per table, seeded from `hash(seed, table)`. Faker uses `seed_instance` per table. Output is written in sorted order.
- **Load**: `COPY` per table in dependency order, then FKs, then views, then `setval`, then `ANALYZE`, then the `lab_ro` role.
