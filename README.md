<div align="center">
  <img src="https://img3.pixhost.to/images/6977/800059020_image.png" alt="Auto-Ontology Lab Banner" width="100%" />
  <h1>Auto-Ontology Lab</h1>
</div>

Test [NVIDIA Auto-Ontology](https://github.com/NVIDIA/auto-ontology) on **your own PostgreSQL schema** without exposing your data. The lab clones your database into a synthetic copy, builds the ontology on that copy, and measures four things:

| Suite | Question it answers |
|---|---|
| `discover` | Does the ontology find your tables, foreign keys and undeclared joins? |
| `ask` | Does its agent answer business questions correctly, compared with giving an LLM the schema? |
| `evolve` | After a schema migration, does the ontology update and keep the edits your reviewers made? |
| `audit` | Does it decline questions your data cannot answer, and does it keep PII out of its catalog? |

## How it works

```
your database ──lab clone (read-only)──▶ synthetic copy ──lab import──▶ Auto-Ontology ──lab measure──▶ lab report
   (untouched)                              (in the lab's Postgres)                                       reports/RESULTS.md
```

- **Your database is only read, once**, by `lab clone`: schema plus statistics (`pg_stats`, value ranges, key cardinalities), through a read-only session with a 60 s statement timeout. Free text and PII-named columns are never read.
- **Everything else runs on the synthetic copy**: same tables, columns, keys, comments and distributions, invented values. Auto-Ontology and its LLM never see real data.
- **Destructive steps are fenced.** `evolve` alters the schema and every suite restores a snapshot (T0) with `DROP DATABASE`; both refuse any database without the lab's `lab_synth:` marker.

## Results on the bundled dataset

`edu-public` is a synthetic 15-table LMS database (41,285 rows) with a known ground truth. Upstream pinned at `c447003f`.

| Suite | Result |
|---|---|
| `discover` | 18/18 foreign keys, 0 invented relationships; a term on 14/15 tables, two with the wrong meaning; the one undeclared join missed |
| `ask` (25 questions × 3 runs) | Agent 73% (95% CI 57–88) vs 72% for a schema-only prompt and 87% with column comments; about 8× slower |
| `evolve` (8 schema changes) | Re-ingest keeps 6/6 edits but misses 3–4 of 8 changes; reset reflects 7/8 and keeps 1/6 edits |
| `audit` | 0 of 20 unanswerable questions declined; raw emails and phones stay in the catalog with PII detection on |

Every table, failed question and SQL: [`reports/RESULTS.md`](reports/RESULTS.md). Screenshots: [`reports/evidence/`](reports/evidence/). Upstream issues found on the way: [`docs/FINDINGS.md`](docs/FINDINGS.md).

## Quick start (bundled dataset)

Requirements: Docker, Python 3.10+, an NVIDIA API key.

```powershell
pip install "psycopg[binary]>=3.1" "numpy>=1.26" "faker>=24.0" "pytest>=8.0"

# 1. The pinned Auto-Ontology stack, plus the request-format proxy (upstream issue #440)
git clone https://github.com/NVIDIA/auto-ontology.git
git -C auto-ontology checkout c447003f
docker compose -f auto-ontology/docker-compose.yml -f config/compose.llm-proxy.yml up -d

# 2. Settings: copy, then fill in the tokens minted in the Auto-Ontology UI (admin and viewer)
cp .env.example .env

# 3. Run
python -m lab doctor                           # stack matches config/freeze.yaml?
python -m lab import  --dataset edu-public     # load, compile, save snapshot T0
python -m lab measure --dataset edu-public     # all four suites (hours: see RESULTS.md)
python -m lab report  --dataset edu-public     # writes reports/RESULTS.md
```

For a quick smoke test (about 15 minutes, most of it restoring T0): `python -m lab measure --dataset edu-public --only ask --limit 3 --runs 1`.

## Run it on your own database

### 1. Clone (reads your database, writes nothing to it)

Set `LAB_SOURCE_DSN` (a read-only role on your database) and `LAB_TARGET_DSN` (the lab's Postgres) in `.env`, then:

```powershell
# Look first: writes profile.json and lists which low-cardinality columns could keep their real values
python -m lab clone --from-env LAB_SOURCE_DSN --out datasets/my-db --profile-only

# Then the full clone
python -m lab clone --from-env LAB_SOURCE_DSN --to-env LAB_TARGET_DSN --out datasets/my-db `
  --allow-categorical public.orders.status,public.customer.tier `
  --implicit-fk "public.payment(ref_code)=public.invoice(code)" `
  --scale 0.1 --seed 42
```

- `--allow-categorical`: status-like columns whose real values (not free text) the copy should keep. Columns whose names look like PII are refused.
- `--implicit-fk`: joins your application relies on but no foreign key declares. They become ground truth for `discover`.
- Exit code `0` passed, `1` failed, `2` passed but some `CHECK` constraints could not be satisfied by synthetic rows and were dropped on the copy. Review them in `validate_report.json`, then record the review with `python -m lab ack-degraded --out datasets/my-db --reviewer <you>` (it still exits `2`, and writes `degraded_ack.json`).

### 2. Write the answer key

The lab scores against answers only someone who knows the data can write. Copy the format from `datasets/edu-public/`:

| File | Suite | What you write |
|---|---|---|
| `questions.json` | `ask` | Business questions with gold SQL |
| `metadata.json` | `ask` | Column descriptions, for the `ddl+meta` baseline |
| `evolve.json`, `mutations/*.json` | `evolve` | Schema changes to try and the reviewer edits to protect |
| `unanswerable.json` | `audit` | Questions your schema cannot answer |

Gold SQL runs on the synthetic copy, so it may only rely on what cloning preserves: allowlisted values, foreign keys, date ranges. Check it before any LLM runs:

```powershell
python -m lab import  --dataset my-db        # about 17 minutes for 15 tables: ingest + LLM compilation
python -m lab measure --dataset my-db --check-gold
```

`--check-gold` also flags questions that cannot tell right from wrong on your copy (a known wrong query returns the same result as the gold one). Rewrite or drop those.

### 3. Measure and report

```powershell
python -m lab measure --dataset my-db               # or --only discover|ask|evolve|audit
python -m lab report  --dataset my-db              # writes reports/RESULTS_my-db.md
```

`report` exits `3` when a suite was unhealthy (more than 5% of answers lost to timeouts or errors); the file is still written and says which.

### Tested on

The "own database" path above was run end to end on 2026-11-09, on two source databases other than the published one:

| Source | Tables | `clone` | `import` | `--check-gold` | `discover` | `ask` (3 questions × 1 run) |
|---|---|---|---|---|---|---|
| edu-public source, cloned at `--scale 0.1` | 15 | 12/12 checks, exit 0 | 17 min; 14 terms, 18/18 FKs | 59/60 usable (`x07` cannot discriminate at 10% scale) | entities F1 97%, FKs 18/18, undeclared join missed | 3/3 in every mode; gateway median 249 s |
| `hahlex_edu` | 45 | exit 2: 8 `CHECK` constraints dropped, acknowledged with `ack-degraded` | not run | — | — | — |

`evolve` and `audit` were not re-run on these copies; they take hours.

## Commands

| Command | Does |
|---|---|
| `lab clone` | `profile` → `generate` → `load` → `validate` in one go |
| `lab profile` / `generate` / `load` / `validate` | The clone steps one at a time. `generate` never connects to the source |
| `lab doctor` | Checks the running stack against `config/freeze.yaml` |
| `lab import` | Registers the copy with Auto-Ontology, compiles it, saves snapshot T0 |
| `lab measure` | Runs the suites from T0 (`--only`, `--runs`, `--limit`, `--modes`, `--mutation`, `--check-gold`) |
| `lab report` | Writes the results file from the newest run of each suite of that dataset |

Run any of them with `python -m lab <command> --help` for every flag.

## Limits

- PostgreSQL only, for both the source and the copy.
- Synthetic values keep distributions, not content: a question about one specific customer will not have the real answer.
- Writing the answer key is manual and is most of the work.
- Never put a password on the command line: `python -m lab` exits if an argument contains one. Use the `.env` variables.

## Tests

```powershell
python -m pytest lab -q
```

> [!WARNING]
> The acceptance tests in `lab/tests/acceptance/` use the stack's live Postgres and reset the `lab_ro` password. Do not run them while a measurement is in progress.

## More

- [`PLAN.md`](PLAN.md): design decisions and milestones.
- [`docs/STEP1_CLONE_PLAN.md`](docs/STEP1_CLONE_PLAN.md): the `lab clone` spec and acceptance checks A1–A13.
- [`docs/UPSTREAM_ARCHITECTURE.md`](docs/UPSTREAM_ARCHITECTURE.md): how Auto-Ontology works, with `file:line` references.
