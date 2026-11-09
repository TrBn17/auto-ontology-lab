"""Execute SQL as the read-only role and compare results.

The comparison follows auto-ontology-eval's scorer, so numbers can be set side by side: rows
compare as a set, row order and duplicates are ignored, and extra columns fail. One deliberate
difference: numbers compare at 6 decimal places, because psql returns text and `1.0` and `1`
are the same answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from lab.common.errors import LabError
from lab.common.pg import Psql, unsafe_sql_reason

_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class ExecResult:
    rows: set[tuple[str, ...]] | None
    error: str | None


def execute(psql_ro: Psql, database: str, sql: str) -> ExecResult:
    problem = unsafe_sql_reason(sql)
    if problem:
        return ExecResult(None, f"{problem}; not run")
    try:
        rows = psql_ro.query(database, sql)
    except LabError as exc:
        return ExecResult(None, str(exc))
    return ExecResult({normalize_row(row) for row in rows}, None)


def score(gold: ExecResult, predicted: ExecResult) -> str:
    if predicted.rows is None:
        return "sql_error"
    return "correct" if predicted.rows == gold.rows else "wrong_result"


def normalize_row(row: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_normalize_value(value) for value in row)


def _normalize_value(value: str) -> str:
    text = value.strip()
    if not _NUMBER.match(text):
        return text
    try:
        number = Decimal(text).quantize(_PLACES)
    except InvalidOperation:
        return text
    return format(number.normalize() + 0, "f")
