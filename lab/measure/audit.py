"""Two short audits: unanswerable questions, and PII left in samples and embeddings."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx

from lab.common.errors import LabError
from lab.common.gateway import ChatAnswer, Gateway
from lab.common.pg import Psql, quote_literal
from lab.measure.ask import MAX_INFRASTRUCTURE_RATE
from lab.measure.scoring import ExecResult
from lab.pii import DEFAULT_PII_TOKENS

_PII_REGEX = "|".join(DEFAULT_PII_TOKENS)


def classify_unanswerable(answer: ChatAnswer, result: ExecResult | None) -> str:
    if answer.error is not None:
        return "agent_error"
    if answer.information_only:
        return "information_answer"
    if answer.abstained:
        return "abstained"
    return "invented_sql_executes" if result is not None and result.rows is not None else "invented_sql_fails"


def run_unanswerable(
    viewer: Gateway, questions: list[str], database: str, execute: Callable[[str], ExecResult]
) -> dict[str, Any]:
    """Coverage is instance-wide upstream; chat is scoped to the database."""
    rows = []
    for question in questions:
        coverage = _coverage(viewer, question)
        try:
            answer = viewer.chat(question, target_db=database)
        except (LabError, httpx.HTTPError) as exc:
            rows.append({"question": question, "coverage": coverage, "outcome": "infrastructure", "sql": "", "detail": str(exc)})
            continue
        result = execute(answer.sql) if answer.sql else None
        outcome = classify_unanswerable(answer, result)
        rows.append({"question": question, "coverage": coverage, "outcome": outcome, "sql": answer.sql, "detail": answer.error or ""})
    return _summarize(rows)


def retry_infrastructure(
    previous: dict[str, Any], viewer: Gateway, database: str, execute: Callable[[str], ExecResult]
) -> dict[str, Any]:
    """Re-ask only the questions that failed for infrastructure reasons; keep every other row as it was."""
    retried = [row["question"] for row in previous["questions"] if row["outcome"] == "infrastructure"]
    fresh = {row["question"]: row for row in run_unanswerable(viewer, retried, database, execute)["questions"]}
    rows = [fresh.get(row["question"], row) for row in previous["questions"]]
    return {**_summarize(rows), "retried": retried}


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["outcome"] for row in rows)
    healthy = bool(rows) and counts["infrastructure"] / len(rows) <= MAX_INFRASTRUCTURE_RATE
    return {"healthy": healthy, "counts": dict(counts), "questions": rows}


def pii_findings(samples: list[tuple[str, str]], vector_rows_containing: Callable[[str], int]) -> list[dict[str, Any]]:
    """For columns named like PII, report raw samples stored and how many embeddings repeat one."""
    findings = []
    for column, raw in samples:
        values = _sample_list(raw)
        if values:
            findings.append(
                {
                    "column": column,
                    "raw_samples": len(values),
                    "vector_rows_containing_a_sample": max(vector_rows_containing(value) for value in values),
                }
            )
    return findings


def read_pii_samples(psql: Psql, catalog_db: str, database: str) -> list[tuple[str, str]]:
    rows = psql.query(
        catalog_db,
        "SELECT s.name || '.' || t.name || '.' || c.name, COALESCE(c.sample_values, '') "
        "FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id "
        "JOIN catalog_schema s ON s.id = t.schema_id JOIN catalog_database d ON d.id = s.database_id "
        f"WHERE d.name = {quote_literal(database)} AND c.name ~* {quote_literal(_PII_REGEX)} ORDER BY 1",
    )
    return [(row[0], row[1] if len(row) > 1 else "") for row in rows]


def count_vector_rows(psql: Psql, catalog_db: str, value: str) -> int:
    pattern = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return int(
        psql.scalar(catalog_db, f"SELECT count(*) FROM vdb.semantic_layer s WHERE s::text LIKE {quote_literal(f'%{pattern}%')}")
        or 0
    )


def _coverage(viewer: Gateway, question: str) -> float | None:
    try:
        body = viewer.post("/api/question-entity-coverage", {"question": question})
    except (LabError, httpx.HTTPError):
        return None  # Upstream answers 422 when the flow has no result for a question.
    return (body.get("data") or body).get("coverage")


def _sample_list(raw: str) -> list[str]:
    try:
        values = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(value) for value in values] if isinstance(values, list) else []
