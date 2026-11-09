from __future__ import annotations

import pytest

from lab.common.pg import unsafe_sql_reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1;",
        "SELECT 1 ;\n",
        "SELECT ';' FROM t",
        "SELECT 'it''s; fine' FROM t",
        'SELECT "odd;name" FROM t',
    ],
)
def test_one_statement_is_allowed(sql: str) -> None:
    assert unsafe_sql_reason(sql) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SET default_transaction_read_only = off; INSERT INTO t VALUES (1)",
        "SELECT 1; SELECT 2",
        "SELECT 'a'; DROP TABLE t;",
    ],
)
def test_a_second_statement_is_refused(sql: str) -> None:
    assert "more than one statement" in (unsafe_sql_reason(sql) or "")


def test_psql_meta_commands_are_refused() -> None:
    assert "backslash" in (unsafe_sql_reason("SELECT 1 \\! touch /tmp/x") or "")
