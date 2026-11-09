"""Load the target database and grant the read-only role Auto-Ontology connects as."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from lab.common.errors import LabError
from lab.common.pg import Psql, quote_ident, quote_literal
from lab.common.snapshot import LAB_MARKER_PREFIX
from lab.importer.dataset import Dataset

RO_ROLE = "lab_ro"
OWNER_ROLE = "lab_owner"
_MAINTENANCE_DB = "postgres"


@dataclass(frozen=True)
class LoadResult:
    action: Literal["loaded", "reused"]
    seconds: float
    row_counts: dict[str, int]
    row_count_check: Literal["matched", "skipped"]


def ensure_droppable(psql: Psql, dataset: Dataset) -> None:
    """Refuse unless the database is absent or carries this dataset's marker. Changes nothing."""
    comment = _database_comment(psql, dataset.db_name)
    if comment is not None and comment != _marker(dataset):
        raise LabError(
            f"database {dataset.db_name!r} already exists with comment {comment!r}, not {_marker(dataset)!r}; "
            "refusing to use or drop it. Choose another dataset name or remove it yourself."
        )


def prepare_target(psql: Psql, dataset: Dataset, *, drop: bool) -> LoadResult:
    """Load the dataset, or reuse the database `lab clone` or an earlier import already loaded."""
    began = time.monotonic()
    ensure_droppable(psql, dataset)
    exists = _database_comment(psql, dataset.db_name) is not None
    if exists and not drop:
        counts, check = _verify_row_counts(psql, dataset)
        _set_search_path(psql, dataset)
        return LoadResult("reused", time.monotonic() - began, counts, check)

    if exists:
        _drop_database(psql, dataset)
    psql.execute(_MAINTENANCE_DB, f"CREATE DATABASE {quote_ident(dataset.db_name)}")
    try:
        psql.execute(
            _MAINTENANCE_DB,
            f"COMMENT ON DATABASE {quote_ident(dataset.db_name)} IS {quote_literal(_marker(dataset))}",
        )
        for path in dataset.load_order:
            psql.run_file(dataset.db_name, path, single_transaction=True)
        _set_search_path(psql, dataset)
        psql.execute(dataset.db_name, "ANALYZE")
        counts, check = _verify_row_counts(psql, dataset)
    except BaseException:
        # A half-loaded database would otherwise look reusable to the next run.
        _drop_database(psql, dataset)
        raise
    return LoadResult("loaded", time.monotonic() - began, counts, check)


def ensure_ro_role(psql: Psql, dataset: Dataset, password: str) -> None:
    """Create or update the cluster-wide role, then grant it read access to this database."""
    exists = psql.scalar(_MAINTENANCE_DB, f"SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(RO_ROLE)}")
    verb = "ALTER" if exists else "CREATE"
    role = quote_ident(RO_ROLE)
    psql.execute(
        _MAINTENANCE_DB,
        f"{verb} ROLE {role} WITH LOGIN PASSWORD {quote_literal(password)} "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;\n"
        f"ALTER ROLE {role} SET default_transaction_read_only = on;\n"
        f"ALTER ROLE {role} SET statement_timeout = '30s';\n"
        f"GRANT CONNECT ON DATABASE {quote_ident(dataset.db_name)} TO {role};\n",
        single_transaction=True,
    )
    schemas = psql.query(
        dataset.db_name,
        "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'",
    )
    grants = "".join(
        f"GRANT USAGE ON SCHEMA {quote_ident(name)} TO {role};\n"
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {quote_ident(name)} TO {role};\n"
        for (name,) in schemas
    )
    psql.execute(dataset.db_name, grants, single_transaction=True)
    _make_foreign_keys_visible(psql, dataset)


def _make_foreign_keys_visible(psql: Psql, dataset: Dataset) -> None:
    """Let the read-only role see foreign keys, which upstream reads through an owner-only view.

    Upstream's connector uses information_schema.constraint_column_usage, which returns rows only
    for tables owned by a role the user holds (docs/FINDINGS.md F6). The role stays read-only by
    default_transaction_read_only, though inheriting ownership means it could turn that off.
    """
    owner = quote_ident(OWNER_ROLE)
    if not psql.scalar(_MAINTENANCE_DB, f"SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(OWNER_ROLE)}"):
        psql.execute(_MAINTENANCE_DB, f"CREATE ROLE {owner} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
    psql.execute(_MAINTENANCE_DB, f"GRANT {owner} TO {quote_ident(RO_ROLE)}")
    tables = psql.query(
        dataset.db_name,
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'",
    )
    statements = "".join(f"ALTER TABLE {quote_ident(s)}.{quote_ident(t)} OWNER TO {owner};\n" for s, t in tables)
    if statements:
        psql.execute(dataset.db_name, statements, single_transaction=True)


def count_foreign_key_columns(psql: Psql, database: str) -> int:
    """Count foreign key columns, which is how the catalog stores them, as seen by the superuser."""
    count = psql.scalar(database, "SELECT COALESCE(sum(cardinality(conkey)), 0) FROM pg_constraint WHERE contype = 'f'")
    return int(count or 0)


def _marker(dataset: Dataset) -> str:
    return f"{LAB_MARKER_PREFIX}{dataset.dataset_id}"


def _database_comment(psql: Psql, database: str) -> str | None:
    """Return the database's comment ('' when it has none), or None when it does not exist."""
    rows = psql.query(
        _MAINTENANCE_DB,
        "SELECT 'x', COALESCE(shobj_description(oid, 'pg_database'), '') FROM pg_database "
        f"WHERE datname = {quote_literal(database)}",
    )
    return rows[0][1] if rows else None


def _drop_database(psql: Psql, dataset: Dataset) -> None:
    psql.execute(_MAINTENANCE_DB, f"DROP DATABASE IF EXISTS {quote_ident(dataset.db_name)} WITH (FORCE)")


def _set_search_path(psql: Psql, dataset: Dataset) -> None:
    if dataset.schemas:
        path = ", ".join(quote_ident(schema) for schema in [*dataset.schemas, "public"])
        psql.execute(dataset.db_name, f"ALTER DATABASE {quote_ident(dataset.db_name)} SET search_path TO {path}")


def _verify_row_counts(psql: Psql, dataset: Dataset) -> tuple[dict[str, int], Literal["matched", "skipped"]]:
    counts = _count_rows(psql, dataset)
    if dataset.row_counts is None:
        return counts, "skipped"
    different = sorted(t for t, want in dataset.row_counts.items() if counts.get(t) != want)
    if different:
        details = ", ".join(f"{t}: manifest {dataset.row_counts[t]}, database {counts.get(t)}" for t in different)
        raise LabError(
            f"row counts in {dataset.db_name!r} differ from the manifest ({details}). "
            "Re-run with --drop to rebuild it."
        )
    return counts, "matched"


def _count_rows(psql: Psql, dataset: Dataset) -> dict[str, int]:
    tables = dataset.tables or list(dataset.row_counts or {})
    if not tables:
        return {}
    union = " UNION ALL ".join(f"SELECT {quote_literal(t)}, count(*) FROM {_qualified(t)}" for t in tables)
    return {name: int(count) for name, count in psql.query(dataset.db_name, union)}


def _qualified(table: str) -> str:
    return ".".join(quote_ident(part) for part in table.split(".", 1))
