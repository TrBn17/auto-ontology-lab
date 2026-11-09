# Auto-Ontology Lab — Results

> **Not a headline result:** at least one suite failed its health check or is incomplete.

## Freeze

| Item | Value |
|---|---|
| Upstream commit | `c447003f72a822301e927196a4ecb5b597c308d5` |
| Alembic head | `99d4d343dd0b` |
| Model: embed | `nvidia/nemotron-3-embed-1b` |
| Model: gym_control | `nvidia/nemotron-3.5-lightning-30b-a3b` |
| Model: non_reasoning | `nvidia/nemotron-3.5-super-vl-120b-a12b` |
| Model: reasoning | `nvidia/nemotron-3.5-lightning-30b-a3b` |
| Model: rerank | `nvidia/nemotron-3.5-rerank-8b` |
| Dataset | `edu-public` (manifest `85f8ff0165ee`) |
| Questions | `076bf746ada2` |
| Flags | `DB_PROBE_JOIN_PATH_CHECK=False`, `DB_PROBE_PROACTIVE=False`, `INTERACTIVE=False`, `LLM_MAX_INFLIGHT=6`, `distinct_value_probing_enabled=True`, `llm_proxy=guided_json_to_top_level`, `pii_detection_enabled=True`, `semantic_compilation_enabled=True`, `sql_query_timeout_seconds=30` |

## Deviations from a stock install

- Chat models go through `lab/proxy/llm_proxy.py`, which only rewrites the structured-output request format (upstream issue #440, `docs/FINDINGS.md` F2).
- `NON_REASONING_MODEL` is `nvidia/nemotron-3.5-super-vl-120b-a12b`, not upstream's default `nvidia/nemotron-3.5-lightning-30b-a3b`, which exhausts its token limit on most calls (F4).
- Tables are owned by a `NOLOGIN` role granted to `lab_ro`, so upstream's connector can read foreign keys (F6). A read-only role alone yields 0 of 18 FKs.

## discover

| Target | Precision | Recall | F1 | Missed |
|---|---|---|---|---|
| Entities | 100% | 93% | 97% | public.cls_instr |
| Physical FK | 100% | 100% | 100% | — |
| Semantic FK | — | 0% | — | public.invoice.code → public.payment.ref_code |

Invented relationships: 0.

## ask (Text-to-SQL)

25 questions × 3 runs. Accuracy is the mean over questions of the share of runs answered correctly; the 95% interval resamples questions (bootstrap, 10,000 draws).

| Mode | Accuracy | 95% CI | By run | Flipped | P50 s | P95 s | Healthy |
|---|---|---|---|---|---|---|---|
| `ddl` | 72% ± 0% | 55%–88% | 72% / 72% / 72% | 2 | 11 | 47 | yes |
| `ddl+meta` | 87% ± 2% | 77%–95% | 84% / 88% / 88% | 7 | 10 | 45 | yes |
| `gateway` | 73% ± 2% | 57%–88% | 76% / 72% / 72% | 4 | 86 | 396 | yes |

**Gateway vs baselines** (exact McNemar on per-question majority over runs):

| Baseline | Questions | Only gateway right | Only baseline right | p |
|---|---|---|---|---|
| `ddl` | 25 | 3 | 2 | 1.000 |
| `ddl+meta` | 25 | 2 | 5 | 0.453 |

**By category** (4–5 questions each: directional only):

| Category | n | `ddl` | `ddl+meta` | `gateway` |
|---|---|---|---|---|
| ambiguous_date | 4 | 83% | 100% | 100% |
| join | 4 | 75% | 83% | 75% |
| metric | 4 | 92% | 83% | 100% |
| no_fk_join | 4 | 50% | 75% | 67% |
| simple | 4 | 100% | 100% | 100% |
| soft_delete (explicit) | 2 | 100% | 83% | 33% |
| soft_delete (implicit) | 3 | 0% | 78% | 0% |

*Implicit* soft-delete questions say "students" without saying "current"; the gold excludes deleted rows. Read them as a test of an unstated convention, not of SQL skill.

### Gateway failures

**j02** (join) — wrong in 1/3 runs (wrong_result): How many enrollments does each course have? Return the course code and the count.

```sql
-- gold
SELECT c.crs_code, count(*) FROM enrollment e JOIN cls_mst k ON k.cls_id = e.cls_id JOIN crs_mst c ON c.crs_id = k.crs_id GROUP BY c.crs_code
-- predicted (run 2)
SELECT crs_mst.crs_code, COUNT(enrollment.enr_id) AS enrollment_count FROM edu_public.public.crs_mst AS crs_mst LEFT JOIN edu_public.public.cls_mst AS cls_mst ON crs_mst.crs_id = cls_mst.crs_id LEFT JOIN edu_public.public.enrollment AS enrollment ON cls_mst.cls_id = enrollment.cls_id GROUP BY crs_mst.crs_code ORDER BY crs_mst.crs_code;
```

**j03** (join) — wrong in 2/3 runs (wrong_result): How many classes does each instructor teach? Return the instructor id and the count.

```sql
-- gold
SELECT i.instr_id, count(*) FROM instr_mst i JOIN cls_instr ci ON ci.instr_id = i.instr_id GROUP BY i.instr_id
-- predicted (run 0)
SELECT instr_mst.instr_id, COUNT(DISTINCT cls_instr.cls_id) AS class_count FROM edu_public.public.instr_mst AS instr_mst LEFT JOIN edu_public.public.cls_instr AS cls_instr ON instr_mst.instr_id = cls_instr.instr_id GROUP BY instr_mst.instr_id ORDER BY instr_mst.instr_id;
```

**n02** (no_fk_join) — wrong in 3/3 runs (wrong_result): What is the total paid per invoice status? Return the invoice status and the total.

```sql
-- gold
SELECT i.inv_sts, sum(p.amount) FROM payment p JOIN invoice i ON i.code = p.ref_code GROUP BY i.inv_sts
-- predicted (run 0)
SELECT inv_sts, SUM(amount) AS total FROM edu_public.public.invoice GROUP BY inv_sts ORDER BY inv_sts;
```

**n03** (no_fk_join) — wrong in 1/3 runs (wrong_result): What is the total paid per term? Return the term id and the total.

```sql
-- gold
SELECT i.term_id, sum(p.amount) FROM payment p JOIN invoice i ON i.code = p.ref_code GROUP BY i.term_id
-- predicted (run 1)
SELECT term_id, SUM(amount) AS total FROM edu_public.public.invoice WHERE inv_sts = 'paid' GROUP BY term_id
```

**x01** (soft_delete) — wrong in 3/3 runs (wrong_result): How many students do we have? Return only the count.

```sql
-- gold
SELECT count(*) FROM std_mst WHERE rec_sts = 'active'
-- predicted (run 0)
SELECT COUNT(std_id) FROM edu_public.public.std_mst;
```

**x02** (soft_delete) — wrong in 3/3 runs (wrong_result): How many students does each department have? Return the department code and the count.

```sql
-- gold
SELECT dept_cd, count(*) FROM std_mst WHERE rec_sts = 'active' GROUP BY dept_cd
-- predicted (run 0)
SELECT dept_mst.dept_cd, COUNT(std_mst.std_id) AS student_count FROM edu_public.public.dept_mst LEFT JOIN edu_public.public.std_mst ON dept_mst.dept_cd = std_mst.dept_cd GROUP BY dept_mst.dept_cd ORDER BY dept_mst.dept_cd;
```

**x03** (soft_delete) — wrong in 3/3 runs (wrong_result): How many students joined in 2024? Return only the count.

```sql
-- gold
SELECT count(*) FROM std_mst WHERE rec_sts = 'active' AND created_at >= '2024-01-01' AND created_at < '2025-01-01'
-- predicted (run 0)
SELECT COUNT(DISTINCT std_id) FROM edu_public.public.enrollment WHERE enrolled_at >= '2024-01-01' AND enrolled_at <= '2024-12-31';
```

**x04** (soft_delete) — wrong in 1/3 runs (wrong_result): How many enrollments belong to current students? Return only the count.

```sql
-- gold
SELECT count(*) FROM enrollment e JOIN std_mst s ON s.std_id = e.std_id WHERE s.rec_sts = 'active'
-- predicted (run 2)
SELECT COUNT(*) FROM edu_public.public.enrollment WHERE enr_sts = 'enrolled';
```

**x05** (soft_delete) — wrong in 3/3 runs (wrong_result): What is the total invoiced to current students? Return only the total.

```sql
-- gold
SELECT sum(i.amount) FROM invoice i JOIN std_mst s ON s.std_id = i.std_id WHERE s.rec_sts = 'active'
-- predicted (run 0)
SELECT SUM(i.amount) FROM edu_public.public.invoice i WHERE EXISTS (SELECT 1 FROM edu_public.public.enrollment e JOIN edu_public.public.std_mst s ON e.std_id = s.std_id WHERE e.std_id = i.std_id AND e.enr_sts = 'enrolled');
```


## evolve (vanilla)

T2 = re-ingest, T3 = compile trigger, T4 = semantic reset. Each cell: schema change reflected (assertions passed), edits kept out of 6, probes that regressed, seconds.

| Mutation | Probes before | T2 | T3 | T4 |
|---|---|---|---|---|
| `m01` Rename a column that has a foreign key | 10/10 | ✓ 3/3; edits 6/6; regressed —; 5s | ✓ 3/3; edits 6/6; regressed j03, x01; 11s | ✓ 3/3; edits 1/6; regressed none; 555s |
| `m02` Add a column to a compiled table | 9/10 | ✗ 1/2; edits 6/6; regressed —; 5s | ✗ 1/2; edits 6/6; regressed j03; 21s | ✓ 2/2; edits 1/6; regressed none; 711s |
| `m03` Drop a column | 9/10 | ✓ 2/2; edits 6/6; regressed —; 5s | ✓ 2/2; edits 6/6; regressed j03; 11s | ✓ 2/2; edits 1/6; regressed d01; 732s |
| `m04` Change a column's data type | 4/4 | ✓ 1/1; edits 6/6; regressed —; 5s | ✓ 1/1; edits 6/6; regressed none; 11s | ✓ 1/1; edits 1/6; regressed none; 1193s |
| `m05` Change a column comment | 4/4 | ✗ 0/1; edits 6/6; regressed —; 5s | ✗ 0/1; edits 6/6; regressed j02; 11s | ✗ 0/1; edits 1/6; regressed none; 702s |
| `m06` Promote an implicit relationship to a physical foreign key | 2/4 | ✓ 1/1; edits 6/6; regressed —; 5s | ✓ 1/1; edits 6/6; regressed n03; 11s | ✓ 1/1; edits 1/6; regressed n03; 944s |
| `m07` A new categorical value appears in the data | 3/4 | ✗ 0/1; edits 6/6; regressed —; 5s | ✗ 0/1; edits 6/6; regressed none; 11s | ✓ 1/1; edits 1/6; regressed none; 646s |
| `m08` Create a new table (positive control) | 1/4 | ✗ 2/3; edits 6/6; regressed —; 5s | ✓ 3/3; edits 6/6; regressed none; 62s | ✓ 3/3; edits 1/6; regressed none; 852s |

`m01`: scored without assertions removed from its spec after the run: `{"column": "public.enrollment.std_id", "kind": "no_attribute_on_column"}`; `{"column": "public.enrollment.student_id", "kind": "attribute_on_column"}`

<details><summary>Failed assertions</summary>

- `m02` T2: `attribute_on_column` public.std_mst.scholarship_pct
- `m02` T3: `attribute_on_column` public.std_mst.scholarship_pct
- `m05` T2: `column_description` public.enrollment.enrolled_at
- `m05` T3: `column_description` public.enrollment.enrolled_at
- `m05` T4: `column_description` public.enrollment.enrolled_at
- `m07` T2: `sample_value` public.attendance.att_sts
- `m07` T3: `sample_value` public.attendance.att_sts
- `m08` T2: `term_for_table` public.scholarship

</details>

## audit

**Unanswerable questions** (20, out of the schema): infrastructure 2, invented_sql_executes 18.

Re-asked after an infrastructure failure in `20261108T194801Z`: What is the average commute time of students?; What is the graduation rate by high school of origin?; What is the student satisfaction score per course?

| Question | Outcome | SQL |
|---|---|---|
| What is the average salary of instructors? | invented_sql_executes | `SELECT CAST(AVG(CASE WHEN i.instr_id IS NULL THEN i.instr_id ELSE NULL END) AS NUMERIC) AS average_salary FROM edu_public.public.instr_mst AS i` |
| Which students live in campus housing? | invented_sql_executes | `SELECT std_id, full_nm FROM edu_public.public.std_mst ORDER BY std_id;` |
| How many library books are currently checked out? | invented_sql_executes | `SELECT COUNT(*) AS total_checked_out_books FROM edu_public.public.assignment WHERE asg_id = -1;` |
| What is the average commute time of students? | infrastructure | `chat exceeded 900s without finishing` |
| Which classes are held in which buildings? | invented_sql_executes | `SELECT cm.cls_id, cm.cls_sts, ss.sess_dt FROM edu_public.public.cls_mst AS cm LEFT JOIN edu_public.public.cls_sess AS ss ON cm.cls_id = ss.cls_id GROUP BY cm.cl` |
| How many parking permits were issued this year? | invented_sql_executes | `SELECT COUNT(*) AS total_issued FROM edu_public.public.invoice WHERE EXTRACT(YEAR FROM issued_dt) = 2026;` |
| What is the graduation rate by high school of origin? | infrastructure | `chat exceeded 900s without finishing` |
| Which instructors hold a doctoral degree? | invented_sql_executes | `SELECT full_nm FROM edu_public.public.instr_mst;` |
| How many students play on a sports team? | invented_sql_executes | `SELECT COUNT(DISTINCT std_mst.std_id) AS student_count FROM edu_public.public.std_mst INNER JOIN edu_public.public.enrollment ON std_mst.std_id = enrollment.std` |
| What is the average GPA of international students? | invented_sql_executes | `SELECT CAST(AVG(final_score) AS NUMERIC) AS average_gpa FROM edu_public.public.grade;` |
| How much did the university spend on marketing? | invented_sql_executes | `SELECT SUM(i.amount) AS total_amount FROM edu_public.public.invoice AS i JOIN edu_public.public.term_mst AS t ON i.term_id = t.term_id WHERE t.start_dt <= DATE ` |
| Which courses have a required textbook? | invented_sql_executes | `SELECT DISTINCT crs_mst.title FROM edu_public.public.assignment JOIN edu_public.public.cls_mst ON assignment.cls_id = cls_mst.cls_id JOIN edu_public.public.crs_` |
| How many meals were served in the dining hall? | invented_sql_executes | `SELECT COUNT(*) AS meals_served FROM edu_public.public.cls_sess WHERE sess_sts = 'dining_hall';` |
| What is the student satisfaction score per course? | invented_sql_executes | `SELECT crs_mst.title, AVG(submission.score) AS avg_satisfaction_score FROM edu_public.public.submission JOIN edu_public.public.assignment ON submission.asg_id =` |
| How many students received a visa extension? | invented_sql_executes | `SELECT COUNT(*) AS total_students FROM edu_public.public.std_mst AS s WHERE EXISTS (SELECT 1 FROM edu_public.public.enrollment AS e WHERE e.std_id = s.std_id AN` |
| Which alumni donated more than a thousand dollars? | invented_sql_executes | `SELECT pay_id, amount, paid_dt, method FROM edu_public.public.payment WHERE amount > 1000` |
| What is the average class temperature in the lab rooms? | invented_sql_executes | `WITH class_base AS (SELECT cls_id, cls_sts FROM edu_public.public.cls_mst WHERE cls_id IS NOT NULL AND cls_sts IS NOT NULL) SELECT AVG(CAST(NULL AS numeric)) AS` |
| How many instructors are on sabbatical? | invented_sql_executes | `SELECT COUNT(*) AS sabbatical_instructor_count FROM edu_public.public.instr_mst WHERE hire_dt IS NULL;` |
| Which students have a disability accommodation? | invented_sql_executes | `SELECT full_nm, NULL AS has_disability_accommodation FROM edu_public.public.std_mst;` |
| What is the employment rate of graduates after six months? | invented_sql_executes | `SELECT CASE WHEN COUNT(DISTINCT e.std_id) = 0 THEN 0 ELSE CAST(COUNT(DISTINCT CASE WHEN e.enr_sts = 'completed' THEN e.std_id END) AS REAL) / COUNT(DISTINCT e.s` |

**PII left in the catalog** (columns named like PII):

| Column | Raw samples stored | Vector rows repeating one |
|---|---|---|
| `public.instr_mst.email` | 5 | 1 |
| `public.std_mst.email` | 5 | 1 |
| `public.std_mst.phone` | 5 | 1 |

## Sources

- `reports/runs/20261108T041804Z`
- `reports/runs/20261108T074233Z`
- `reports/runs/20261108T074256Z`
- `reports/runs/20261108T092404Z`
- `reports/runs/20261108T110907Z`
- `reports/runs/20261108T125313Z`
- `reports/runs/20261108T143535Z`
- `reports/runs/20261108T153432Z`
- `reports/runs/20261108T162930Z`
- `reports/runs/20261108T175109Z`
- `reports/runs/20261108T184734Z`
- `reports/runs/20261109T003649Z`
