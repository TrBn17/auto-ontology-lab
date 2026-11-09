"""Checks that make a run's numbers trustworthy: the input before it, and the pipeline during it."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from typing import Any

from lab.common.config import LabConfig
from lab.common.errors import LabError
from lab.common.pg import Psql
from lab.importer import register
from lab.importer.dataset import Dataset
from lab.importer.target import _verify_row_counts, count_foreign_key_columns

MAX_SKIPPED_STEP_RATE = 0.05
# Both mean the agent continued on a default instead of this step's result (docs/FINDINGS.md F5).
_SKIPPED_STEP = re.compile(r"LLM returned None for (\w+) on attempt 3/3|Reasoning LLM also failed for (\w+)")


def check_input(psql: Psql, cfg: LabConfig, dataset: Dataset) -> dict[str, Any]:
    """Compare the restored T0 with the dataset. Raises rather than measure the wrong input."""
    db = dataset.db_name
    row_counts, row_check = _verify_row_counts(psql, dataset)
    target_fks = count_foreign_key_columns(psql, db)
    catalog_fks = register.catalog_foreign_key_columns(psql, cfg.catalog_db, db)
    coverage = register.term_coverage(psql, cfg.catalog_db, db)
    catalog_tables = {table for table, _ in coverage}

    problems = []
    if catalog_fks != target_fks:
        problems.append(f"the catalog holds {catalog_fks} foreign key columns, the database has {target_fks}")
    missing = sorted(set(dataset.tables) - catalog_tables)
    if missing:
        problems.append(f"tables missing from the catalog: {', '.join(missing)}")
    if problems:
        raise LabError(f"input for {db!r} is not trustworthy: {'; '.join(problems)}. Re-run `lab import --drop`.")
    return {
        "row_count_check": row_check,
        "rows": sum(row_counts.values()),
        "tables": len(catalog_tables),
        "tables_without_terms": sorted(table for table, covered in coverage if covered == "0"),
        "foreign_key_columns": target_fks,
    }


def parse_skipped_steps(log: str) -> dict[str, int]:
    counts = Counter(first or second for first, second in _SKIPPED_STEP.findall(log))
    return dict(sorted(counts.items()))


def skipped_steps_since(container: str, since: str) -> dict[str, int]:
    proc = subprocess.run(["docker", "logs", "--since", since, container], capture_output=True)
    if proc.returncode != 0:
        raise LabError(f"could not read the logs of {container}, so skipped steps cannot be counted")
    return parse_skipped_steps((proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace"))


def skipped_rate_ok(skipped: dict[str, int], answers: int) -> bool:
    return answers == 0 or sum(skipped.values()) / answers <= MAX_SKIPPED_STEP_RATE
