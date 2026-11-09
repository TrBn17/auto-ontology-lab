from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from lab.common.errors import LabError
from lab.common.pg import Psql, quote_ident
from lab.importer import target
from lab.importer.dataset import Dataset
from lab.importer.target import count_foreign_key_columns, ensure_droppable, ensure_ro_role, prepare_target

pytestmark = pytest.mark.docker

PASSWORD = "s3cret-Value"
TABLES_SQL = """
CREATE SCHEMA crm;
CREATE TABLE public.a (id int PRIMARY KEY);
CREATE TABLE crm.b (id int PRIMARY KEY);
"""
DATA_SQL = """
COPY public.a (id) FROM stdin WITH (FORMAT csv);
1
2
\\.
INSERT INTO crm.b VALUES (1);
"""


def marker(psql: Psql, database: str) -> str | None:
    return psql.scalar(
        "postgres",
        f"SELECT COALESCE(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname = '{database}'",
    )


@pytest.fixture
def dataset(scratch: Psql, tmp_path: Path) -> Iterator[Dataset]:
    (tmp_path / "tables.sql").write_text(TABLES_SQL, encoding="utf-8")
    (tmp_path / "data.sql").write_text(DATA_SQL, encoding="utf-8")
    db_name = f"t_{uuid.uuid4().hex[:12]}"
    yield Dataset(
        root=tmp_path,
        dataset_id="edu-test",
        db_name=db_name,
        load_order=[tmp_path / "tables.sql", tmp_path / "data.sql"],
        schemas=["crm"],
        tables=["public.a", "crm.b"],
        row_counts={"public.a": 2, "crm.b": 1},
    )
    scratch.execute("postgres", f"DROP DATABASE IF EXISTS {quote_ident(db_name)} WITH (FORCE)")


def create_unmarked(scratch: Psql, dataset: Dataset) -> None:
    scratch.execute("postgres", f"CREATE DATABASE {quote_ident(dataset.db_name)}")
    scratch.execute(dataset.db_name, "CREATE TABLE precious (id int); INSERT INTO precious VALUES (1)")


def test_new_database_is_loaded_marked_and_analyzed(scratch: Psql, dataset: Dataset) -> None:
    result = prepare_target(scratch, dataset, drop=False)
    assert result.action == "loaded"
    assert result.row_counts == {"public.a": 2, "crm.b": 1}
    assert result.row_count_check == "matched"
    assert marker(scratch, dataset.db_name) == "lab_synth:edu-test"
    analyzed = scratch.scalar(
        dataset.db_name, "SELECT count(*) FROM pg_stat_user_tables WHERE last_analyze IS NOT NULL"
    )
    assert analyzed == "2"


def test_existing_marked_database_is_reused_not_reloaded(scratch: Psql, dataset: Dataset) -> None:
    prepare_target(scratch, dataset, drop=False)
    result = prepare_target(scratch, dataset, drop=False)
    assert result.action == "reused"
    assert result.row_counts["public.a"] == 2


def test_existing_unmarked_database_is_refused_without_drop(scratch: Psql, dataset: Dataset) -> None:
    create_unmarked(scratch, dataset)
    with pytest.raises(LabError, match="lab_synth:edu-test"):
        prepare_target(scratch, dataset, drop=False)


def test_database_marked_for_another_dataset_is_refused(scratch: Psql, dataset: Dataset) -> None:
    prepare_target(scratch, dataset, drop=False)
    other = replace(dataset, dataset_id="other")
    with pytest.raises(LabError, match="lab_synth:other"):
        prepare_target(scratch, other, drop=False)
    with pytest.raises(LabError, match="lab_synth:other"):
        prepare_target(scratch, other, drop=True)
    assert marker(scratch, dataset.db_name) == "lab_synth:edu-test"


def test_drop_refuses_unmarked_database_and_leaves_it(scratch: Psql, dataset: Dataset) -> None:
    create_unmarked(scratch, dataset)
    with pytest.raises(LabError, match="lab_synth:edu-test"):
        prepare_target(scratch, dataset, drop=True)
    assert scratch.scalar(dataset.db_name, "SELECT count(*) FROM precious") == "1"


def test_ensure_droppable_checks_without_changing_anything(scratch: Psql, dataset: Dataset) -> None:
    ensure_droppable(scratch, dataset)  # absent is fine
    create_unmarked(scratch, dataset)
    with pytest.raises(LabError, match="lab_synth:edu-test"):
        ensure_droppable(scratch, dataset)
    assert scratch.scalar(dataset.db_name, "SELECT count(*) FROM precious") == "1"


def test_drop_rebuilds_marked_database(scratch: Psql, dataset: Dataset) -> None:
    prepare_target(scratch, dataset, drop=False)
    scratch.execute(dataset.db_name, "INSERT INTO public.a VALUES (99)")
    result = prepare_target(scratch, dataset, drop=True)
    assert result.action == "loaded"
    assert result.row_counts["public.a"] == 2
    assert marker(scratch, dataset.db_name) == "lab_synth:edu-test"


def test_row_count_mismatch_fails(scratch: Psql, dataset: Dataset) -> None:
    wrong = replace(dataset, row_counts={"public.a": 3, "crm.b": 1})
    with pytest.raises(LabError, match=r"public\.a"):
        prepare_target(scratch, wrong, drop=False)


def test_row_counts_absent_records_skipped(scratch: Psql, dataset: Dataset) -> None:
    result = prepare_target(scratch, replace(dataset, row_counts=None), drop=False)
    assert result.row_count_check == "skipped"


def test_failed_load_removes_the_database_it_created(scratch: Psql, dataset: Dataset) -> None:
    good_data = dataset.load_order[1].read_text(encoding="utf-8")
    dataset.load_order[1].write_text("INSERT INTO public.a VALUES (7);\nSELECT broken;\n", encoding="utf-8")
    with pytest.raises(LabError, match="broken"):
        prepare_target(scratch, dataset, drop=False)
    assert marker(scratch, dataset.db_name) is None

    dataset.load_order[1].write_text(good_data, encoding="utf-8")
    assert prepare_target(scratch, dataset, drop=False).action == "loaded"


def test_custom_schemas_set_search_path(scratch: Psql, dataset: Dataset) -> None:
    prepare_target(scratch, dataset, drop=False)
    assert scratch.scalar(dataset.db_name, "SHOW search_path") == "crm, public"


def test_ro_role_can_select_every_table_and_cannot_insert(scratch: Psql, dataset: Dataset) -> None:
    prepare_target(scratch, dataset, drop=False)
    ensure_ro_role(scratch, dataset, PASSWORD)
    readonly = Psql(scratch.container, "lab_ro")
    assert readonly.scalar(dataset.db_name, "SELECT count(*) FROM public.a") == "2"
    assert readonly.scalar(dataset.db_name, "SELECT count(*) FROM crm.b") == "1"
    with pytest.raises(LabError):
        readonly.execute(dataset.db_name, "INSERT INTO public.a VALUES (5)")


def test_ro_role_sees_foreign_keys_and_is_still_read_only(scratch: Psql, dataset: Dataset) -> None:
    prepare_target(scratch, dataset, drop=False)
    scratch.execute(
        dataset.db_name, "CREATE TABLE public.c (id int PRIMARY KEY, a_id int REFERENCES public.a (id))"
    )
    ensure_ro_role(scratch, dataset, PASSWORD)
    readonly = Psql(scratch.container, "lab_ro")
    visible = readonly.scalar(
        dataset.db_name,
        "SELECT ccu.table_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.constraint_column_usage ccu USING (constraint_name, table_schema) "
        "WHERE tc.constraint_type = 'FOREIGN KEY'",
    )
    assert visible == "a"
    assert count_foreign_key_columns(scratch, dataset.db_name) == 1
    with pytest.raises(LabError):
        readonly.execute(dataset.db_name, "INSERT INTO public.a VALUES (5)")


def test_ro_role_settings(scratch: Psql, dataset: Dataset) -> None:
    prepare_target(scratch, dataset, drop=False)
    ensure_ro_role(scratch, dataset, PASSWORD)
    readonly = Psql(scratch.container, "lab_ro")
    assert readonly.scalar(dataset.db_name, "SHOW default_transaction_read_only") == "on"
    assert readonly.scalar(dataset.db_name, "SHOW statement_timeout") == "30s"


def test_ro_role_is_idempotent_across_two_databases(scratch: Psql, dataset: Dataset, tmp_path: Path) -> None:
    second = replace(dataset, db_name=f"{dataset.db_name}_b")
    prepare_target(scratch, dataset, drop=False)
    prepare_target(scratch, second, drop=False)
    try:
        ensure_ro_role(scratch, dataset, PASSWORD)
        ensure_ro_role(scratch, second, PASSWORD)
        ensure_ro_role(scratch, dataset, PASSWORD)
        readonly = Psql(scratch.container, "lab_ro")
        assert readonly.scalar(dataset.db_name, "SELECT count(*) FROM public.a") == "2"
        assert readonly.scalar(second.db_name, "SELECT count(*) FROM public.a") == "2"
    finally:
        scratch.execute("postgres", f"DROP DATABASE IF EXISTS {quote_ident(second.db_name)} WITH (FORCE)")


def test_ro_password_absent_from_error_messages(
    scratch: Psql, dataset: Dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_target(scratch, dataset, drop=False)
    # A syntax error makes psql echo the failing line, which holds the password clause.
    monkeypatch.setattr(target, "RO_ROLE", "")
    with pytest.raises(LabError) as caught:
        ensure_ro_role(scratch, dataset, PASSWORD)
    assert "PASSWORD '***'" in str(caught.value)
    assert PASSWORD not in str(caught.value)
