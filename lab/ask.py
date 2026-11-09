"""`lab ask`: ask questions through the gateway. Only `--probe` exists until Step 4."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from lab.common.config import load_config, require_token
from lab.common.errors import LabError
from lab.common.gateway import Gateway
from lab.common.pg import Psql, unsafe_sql_reason
from lab.importer.dataset import Dataset, load_dataset
from lab.importer.target import RO_ROLE

PROBE_COUNT = 5
PROBE_SOURCE = "lab-probe"

ProbeStatus = Literal["valid", "invalid_sql", "abstained", "no_sql", "agent_error", "infrastructure"]


@dataclass(frozen=True)
class ProbeResult:
    question: str
    status: ProbeStatus
    sql: str
    detail: str


def probe_questions(dataset: Dataset, count: int = PROBE_COUNT) -> list[str]:
    """Build questions from the manifest, so smoke tests need no hand-written file.

    A probe checks that the pipeline runs, so it avoids counting rows: upstream rejects a bare
    `SELECT COUNT(*) FROM t` (docs/FINDINGS.md, F1), and that belongs in the measured results.
    """
    sizes = dataset.row_counts or {}
    tables = sorted(dataset.tables, key=lambda table: (-sizes.get(table, 0), table))
    return [f"Show 5 rows from {table}." for table in tables[:count]]


def validate_sql(psql_ro: Psql, database: str, sql: str) -> str | None:
    """Return the error from planning sql as the read-only role, or None when it is valid."""
    problem = unsafe_sql_reason(sql)
    if problem:
        return f"{problem}; not validated"
    try:
        # ANALYZE runs the query, because plain EXPLAIN accepts DML in a read-only transaction.
        psql_ro.query(database, f"EXPLAIN ANALYZE {sql.strip()}")
    except LabError as exc:
        return str(exc)
    return None


def run_probe(viewer: Gateway, psql_ro: Psql, dataset: Dataset) -> list[ProbeResult]:
    """Ask sequentially, and keep infrastructure failures distinct from bad SQL."""
    return [_ask_one(viewer, psql_ro, dataset, question) for question in probe_questions(dataset)]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", required=True, help="Dataset folder name under datasets/.")
    parser.add_argument("--probe", action="store_true", help="Ask 5 smoke questions and check the SQL is valid.")
    parser.add_argument("--freeze", type=Path, default=None, help="Freeze file (default: config/freeze.yaml).")


def run(args: argparse.Namespace) -> int:
    if not args.probe:
        print("Error: only `lab ask --probe` exists until Step 4.", file=sys.stderr)
        return 1
    dataset = load_dataset(args.dataset)
    cfg = load_config(args.freeze)
    viewer = Gateway(cfg.gateway_url, require_token("viewer"), source=PROBE_SOURCE)
    results = run_probe(viewer, Psql(cfg.containers.postgres, RO_ROLE), dataset)
    for result in results:
        print(f"{result.status:<15} {result.question}" + (f"\n    {result.detail}" if result.detail else ""))
    passed = sum(result.status == "valid" for result in results)
    print(f"{passed}/{len(results)} probes returned valid SQL")
    return 0 if results and passed == len(results) else 1


def _ask_one(viewer: Gateway, psql_ro: Psql, dataset: Dataset, question: str) -> ProbeResult:
    try:
        answer = viewer.chat(question, target_db=dataset.db_name)
    except (LabError, httpx.HTTPError) as exc:
        return ProbeResult(question, "infrastructure", "", str(exc))
    if answer.error is not None:
        return ProbeResult(question, "agent_error", answer.sql, answer.error)
    if answer.information_only:
        return ProbeResult(question, "no_sql", "", "answered from the information path, without SQL")
    if answer.abstained:
        return ProbeResult(question, "abstained", "", "the agent returned no SQL")
    error = validate_sql(psql_ro, dataset.db_name, answer.sql)
    if error is not None:
        return ProbeResult(question, "invalid_sql", answer.sql, error)
    return ProbeResult(question, "valid", answer.sql, "")
