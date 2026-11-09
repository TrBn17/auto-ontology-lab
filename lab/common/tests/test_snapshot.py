from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from lab.common.errors import LabError
from lab.common.pg import Psql
from lab.common.snapshot import restore_catalog, restore_target, snapshot_catalog, snapshot_target

pytestmark = pytest.mark.docker

CATALOG = "catalog_under_test"
TARGET = "target_under_test"

# Shaped like the real catalog: an FK inside public, a pgvector table, a bigserial sequence,
# frontend tables in scope, and auth tables that must never enter a snapshot.
CATALOG_DDL = """
CREATE EXTENSION vector;
CREATE SCHEMA vdb; CREATE SCHEMA frontend;
CREATE TABLE public.alembic_version (version_num text PRIMARY KEY);
INSERT INTO public.alembic_version VALUES ('head1');
CREATE TABLE public.term (id text PRIMARY KEY, description text);
CREATE TABLE public.column_attribute (id text PRIMARY KEY, term_id text REFERENCES public.term(id));
CREATE TABLE public.semantic_compilation_history (id bigserial PRIMARY KEY, status text);
CREATE TABLE vdb.semantic_layer (id serial PRIMARY KEY, body text, embedding vector(3));
CREATE TABLE frontend."user" (id text PRIMARY KEY);
CREATE TABLE frontend.apikey (id text PRIMARY KEY, key text, "referenceId" text REFERENCES frontend."user"(id));
CREATE TABLE frontend.configurations (key text PRIMARY KEY, value text);
CREATE TABLE frontend.conversation_analytics (id serial PRIMARY KEY, user_id text REFERENCES frontend."user"(id));
INSERT INTO frontend."user" VALUES ('u1');
INSERT INTO frontend.apikey VALUES ('k1', 'SECRET_HASH', 'u1');
INSERT INTO public.term VALUES ('t1', 'tiếng Việt, with ''quote''');
INSERT INTO public.column_attribute VALUES ('c1', 't1');
INSERT INTO public.semantic_compilation_history (status) VALUES ('succeeded'), ('succeeded');
INSERT INTO vdb.semantic_layer (body, embedding) VALUES ('Term: t1', '[1,2,3]');
INSERT INTO frontend.configurations VALUES ('semantic_compilation_enabled', 'true');
INSERT INTO frontend.conversation_analytics (user_id) VALUES ('u1');
"""


@pytest.fixture
def catalog(scratch: Psql) -> Iterator[str]:
    scratch.execute("postgres", f"DROP DATABASE IF EXISTS {CATALOG} WITH (FORCE)")
    scratch.execute("postgres", f"CREATE DATABASE {CATALOG}")
    scratch.execute(CATALOG, CATALOG_DDL)
    yield CATALOG
    scratch.execute("postgres", f"DROP DATABASE IF EXISTS {CATALOG} WITH (FORCE)")


@pytest.fixture
def target(scratch: Psql) -> Iterator[str]:
    scratch.execute("postgres", f"DROP DATABASE IF EXISTS {TARGET} WITH (FORCE)")
    scratch.execute("postgres", f"CREATE DATABASE {TARGET}")
    scratch.execute(TARGET, "CREATE TABLE enrollment (id int PRIMARY KEY, enrolled_at date); INSERT INTO enrollment VALUES (1, '2026-01-02');")
    scratch.execute("postgres", f"COMMENT ON DATABASE {TARGET} IS 'lab_synth:edu'")
    yield TARGET
    scratch.execute("postgres", f"DROP DATABASE IF EXISTS {TARGET} WITH (FORCE)")


def _idle() -> bool:
    return True


def _mutate(psql: Psql) -> None:
    psql.execute(
        CATALOG,
        """
        UPDATE public.term SET description = 'edited';
        INSERT INTO public.term VALUES ('t2', 'new');
        DELETE FROM public.column_attribute;
        INSERT INTO public.semantic_compilation_history (status) VALUES ('failed');
        DELETE FROM vdb.semantic_layer;
        UPDATE frontend.configurations SET value = 'false';
        DELETE FROM frontend.conversation_analytics;
        """,
    )


def test_roundtrip_restores_every_table_in_scope(scratch: Psql, catalog: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0_edu.dump"
    meta = snapshot_catalog(scratch, catalog, dump)
    assert meta.row_counts["public.term"] == 1
    assert meta.row_counts["vdb.semantic_layer"] == 1
    assert meta.alembic_head == "head1"

    _mutate(scratch)
    restore_catalog(scratch, catalog, dump, is_idle=_idle)

    assert scratch.query(catalog, "SELECT id, description FROM public.term") == [("t1", "tiếng Việt, with 'quote'")]
    assert scratch.scalar(catalog, "SELECT count(*) FROM public.column_attribute") == "1"
    assert scratch.scalar(catalog, "SELECT count(*) FROM public.semantic_compilation_history") == "2"
    assert scratch.scalar(catalog, "SELECT embedding::text FROM vdb.semantic_layer") == "[1,2,3]"
    assert scratch.scalar(catalog, "SELECT value FROM frontend.configurations") == "true"
    assert scratch.scalar(catalog, "SELECT count(*) FROM frontend.conversation_analytics") == "1"


def test_sequences_are_restored_so_next_history_id_continues(scratch: Psql, catalog: str, tmp_path: Path) -> None:
    # Readiness compares history ids to a baseline, so a sequence left ahead or behind would break it.
    dump = tmp_path / "T0.dump"
    snapshot_catalog(scratch, catalog, dump)
    _mutate(scratch)
    restore_catalog(scratch, catalog, dump, is_idle=_idle)
    scratch.execute(catalog, "INSERT INTO public.semantic_compilation_history (status) VALUES ('succeeded')")
    assert scratch.scalar(catalog, "SELECT max(id) FROM public.semantic_compilation_history") == "3"


def test_auth_tables_are_neither_dumped_nor_reset(scratch: Psql, catalog: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.dump"
    snapshot_catalog(scratch, catalog, dump)
    assert b"SECRET_HASH" not in dump.read_bytes()

    # A token minted after T0 has to survive a restore, or every restore would log the lab out.
    scratch.execute(catalog, """INSERT INTO frontend.apikey VALUES ('k2', 'LATER', 'u1')""")
    restore_catalog(scratch, catalog, dump, is_idle=_idle)
    assert scratch.scalar(catalog, "SELECT count(*) FROM frontend.apikey") == "2"


def test_refuses_restore_while_a_pass_is_running(scratch: Psql, catalog: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.dump"
    snapshot_catalog(scratch, catalog, dump)
    _mutate(scratch)
    with pytest.raises(LabError, match="running"):
        restore_catalog(scratch, catalog, dump, is_idle=lambda: False)
    assert scratch.scalar(catalog, "SELECT description FROM public.term WHERE id = 't1'") == "edited"


def test_refuses_tampered_dump(scratch: Psql, catalog: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.dump"
    snapshot_catalog(scratch, catalog, dump)
    with dump.open("ab") as handle:
        handle.write(b"-- tampered\n")
    with pytest.raises(LabError, match="sha256"):
        restore_catalog(scratch, catalog, dump, is_idle=_idle)


def test_refuses_dump_from_different_schema_revision(scratch: Psql, catalog: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.dump"
    snapshot_catalog(scratch, catalog, dump)
    scratch.execute(catalog, "UPDATE public.alembic_version SET version_num = 'head2'")
    with pytest.raises(LabError, match="head1"):
        restore_catalog(scratch, catalog, dump, is_idle=_idle)


def test_refuses_when_a_table_was_added_since_the_dump(scratch: Psql, catalog: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.dump"
    snapshot_catalog(scratch, catalog, dump)
    scratch.execute(catalog, "CREATE TABLE public.added_later (id int)")
    with pytest.raises(LabError, match="added_later"):
        restore_catalog(scratch, catalog, dump, is_idle=_idle)


def test_failed_restore_rolls_back_and_leaves_catalog_untouched(scratch: Psql, catalog: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.dump"
    snapshot_catalog(scratch, catalog, dump)
    _mutate(scratch)

    # Corrupt the dump but keep its recorded hash valid, so the failure happens inside the transaction.
    with dump.open("ab") as handle:
        handle.write(b"\nSELECT * FROM does_not_exist;\n")
    sidecar = dump.with_suffix(".json")
    recorded = json.loads(sidecar.read_text(encoding="utf-8"))
    recorded["sha256"] = hashlib.sha256(dump.read_bytes()).hexdigest()
    sidecar.write_text(json.dumps(recorded), encoding="utf-8")

    with pytest.raises(LabError):
        restore_catalog(scratch, catalog, dump, is_idle=_idle)
    assert scratch.scalar(catalog, "SELECT count(*) FROM public.term") == "2"


def test_failed_snapshot_leaves_no_partial_files(scratch: Psql, tmp_path: Path) -> None:
    dump = tmp_path / "T0.dump"
    with pytest.raises(LabError):
        snapshot_catalog(scratch, "no_such_database", dump)
    assert list(tmp_path.iterdir()) == []


def test_target_roundtrip_undoes_schema_mutation(scratch: Psql, target: str, tmp_path: Path) -> None:
    # Step 5 runs ALTER on the target, so catalog restore alone cannot return to T0.
    dump = tmp_path / "T0.target.dump"
    snapshot_target(scratch, target, dump)
    scratch.execute(target, "ALTER TABLE enrollment RENAME COLUMN enrolled_at TO enrollment_date")

    restore_target(scratch, target, dump, catalog_db=CATALOG)

    assert scratch.query(target, "SELECT id, enrolled_at::text FROM enrollment") == [("1", "2026-01-02")]
    marker = scratch.scalar("postgres", f"SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = '{target}'")
    assert marker == "lab_synth:edu"


def test_target_restore_refuses_database_without_lab_marker(scratch: Psql, target: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.target.dump"
    snapshot_target(scratch, target, dump)
    scratch.execute("postgres", f"COMMENT ON DATABASE {target} IS NULL")
    with pytest.raises(LabError, match="lab_synth"):
        restore_target(scratch, target, dump, catalog_db=CATALOG)
    assert scratch.scalar(target, "SELECT count(*) FROM enrollment") == "1"


def test_target_restore_refuses_the_catalog_even_if_marked(scratch: Psql, target: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.target.dump"
    snapshot_target(scratch, target, dump)
    with pytest.raises(LabError, match="catalog"):
        restore_target(scratch, target, dump, catalog_db=target)
    assert scratch.scalar(target, "SELECT count(*) FROM enrollment") == "1"


def test_target_restore_refuses_bad_dump_before_dropping(scratch: Psql, target: str, tmp_path: Path) -> None:
    dump = tmp_path / "T0.target.dump"
    snapshot_target(scratch, target, dump)
    with dump.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(LabError, match="sha256"):
        restore_target(scratch, target, dump, catalog_db=CATALOG)
    assert scratch.scalar(target, "SELECT count(*) FROM enrollment") == "1"
