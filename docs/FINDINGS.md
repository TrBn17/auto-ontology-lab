# Findings from running against the live stack

Observed behaviour of Auto-Ontology that affects measurement. Each entry says how it was verified. Environment: backend image built 2026-11-06 15:33 UTC (before pin `c447003f`, Python 3.12.13), `nvidia/nemotron-3.5-lightning-30b-a3b` on `integrate.api.nvidia.com`.

## F1. A query naming no column is rejected as referencing no table

`SELECT COUNT(*) FROM attendance` fails validation with "SQL doesn't reference any table known to the catalog". The agent then retries 7 times, restarts the pipeline, and gives up, although the SQL is correct.

**Verified** by calling `validate_sql` in the backend container with no LLM, on `edu_public` and `hahlex_edu`:

| SQL | Result |
|---|---|
| `SELECT COUNT(*) FROM attendance` | Rejected |
| `SELECT COUNT(*) FROM academic_programs` (`hahlex_edu`) | Rejected |
| `SELECT 1 FROM attendance` | Rejected |
| `SELECT * FROM attendance` | Accepted |
| `SELECT att_id FROM attendance` | Accepted |
| `SELECT count(att_id) FROM public.attendance` | Accepted |
| `SELECT COUNT(*) FROM attendance WHERE att_sts = 'present'` | Accepted |
| `SELECT COUNT(*) FROM enrollment e JOIN std_mst s ON s.std_id = e.std_id` | Accepted |

**Where:** `parse_query_single` returns `None` when `parse_query_slim` reports nothing parsed (`auto_ontology/catalog/sql_parse.py:173`), and `_sql_parse_validation` turns that into this message (`retrieval/text_to_sql/agents/sql_parse_validation.py:969`). Why `parse_query_slim` returns false here has not been traced.

**Status:** `sql_parse.py` on upstream `main` matches the pin, and two issue searches found no report.

**Effect on the lab:** "how many X are there" is a basic question. Gold queries `s01`, `s02`, `s03` have this exact shape, so expect failures there unless the agent happens to name a column. They stay in the bank, since this is a real failure. Probes avoid this shape.

## F2. Structured output fails on the hosted endpoint (upstream issue #440)

Structured calls log `[400] unknown field guided_json`, as [#440](https://github.com/NVIDIA/auto-ontology/issues/440) describes.

**Verified:** the same error in our logs during chat and compile, and chat questions taking over 15 minutes. The first probe hit the lab's 900s client timeout.

**Measured directly against the endpoint, with `max_tokens=6000`:**

| Request shape | Runs | Result |
|---|---|---|
| `nvext: {guided_json}`, as the library sends it | 2 | 400 |
| Top-level `guided_json` | 3 | 200, valid JSON |
| `response_format: json_schema` | 3 | 200, valid JSON |

This differs from #440 on one point. It reports `response_format` returning empty content, while here it returned JSON. With `max_tokens=400`, every shape we tried returned empty content, including a plain request with no structured output, and these answers used 878 to 3,109 completion tokens. That suggests a token limit rather than the format, but #440's conditions were not reproduced. The surfaced 400 can also hide a failure in the library's first attempt, which is not yet explained.

**Effect on the lab:** at this speed a full `ask` is not feasible, and scores would reflect this failure more than the product's ability. During the compile, 16 log lines mentioned the error. Terms were still produced for 15 of 15 tables, but 3 have empty descriptions, and whether that comes from this error is unconfirmed.

**Workaround, to be disclosed with any result:** `lab/proxy/llm_proxy.py` moves the guided keys to the top level of the request and changes nothing else. Upstream's code is unmodified. One side effect: the library treats a non-NVIDIA URL as self-hosted and tries guided output before `response_format`, the reverse of its hosted order, so the proxied path is not identical to a working hosted one.

**Update:** the proxy did rewrite calls during this run (36 of 123 in F4), and calls succeed, so #440's 400 is no longer what slows chat. See F4.

## F4. Chat calls exhaust the token limit, so questions take 5 to over 15 minutes

Measured on 2026-11-07 with the image built from the pin, through the proxy, asking the 5 probe questions sequentially. No other chat or compile ran; two short `lab clone` runs used the same Postgres for about 30 seconds:

| Metric | Value |
|---|---|
| Valid SQL | 1 of 5; the other 4 hit the lab's 900s client timeout |
| LLM calls that completed | 123, all HTTP 200 |
| Calls with `finish=length` | 87 of 123 |
| Median completion tokens | 8,192, the limit `get_llm_client` sets by default |
| Total completion tokens | 848,831 |
| Median call duration | 70.7s; maximum 144.6s |

**Results vary between runs.** An earlier run of the same questions returned 2 of 5, with `submission` and `grade` valid and `enrollment` timing out. Here it was the reverse. `attendance` alone took 303s and made 8 calls, 1 of them truncated, yet it timed out in both full runs.

**Mechanism, read from code:** the library's parser returns `None` when output cannot be parsed, and its fallback treats `None` as failure and tries the next of 3 request formats (`langchain-nvidia-ai-endpoints` 1.4.3, `chat_models.py`). `safe_invoke_with_structured_output` then retries, and falls back to the reasoning model, which here is the same model. So one truncated answer can multiply into many long calls. That truncation is what produces these `None` results is an inference, since the proxy does not log bodies.

**Effect of changing only `NON_REASONING_MODEL`,** on the same 5 probes, with `REASONING_MODEL` unchanged. One run each, so these are not stable estimates:

| Non-reasoning model | Valid SQL | Total time | Completion tokens | `finish=length` | Steps returning `None` |
|---|---|---|---|---|---|
| `nemotron-3.5-lightning-30b-a3b` (upstream default) | 1/5 | 3,921s | 848,831 | 87 of 123 | Not counted; several steps timed out |
| `nemotron-3.5-bolt-e4b` | 5/5 | 630s | 31,536 | 1 of 233 | All 5 steps on every question (F5) |
| `nemotron-3.5-super-vl-120b-a12b` | 5/5 | 1,054s | 124,734 | 4 of 40 | None |
| `nemotron-3-nano-omni-30b-a3b-reasoning` | 4/5 | 651s | 60,182 | 0 of 42 | None |

`super-vl` also logged one `SQLGenerationModel` timeout and one proxy 502. `nano-omni` sent `submission` to the information path, so it returned no SQL. Each boot pass before these probes took about 2 to 4 minutes, but those passes also ran on already-compiled data, so they are not comparable to a full compile.

**Not established:** why the default model uses this many tokens, and whether the proxy's format order contributes (see F2).

## F5. Agent steps can fail on every question while chat still returns valid SQL

With `NON_REASONING_MODEL=nvidia/nemotron-3.5-bolt-e4b`, all 5 probes returned valid SQL in 630s total, using 31,536 completion tokens. But `QuestionIntentModel`, `QuestionExtractionModel`, `TableRelevanceModel`, `AnchorColumnModel` and `IntentValidationModel` each logged "LLM returned None" on all 3 attempts, for every question.

**Why it stays silent:** when all attempts return `None`, `safe_invoke_with_structured_output` raises nothing, so the reasoning-model fallback in `invoke_with_structured_output` never runs (`utils/llm_invoke.py`). The log shows no fallback, and the agent continues on defaults, such as "defaulting to calculation" and "using original question".

**Effect on the lab:** `lab ask --probe` cannot see this, because it checks only that the SQL plans. A fast model can look healthy while most of the pipeline is skipped, so any model comparison must also count these warnings. Why `bolt` returns `None` on the real prompts is not established; on a test prompt it returned JSON with an invalid `question_type` enum value despite guided output.

## F6. A read-only connection silently loses every physical foreign key

`edu_public` has 18 foreign keys, but after ingest and compile through `lab_ro` the catalog holds 0 for it and no error is logged. `hahlex_edu`, registered as `postgres`, has 388.

**Why:** the Postgres connector reads foreign keys through `information_schema.constraint_column_usage` (`auto_ontology/connectors/postgres.py:285`), and Postgres only returns rows there for tables owned by a role the user holds. Verified on `edu_public`:

| Role | `constraint_column_usage` rows | `pg_constraint` foreign keys |
|---|---|---|
| `postgres` | 48 | 18 |
| `lab_ro` | 0 | 18 |

**Effect:** a least-privilege connection, the usual choice for production, gets no physical foreign keys. Compile recovered 15 of the 18 as semantic foreign keys, and missed the 1 trap with no physical key (`invoice.code` to `payment.ref_code`). `pg_constraint` would work for any role.

**Effect on the lab:** every `edu_public` result so far ran without physical foreign keys, including the 12-question trial (F4 timings, 6 of 11 correct). That physical FK recall of 0 of 18 in `discover` comes from this role, not from the product's behaviour with an owner connection. Whether this changes accuracy is not measured.

**Lab fix, to be disclosed with any result:** tables now belong to a `lab_owner` role that `lab_ro` inherits (`lab/importer/target.py`). `lab_ro` can therefore leave read-only mode, so the lab refuses LLM-written SQL with more than one statement (`unsafe_sql_reason` in `lab/common/pg.py`). `lab import` now reports foreign key columns in the catalog against the database.

## F3. `cls_instr` was named "Class Instrument"

One compile named the class-to-instructor bridge table "Class Instrument". This is an example of what the abbreviated-name trap measures, from one run while F2 was active, so it is not yet a measured result.
