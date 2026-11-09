"""Run the importer's catalog SQL against real tables shaped like upstream's, since fakes cannot check SQL."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest

from lab.common.pg import Psql, quote_ident, quote_literal
from lab.common.readiness import ingest_evidence, latest_run_status
from lab.importer.register import catalog_row_exists, connections_with_other_ro_password, term_coverage

pytestmark = pytest.mark.docker

DDL = """
CREATE SCHEMA vdb;
CREATE TABLE catalog_database (id text PRIMARY KEY, name text UNIQUE, connection jsonb, pulled timestamptz);
CREATE TABLE catalog_schema (id text PRIMARY KEY, database_id text, name text);
CREATE TABLE catalog_table (id text PRIMARY KEY, schema_id text, name text);
CREATE TABLE table__term (table_id text, term_id text);
CREATE TABLE semantic_compilation_history (id bigserial PRIMARY KEY, status text);
CREATE TABLE vdb.data_objects_layer (database_name varchar(100));
INSERT INTO catalog_schema VALUES ('s1', 'd1', 'public');
INSERT INTO catalog_table VALUES ('t1', 's1', 'a'), ('t2', 's1', 'link');
INSERT INTO table__term VALUES ('t1', 'x');
INSERT INTO vdb.data_objects_layer VALUES ('edu'), ('edu'), ('other');
"""


def connection(user: str, password: str) -> str:
    return quote_literal(json.dumps({"user": user, "password": password}))


@pytest.fixture
def catalog(scratch: Psql) -> Iterator[str]:
    name = f"c_{uuid.uuid4().hex[:12]}"
    scratch.execute("postgres", f"CREATE DATABASE {quote_ident(name)}")
    scratch.execute(name, DDL)
    scratch.execute(
        name,
        "INSERT INTO catalog_database VALUES "
        f"('d1', 'edu', {connection('lab_ro', 'current')}, now()), "
        f"('d2', 'old', {connection('lab_ro', 'stale')}, NULL), "
        f"('d3', 'prod', {connection('postgres', 'other')}, NULL), "
        "('d4', 'vaulted', NULL, NULL)",
    )
    yield name
    scratch.execute("postgres", f"DROP DATABASE {quote_ident(name)} WITH (FORCE)")


def test_term_coverage(scratch: Psql, catalog: str) -> None:
    assert term_coverage(scratch, catalog, "edu") == [("public.a", "1"), ("public.link", "0")]
    assert term_coverage(scratch, catalog, "nope") == []


def test_catalog_row_exists(scratch: Psql, catalog: str) -> None:
    assert catalog_row_exists(scratch, catalog, "vaulted") is True
    assert catalog_row_exists(scratch, catalog, "nope") is False


def test_ingest_evidence(scratch: Psql, catalog: str) -> None:
    assert ingest_evidence(scratch, catalog, "edu") == (True, 2)
    assert ingest_evidence(scratch, catalog, "old") == (False, 0)


def test_latest_run_status(scratch: Psql, catalog: str) -> None:
    assert latest_run_status(scratch, catalog) is None
    scratch.execute(catalog, "INSERT INTO semantic_compilation_history (status) VALUES ('succeeded')")
    assert latest_run_status(scratch, catalog) == "succeeded"
    scratch.execute(catalog, "INSERT INTO semantic_compilation_history (status) VALUES (NULL)")
    assert latest_run_status(scratch, catalog) is None


def test_only_lab_ro_connections_with_another_password_are_named(scratch: Psql, catalog: str) -> None:
    assert connections_with_other_ro_password(scratch, catalog, "current") == ["old"]
