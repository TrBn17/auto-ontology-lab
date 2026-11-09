from __future__ import annotations

from pathlib import Path

import pytest

from lab.common.errors import LabError
from lab.common.pg import Psql, quote_ident, quote_literal, scrub_secrets


def test_quote_ident_doubles_quotes() -> None:
    assert quote_ident('we"ird') == '"we""ird"'


def test_quote_literal_doubles_quotes() -> None:
    assert quote_literal("it's") == "'it''s'"


def test_scrub_secrets_masks_password_clause() -> None:
    text = "LINE 1: ALTER ROLE lab_ro PASSWORD 'hunter2';"
    assert "hunter2" not in scrub_secrets(text)


@pytest.mark.docker
def test_query_returns_rows_with_tricky_values(scratch: Psql) -> None:
    rows = scratch.query("postgres", "SELECT 'a|b', E'line1\\nline2' UNION ALL SELECT 'tiếng', 'Việt'")
    assert sorted(rows) == [("a|b", "line1\nline2"), ("tiếng", "Việt")]


@pytest.mark.docker
def test_scalar_returns_none_for_no_rows(scratch: Psql) -> None:
    assert scratch.scalar("postgres", "SELECT 1 WHERE false") is None
    assert scratch.scalar("postgres", "SELECT 41 + 1") == "42"


@pytest.mark.docker
def test_failed_sql_raises_and_stops_at_first_error(scratch: Psql) -> None:
    scratch.execute("postgres", "DROP TABLE IF EXISTS stop_test")
    with pytest.raises(LabError, match="does_not_exist"):
        scratch.execute("postgres", "SELECT * FROM does_not_exist; CREATE TABLE stop_test();")
    assert scratch.scalar("postgres", "SELECT to_regclass('stop_test')") in (None, "")


@pytest.mark.docker
def test_error_never_contains_password(scratch: Psql) -> None:
    with pytest.raises(LabError) as exc:
        scratch.execute("postgres", "ALTER ROLE no_such_role PASSWORD 'hunter2'")
    assert "hunter2" not in str(exc.value)


@pytest.mark.docker
def test_run_file_streams_utf8_and_copy_blocks_with_prefix(scratch: Psql, tmp_path: Path) -> None:
    sql = tmp_path / "load.sql"
    sql.write_bytes("COPY stream_test (v) FROM stdin;\ntiếng Việt\n\\.\n".encode())
    scratch.run_file(
        "postgres",
        sql,
        prefix="DROP TABLE IF EXISTS stream_test; CREATE TABLE stream_test (v text);\n",
        single_transaction=True,
    )
    assert scratch.scalar("postgres", "SELECT v FROM stream_test") == "tiếng Việt"


@pytest.mark.docker
def test_run_file_single_transaction_rolls_back_on_error(scratch: Psql, tmp_path: Path) -> None:
    scratch.execute("postgres", "DROP TABLE IF EXISTS rollback_test; CREATE TABLE rollback_test (v int); INSERT INTO rollback_test VALUES (1);")
    sql = tmp_path / "bad.sql"
    sql.write_text("TRUNCATE rollback_test;\nSELECT * FROM does_not_exist;\n", encoding="utf-8")
    with pytest.raises(LabError):
        scratch.run_file("postgres", sql, single_transaction=True)
    assert scratch.scalar("postgres", "SELECT count(*) FROM rollback_test") == "1"
