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
| **No text values by default** | Text, varchar and char columns contribute only type, null rate and length statistics. Not even min or max is read. Array and JSON columns follow the same rule: null rate plus array cardinality or text length, never their contents. |
| **No min/max on text or PII-named columns** | Min and max are read only for numeric, date and timestamp columns, and never when the column name matches the PII pattern. A `birth_date`, or a phone number stored as `bigint`, would otherwise put a real value into `profile.json`. Those columns use a default range for their type. |
| **Categorical values come from three sources only** | (a) `pg_enum` labels; (b) literals in `CHECK (col IN (...))`, which Postgres stores and returns as `= ANY (ARRAY[...])`; (c) an explicit `--allow-categorical schema.table.col` allowlist that a human has reviewed. `GROUP BY` frequencies are read **only** for columns in (c). Columns in (a) and (b) get frequencies only if they are also in (c); otherwise labels are sampled uniformly. |
| **The candidate list is printed, not applied** | `--profile-only` prints *candidates* for the allowlist (text columns with ≤ 25 distinct values, max length ≤ 30, no PII name match) together with their distinct count. It never prints the values. Columns that meet the same limits but match the PII pattern appear in a separate "vetoed" list, also as names and counts only. The operator copies the columns they approve into `--allow-categorical`. This matters: Auto-Ontology puts sample values of low-cardinality text columns straight into the SQL prompt (`UPSTREAM_ARCHITECTURE.md` §2.3). Leave `status`-like columns out and the agent filters on made-up literals, which is a dataset defect and not a finding about the product. |
| **PII name match is a veto that needs a stated reason to override** | Columns whose names match the PII pattern (`email|phone|mobile|name|address|street|dob|birth|ssn|tax|passport|iban|card` plus `--pii-extra`) cannot be allowlisted by default. The pattern also hits harmless columns such as `card_type` or `tax_category`, so `--override-pii-veto "schema.table.col=<reason>"` lifts the veto for that one column. The CLI refuses an empty reason. The override only makes the column eligible for `--allow-categorical`; it does not enable min/max. Every override is written to `profile.json` under `pii_veto_overrides` as `{column, matched_pattern, reason}`, so it can be audited later. |
| **Generation never connects to the source** | Profiling writes `profile.json`. Generation reads only `profile.json`. The source DSN is not passed to any generation code path. |

### 1.2 Output layout

```text
datasets/<name>/
├── profile.json            # the only artefact derived from the source
├── ddl/01_types.sql 02_tables.sql 03_fkeys.sql 04_views.sql
├── data/<schema>.<table>.csv   # header row, NULL = \N (matches auto-ontology-eval)
├── manifest.json           # see 1.3
├── validate_report.json
├── degraded_ack.json       # only after a reviewer acknowledges dropped CHECKs (see 1.5)
└── .lab-dataset            # marker file: dataset id, seed, profile hash
```

No `seed.sql`. Step 2 (`lab import`) loads `data/`. Until Step 2 lands, `manifest.json.load_order` lets the existing `scripts/import_dataset.py` work, because it already honours that field.

### 1.3 `manifest.json`

```json
{
  "dataset": "<name>",
  "load_order": ["ddl/01_types.sql", "ddl/02_tables.sql", "data/_load.sql", "ddl/03_fkeys.sql", "ddl/04_views.sql"],
  "row_counts": {"public.enrollment": 500, "public.grade": 500, "public.payment": 500, "public.course": 50},
  "entities": [{"table": "public.enrollment", "pk": ["student_id", "course_id"], "row_count": 500}],
  "relationships": [
    {"kind": "fk", "from": {"table": "public.grade", "columns": ["student_id", "course_id"]},
                   "to":   {"table": "public.enrollment", "columns": ["student_id", "course_id"]}},
    {"kind": "implicit", "from": {"table": "public.payment", "columns": ["course_code"]},
                         "to":   {"table": "public.course", "columns": ["code"]}}
  ],
  "degraded": [{"table": "public.payment", "constraint": "chk_amount_matches_plan", "rows_failed": 412, "rows_repaired": 18, "repair_rate": 0.04, "reason": "repair rate below threshold; dropped on target"}],
  "semantic": {}
}
```

- Relationships are column **lists**, so composite FKs are first-class.
- **Undeclared FKs are declared by the operator** with `--implicit-fk "public.payment(course_code)=public.course(code)"` at profiling time. They get the same aggregate statistics and generation as real FKs but no constraint on the target, and appear in the manifest as `kind: implicit`. Without this, the two columns share no values, so Auto-Ontology prunes the semantic FK and Step 3 and the `no_fk_join` questions in Step 4 measure nothing.
- `data/_load.sql` holds `COPY ... FROM stdin (FORMAT csv, NULL '\N')` blocks with the rows **inline**. `import_dataset.py` pipes SQL into `docker exec psql`, and the container cannot see files on the host, so `\copy` from `data/*.csv` would fail.
- `semantic` is left empty for a human to fill in (Step 3).

### 1.4 Safety

- **The source is read-only.** Every session runs `SET default_transaction_read_only = on` and `SET statement_timeout = '60s'`. Tables over 1M rows are profiled with `TABLESAMPLE SYSTEM (1)`.
- **No passwords in argv.** DSNs are read from environment variables (`--from-env` / `--to-env`) or from `~/.pgpass`. A DSN that contains a password and is passed directly gets rejected.
- **`--drop` deletes only lab databases.** It drops the target only if the database carries the comment `lab_synth:<dataset id>` that `lab clone` set with `COMMENT ON DATABASE`. Otherwise it refuses. The marker is deliberately not a table: Auto-Ontology ingests every schema except `pg_catalog` and `information_schema`, so a marker table would become part of the ontology.
- **RLS is removed from the target DDL entirely.** That covers `CREATE POLICY` and `ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY`. It also strips `OWNER TO`, `GRANT/REVOKE`, `SET TABLESPACE`, `\restrict` and `\connect`.
- **A read-only role is created on the target.** `lab_ro` gets `SELECT` only, `default_transaction_read_only=on` and `statement_timeout=30s`. Its password is read from `LAB_RO_PASSWORD` in the environment (falling back to `lab_ro` in local scratch tests) and is never written to any artefact or log. Both `lab clone` and `lab import` read `LAB_RO_PASSWORD` so the cluster-wide role credentials stay in sync. This role is what Auto-Ontology and the scorer use.

### 1.5 Exit codes

| Code | Meaning |
|---|---|
| 0 | Every acceptance check passed |
| 2 | **Degraded.** Finished, but one or more CHECK constraints could not be satisfied after row regeneration. They were dropped on the target and listed in `manifest.degraded` and `validate_report.json`. Everything else passed. `python -m lab ack-degraded --out <dir> --reviewer <name>` prints the list and records the reviewer and a hash of the list in `degraded_ack.json`. The exit code stays 2, and the acknowledgement stops being valid if the list changes. |
| 1 | Failed. An acceptance check other than CHECK failed, or the run aborted. |

---

## 2. Acceptance suite (engine-independent)

This suite is how an engine gets chosen, and it is also the gate that says Step 1 is done. It lives in `lab/tests/acceptance/` and runs against any engine through a thin adapter.

### 2.1 Fixtures

- **`fixtures/accept.sql`**: committed to the repo. About 15 tables, built to exercise every hard case:
  - a composite PK and a composite FK
  - two composite FKs on one table that share a column, such as `(org_id, program_id)` and `(org_id, program_id, unit_id)`
  - an undeclared FK, declared through `--implicit-fk`
  - a self-referencing FK
  - an FK cycle broken by a nullable column
  - a nullable FK
  - an enum type
  - `CHECK (col IN (...))`
  - a single-column range CHECK
  - a multi-column CHECK (`end_date >= start_date`)
  - a CHECK using a regex (`~`) and one using a function (`char_length(btrim(col))`)
  - a CHECK that often fails on the first draw but is repairable, on a parent table that has children
  - one deliberately unsatisfiable CHECK, which must come out as degraded
  - UNIQUE on two columns
  - a partial unique index
  - a table with RLS enabled and a policy
  - a `NOT NULL` text array with a `cardinality(...)` CHECK, and a `NOT NULL` jsonb column
  - identity and uuid PKs
  - a table with 2 rows, a table with 5 rows and a table with more than 1,000 rows
  - PII-named columns, including a `date` and a numeric one
  - a PII-named column allowlisted through `--override-pii-veto`, such as `card_type`
  - a free-text column
  - a low-cardinality text column that is allowlisted, and one that is not
- **`fixtures/accept_seed.sql`**: fills it with data. Every text value, array element and JSON string contains the canary `CANARY_7f3a`. PII-named numeric and date columns hold sentinels that occur nowhere else: `987654321012` and `1901-02-03`.

### 2.2 Checks

| # | Check | Pass when |
|---|---|---|
| A1 | **Canary** | The string `CANARY_7f3a` and both sentinels do not appear in `profile.json`, `data/`, `ddl/`, `manifest.json`, any log, or any row of the target DB. Searched with `grep -r` plus `SELECT` over every column, cast to text. Applies to every table, including the 2-row and 5-row ones. The one exception is allowlisted categorical columns, whose values are expected to appear. |
| A2 | **Schema fidelity** | Every PK, UNIQUE (partial indexes included), FK (composite FKs included), enum and view is present on the target. Each CHECK is either present or listed in `degraded`. A CHECK missing from both is a failure. The target has no RLS and no schema or table that the source lacks. |
| A3 | **Integrity** | Zero orphan FK rows, zero PK or UNIQUE violations, every remaining CHECK holds. |
| A4 | **Row counts** | Each table has `min(row_count, max(50, round(row_count × scale)))` rows, capped by `--max-rows`. Tolerance is ±5% or ±2 rows, whichever is larger. This formula alone sets row counts; A5 checks how child rows are spread across parents. |
| A5 | **Cardinality** | Per table, `s = target rows / source rows`. For each FK, non-null child rows per parent row is within ±20% of `source average × s_child / s_parent`. The ratio is adjusted because A4 scales tables differently: 154,000 children under 40 unscaled parents drop to a tenth of the source average at scale 0.1, and that is correct. The share of parents that have children is compared (±0.10) only where `s_child` and `s_parent` are within 1% of each other. |
| A6 | **Null rate** | Per column, \|Δ\| ≤ max(0.03, 1/n), where n is the target row count. |
| A7 | **Categorical L1** | For allowlisted columns, Σ\|p_src − p_tgt\| ≤ max(0.10, k/n), where k is the number of distinct values. |
| A8 | **Usability** | `lab_ro` can `SELECT` from every table, sees the expected row count in each (including tables that had RLS on the source), and cannot `INSERT`. |
| A9 | **Determinism** | Two runs in separate processes with the same profile and seed produce byte-identical `data/`. |
| A10 | **Importable** | `scripts/import_dataset.py --dataset <name> --db-name <new name>` loads the dataset into a database that **did not exist before**, because the script skips loading when the database already has tables. Row counts match the manifest, Auto-Ontology ingests it, and compilation produces at least one Term. |
| A11 | **First 1,000 rows** | Auto-Ontology profiles only `LIMIT 1000` with no `ORDER BY`. For tables over 1,000 rows, read in physical order: null rate within ±0.05 of the source, allowlisted categorical L1 ≤ 0.20, and every value with a source share ≥ 5% is present. The limits are looser than A6 and A7 because this is a sample of the table. |
| A12 | **Implicit FKs** | Every non-null value in the source column of an implicit FK exists in the target column, the target column's values are unique in its first 1,000 rows, and A5 holds for the pair. |
| A13 | **CHECK repair** | The repairable CHECK from the fixture stays on the target and is not in `degraded`, its table has the exact row count from A4, and A3 reports no orphans among the children of its rows. |

A10 needs the Auto-Ontology stack. The others need only a scratch Postgres.

**A6 and A7 assume exact quotas.** Their limits are reachable only if null rates and categorical values are allocated by quota. With plain random sampling at n = 50, L1 ≤ 0.10 passes 42% of the time for 3 values and 6% for 5 values (simulated). An engine that samples at random fails these checks on small tables by design, and that counts as a real failure.

---

## 3. Engine decision

seedgen, used as is, was run against A1–A13 on `fixtures/accept.sql` and failed the privacy, schema, integrity and row-count checks (A1–A4), so it was not adopted. Step 1 is a pure-Python engine in `lab/`. `introspect.py` (privacy boundary), `ddl.py`, `validate.py` and the acceptance suite are the lab's own; the engine only turns `profile.json` into rows.

---

## 4. Implementation

Modules, following Appendix A:

| # | Module | Responsibility | Done when |
|---|---|---|---|
| 0a | `lab/tests/fixtures/accept.sql` + `accept_seed.sql` | 15-table + 1-view + 1-excluded-table acceptance fixture with `CANARY_7f3a` and PII sentinels | Loads cleanly into a scratch PostgreSQL instance |
| 0b | `lab/dsn.py` + `lab/pii.py` + `lab/introspect.py` | Password-free `argv` guard (§1.4), PII matcher & override auditing (§1.2), and read-only catalog profiler (`profile.json`, `--profile-only`) | Canary and sentinels absent from `profile.json`; enum and CHECK-IN labels captured; composite keys and expression/partial unique indexes captured; `--override-pii-veto` without a reason is refused |
| 0c | `lab/ddl.py` | `pg_dump --schema-only` via the `postgres` container with password in `PGPASSWORD` env (`pg_catalog` fallback), plus DDL sanitiser stripping RLS, triggers, `COMMENT ON`, `OWNER TO`, `GRANT`/`REVOKE`, and `TABLESPACE` | Sanitised 4-file DDL (`01_types.sql`, `02_tables.sql`, `03_fkeys.sql`, `04_views.sql`) applies cleanly to an empty DB and contains no RLS or triggers |
| 0d | `lab/validate.py` | Acceptance suite checks `A1`–`A9` and `A11`–`A13` (`validate_report.json`, exit codes `0` / `2` / `1`) | Runs against both the acceptance fixture and real synthetic clones and verifies privacy, schema, FKs, CHECKs, distributions, determinism, and `lab_ro` |
| 1a | `lab/plan.py` + `lab/check_parser.py` | Row-count scaling (`min(max_rows, min(row_count, max(50, round(row_count * scale))))`), topological FK ordering with cycle breaking at nullable FKs, and constructive CHECK parsing | Produces deterministic generation order and extracts regex/range/set/comparison hints from `pg_get_constraintdef` |
| 1b | `lab/generate.py` + `lab/writers.py` | Per-table seeded RNG (`sha256(f"{seed}:{table}")`), exact largest-remainder quotas, connected-component composite FK generation, partial/expression unique enforcement, and scratch `pg_temp` CHECK repair (`--check-max-attempts`, `--check-min-repair-rate`) | Writes shuffled `data/*.csv`, `data/_load.sql`, `manifest.json`, `.lab-dataset`, and supports `ack-degraded` (`degraded_ack.json`) |
| 1c | `lab/load.py` + `lab/cli.py` | Safe `--drop` guard (`COMMENT ON DATABASE ... IS 'lab_synth:<id>'`), phased DDL + CSV + FK + view loading, `setval`, `ANALYZE`, `lab_ro` provisioning, and CLI subcommands (`clone`, `profile`, `generate`, `load`, `validate`, `ack-degraded`) | End-to-end `python -m lab clone` passes A1–A13 on `accept.sql` and real schemas |

### Milestones

1. **Rehearsal.** `lab clone` against `fixtures/accept.sql` in the repo. All checks except A10 pass. The expected exit code is 2, with `degraded` holding the deliberately unsatisfiable CHECK and any other CHECK the chosen engine cannot repair. A reviewer goes through each one.
2. **Real run.** `lab clone` against a real internal database through a read-only role. A1–A13 pass, and a human reviews `profile.json` plus the allowlist before generation. Exit code 0 or 2 is accepted. Exit 2 counts only if every dropped CHECK is listed in the report and `degraded_ack.json` shows a reviewer has acknowledged that exact list. How a particular internal database is handled is kept outside this repo. Public docs describe the procedure for any database.

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
- **Composite FKs**: a child picks one whole parent key tuple. It never picks columns one at a time. When several FKs on a table share a column, the widest FK picks first and the others choose only among parent tuples that match the columns already set. If none match, the FK is NULL when nullable; otherwise the row's FKs are redrawn.
- **Implicit FKs**: generated exactly like real FKs from the same parent pool, without a constraint. The target column is generated as unique.
- **CHECKs, by construction**: a CHECK is satisfied up front when the generator can read it from `pg_get_constraintdef`: enum, `= ANY (ARRAY[...])`, a single-column range on a numeric or date column, or `colA >= colB` on two date or numeric columns.
- **CHECKs, by regeneration**: any other CHECK is evaluated on the generated rows in a scratch schema. **Rows are never deleted**, because removing a parent orphans its children and removing any row skews the distributions. Instead:
  1. Each failing row is regenerated, up to `--check-max-attempts` times (default 20). Only the columns the failing CHECK references are redrawn, so the row keeps its identity. A key column may change only before the table's keys are handed to child tables; after that, keys stay fixed. Columns under a quota are changed by swapping values with another row in the table, so the totals stay exact. Each attempt re-tests every CHECK on the table.
  2. Repair rate = rows fixed within the attempt limit ÷ rows that failed initially, recorded per CHECK. Below `--check-min-repair-rate` (default 0.20), the CHECK with the lowest rate is **dropped on the target and reported as degraded** (exit code 2), then the remaining rows are re-evaluated.
  3. At or above that rate, leftover rows get a second pass with 10× the attempts, redrawing all non-key columns. Any row still failing means the CHECK cannot exist on the target, so it is also dropped and reported as degraded.

  No `rejected/` files.
- **FK cycles**: a cycle can only be broken at a nullable FK column. The table at that edge is generated first with the column NULL. Once the other table exists, the column is filled in memory, before anything is written, so the load never needs an `UPDATE` pass. A cycle with no nullable edge is reported as a hard failure (exit code 1).
- **Text**: free text comes from `faker.text()` trimmed to the length statistics. PII-named columns get the matching Faker provider. Locale is a flag, `--locale`, defaulting to `en_US`. Locale does not change what Auto-Ontology sees: it infers meaning from column names and the low-cardinality samples, never from names of people.
- **Categorical and nulls**: allowlisted columns and null rates are allocated by **exact quota** (largest remainder), then shuffled; A6 and A7 depend on this. Enum and CHECK-IN columns that are not allowlisted get an even quota over their labels.
- **Arrays**: element count is drawn within the profiled cardinality range. Elements come from a `<@ ARRAY[...]` CHECK if there is one, otherwise from a template based on the column name.
- **Unique**: track values in a set, per predicate for partial indexes. On a collision, redraw up to 20 times. After that, text columns get a deterministic suffix; on any other type the run fails and names the constraint.
- **Determinism**: one `numpy.random.Generator` per table, seeded from `sha256(f"{seed}:{table}")`. Python's built-in `hash()` differs between processes for strings. Faker uses `seed_instance` per table.
- **Row order**: rows are written in a seeded shuffled order and never sorted, so the first 1,000 rows represent the table (A11).
- **Load**: `COPY` per table in dependency order, then FKs, then views, then `setval`, then `ANALYZE`, then the `lab_ro` role.
