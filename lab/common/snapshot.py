"""Snapshot and restore T0: the catalog state, and separately the target database.

Catalog snapshots hold data only, for the catalog tables a run can change. Auth tables stay
out on purpose: tokens minted after T0 must survive a restore, and password hashes do not
belong in a dump. Data-only also means restore never has to drop `public`, which owns the
vector extension. That is safe because the schema is pinned, and restore checks it.

Dumps can contain live connection credentials, because connections are stored in plaintext
on the catalog row when Vault is off. snapshots/ must stay gitignored.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from lab.common.errors import LabError
from lab.common.pg import Psql, quote_ident, quote_literal

_CATALOG_SCHEMAS = ("public", "vdb")
_FRONTEND_TABLES = ("configurations", "conversation_analytics")
LAB_MARKER_PREFIX = "lab_synth:"


@dataclass(frozen=True)
class SnapshotMeta:
    sha256: str
    alembic_head: str | None
    tables: list[str]
    row_counts: dict[str, int]


def snapshot_catalog(psql: Psql, catalog_db: str, dest: Path) -> SnapshotMeta:
    """Dump catalog data to dest, with its metadata beside it as dest.json."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tables = _catalog_tables(psql, catalog_db)
    selectors = [arg for table in tables for arg in ("-t", _qualified(table))]
    psql.dump_to_file(["-d", catalog_db, "--data-only", "--disable-triggers", *selectors], dest)
    meta = SnapshotMeta(
        sha256=_sha256(dest),
        alembic_head=_alembic_head(psql, catalog_db),
        tables=tables,
        row_counts=_row_counts(psql, catalog_db, tables),
    )
    _sidecar(dest).write_text(json.dumps(asdict(meta), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return meta


def restore_catalog(psql: Psql, catalog_db: str, src: Path, *, is_idle: Callable[[], bool]) -> None:
    """Replace in-scope catalog data with a snapshot, as one transaction.

    is_idle has to report that no pass is running, because a pass writing during the
    restore would mix its rows into T0. Running services keep their in-memory connector
    caches, so restore only into the same set of connections the snapshot was taken with.
    """
    meta = _load_meta(src)
    if _sha256(src) != meta.sha256:
        raise LabError(f"{src} does not match the sha256 recorded when it was taken")
    live_head = _alembic_head(psql, catalog_db)
    if live_head != meta.alembic_head:
        raise LabError(f"{src} was taken at schema revision {meta.alembic_head}, but the catalog is at {live_head}")
    live_tables = _catalog_tables(psql, catalog_db)
    if live_tables != meta.tables:
        changed = sorted(set(live_tables) ^ set(meta.tables))
        raise LabError(f"catalog tables differ from the snapshot: {', '.join(changed)}")
    if not is_idle():
        raise LabError("a compilation pass is running; wait for it to finish before restoring")

    truncate = ", ".join(_qualified(table) for table in meta.tables)
    psql.run_file(catalog_db, src, prefix=f"TRUNCATE {truncate};\n", single_transaction=True)

    restored = _row_counts(psql, catalog_db, meta.tables)
    if restored != meta.row_counts:
        different = sorted(t for t in meta.tables if restored.get(t) != meta.row_counts.get(t))
        raise LabError(f"restore finished but row counts differ from the snapshot: {', '.join(different)}")


def snapshot_target(psql: Psql, database: str, dest: Path) -> None:
    """Dump a whole target database. Step 5's ALTERs change its schema, not just its rows."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    psql.dump_to_file(["-d", database, "-Fc"], dest)
    _sidecar(dest).write_text(json.dumps({"sha256": _sha256(dest)}, indent=2) + "\n", encoding="utf-8")


def restore_target(psql: Psql, database: str, src: Path, *, catalog_db: str) -> None:
    """Drop and recreate a lab target from its dump. Refuses anything not marked as a lab database."""
    if database == catalog_db:
        raise LabError(f"refusing to restore over the catalog database {catalog_db!r}")
    marker = psql.scalar(
        "postgres",
        f"SELECT COALESCE(shobj_description(oid, 'pg_database'), '') FROM pg_database "
        f"WHERE datname = {quote_literal(database)}",
    )
    if not (marker or "").startswith(LAB_MARKER_PREFIX):
        raise LabError(f"database {database!r} has no {LAB_MARKER_PREFIX} comment; refusing to drop it")
    if _sha256(src) != json.loads(_sidecar(src).read_text(encoding="utf-8"))["sha256"]:
        raise LabError(f"{src} does not match the sha256 recorded when it was taken")

    psql.execute("postgres", f"DROP DATABASE {quote_ident(database)} WITH (FORCE)")
    # --create restores the database comment too, so the marker survives.
    psql.restore_from_file(["-d", "postgres", "--create", "--exit-on-error"], src)


def _catalog_tables(psql: Psql, catalog_db: str) -> list[str]:
    schemas = ", ".join(quote_literal(s) for s in _CATALOG_SCHEMAS)
    frontend = ", ".join(quote_literal(t) for t in _FRONTEND_TABLES)
    rows = psql.query(
        catalog_db,
        "SELECT schemaname || '.' || tablename FROM pg_tables "
        f"WHERE schemaname IN ({schemas}) OR (schemaname = 'frontend' AND tablename IN ({frontend})) "
        "ORDER BY 1",
    )
    return [row[0] for row in rows]


def _row_counts(psql: Psql, catalog_db: str, tables: list[str]) -> dict[str, int]:
    union = " UNION ALL ".join(f"SELECT {quote_literal(t)}, count(*) FROM {_qualified(t)}" for t in tables)
    return {name: int(count) for name, count in psql.query(catalog_db, union)}


def _alembic_head(psql: Psql, catalog_db: str) -> str | None:
    return psql.scalar(catalog_db, "SELECT version_num FROM public.alembic_version")


def _load_meta(src: Path) -> SnapshotMeta:
    sidecar = _sidecar(src)
    if not src.is_file() or not sidecar.is_file():
        raise LabError(f"snapshot {src} or its metadata {sidecar} is missing")
    return SnapshotMeta(**json.loads(sidecar.read_text(encoding="utf-8")))


def _qualified(table: str) -> str:
    schema, _, name = table.partition(".")
    return f"{quote_ident(schema)}.{quote_ident(name)}"


def _sidecar(dump: Path) -> Path:
    return dump.with_suffix(".json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
