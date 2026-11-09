"""Register a target through the gateway and wait until ingest and compilation really finished.

The order here is forced by upstream behaviour (docs/UPSTREAM_ARCHITECTURE.md). Resuming relies on evidence for each stage (connection present,
ingest verdict, embedding rows, last pass status), because a catalog row or a Term count only
shows that an earlier stage started.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from lab.common.config import LabConfig
from lab.common.errors import LabError
from lab.common.gateway import Gateway
from lab.common.pg import Psql, quote_literal
from lab.common.readiness import (
    CompileRun,
    IngestResult,
    count_terms,
    fetch_run_after,
    ingest_evidence,
    ingest_state,
    latest_run_id,
    latest_run_status,
    read_ingestion_logs,
    read_logs_since_boot,
    wait_for_compile,
    wait_for_ingest,
)
from lab.importer.target import RO_ROLE

_STATUS_PATH = "/api/semantic-compilation/status"
_COMPILE_CONFIG_PATH = "/api/configurations/semantic-compilation"
_CONNECTIONS_PATH = "/api/connections"
_POSTGRES_PORT = "5432"


@dataclass(frozen=True)
class RegisterResult:
    resumed_from: Literal["fresh", "compile", "snapshot"]
    ingest: IngestResult | None
    compile_run_id: int | None
    compile_seconds: float | None
    terms: int
    catalog_tables: list[str]
    tables_without_terms: list[str]


def wait_until_idle(
    admin: Gateway,
    *,
    timeout: float = 7200.0,
    poll: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Wait out a running pass, because a trigger during one queues a second."""
    began = monotonic()
    while admin.get(_STATUS_PATH).get("running"):
        if monotonic() - began >= timeout:
            raise LabError(f"a compilation pass was still running after {timeout:.0f}s")
        sleep(poll)


def connection_registered(admin: Gateway, db: str) -> bool:
    """Ask the API: a catalog row can outlive its connection, and the row is blank under Vault."""
    return any(item.get("database_name") == db for item in admin.get(_CONNECTIONS_PATH).get("data") or [])


def catalog_row_exists(psql: Psql, catalog_db: str, db: str) -> bool:
    return psql.scalar(catalog_db, f"SELECT 1 FROM catalog_database WHERE name = {quote_literal(db)}") is not None


def ensure_no_ingest_running(cfg: LabConfig, db: str) -> None:
    """Refuse while an ingest thread for this database is alive, since nothing can stop it."""
    if ingest_state(read_logs_since_boot(cfg.containers.ingestion), db) == "running":
        raise LabError(
            f"an ingest for {db!r} is still running in the ingestion service. It cannot be stopped from here; "
            "wait for it to finish, then re-run."
        )


def connections_with_other_ro_password(psql: Psql, catalog_db: str, password: str) -> list[str]:
    """Name stored lab_ro connections that the password about to be set would lock out."""
    rows = psql.query(
        catalog_db,
        "SELECT name FROM catalog_database "
        f"WHERE connection->>'user' = {quote_literal(RO_ROLE)} "
        f"AND connection->>'password' IS DISTINCT FROM {quote_literal(password)} ORDER BY name",
    )
    return [row[0] for row in rows]


def remove_connection(
    admin: Gateway,
    psql: Psql,
    catalog_db: str,
    db: str,
    timeout: float = 300.0,
    *,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Delete a connection if there is one, then wait for its catalog row to go.

    DELETE clears the connection at once and hands the row to the ingestion service, best-effort.
    A retry can therefore find a row with no connection, where a second DELETE would return 404.
    """
    registered = connection_registered(admin, db)
    if registered:
        admin.delete(f"{_CONNECTIONS_PATH}/{db}")
    began = monotonic()
    while catalog_row_exists(psql, catalog_db, db):
        if monotonic() - began >= timeout:
            raise LabError(
                f"catalog still has a row for {db!r} {timeout:.0f}s after its connection was removed: the "
                "ingestion service's cleanup is best-effort and may have failed. Check its log. Registering "
                "the connection again and deleting it in the web UI re-sends the cleanup."
            )
        sleep(poll)
    return registered


def register_and_compile(admin: Gateway, psql: Psql, cfg: LabConfig, db: str, ro_password: str) -> RegisterResult:
    """Run the registration steps, resuming only from a stage with evidence that it finished."""
    catalog_db = cfg.catalog_db
    ingest: IngestResult | None = None
    resumed_from: Literal["fresh", "compile", "snapshot"]
    if connection_registered(admin, db):
        _require_finished_ingest(psql, cfg, db)
        if count_terms(psql, catalog_db, db) > 0 and latest_run_status(psql, catalog_db) == "succeeded":
            print(f"{db!r} is ingested and the last pass succeeded; going straight to the snapshot.", flush=True)
            return _result("snapshot", None, None, None, psql, catalog_db, db)
        resumed_from = "compile"
        print(f"{db!r} is ingested but not fully compiled; resuming at compilation.", flush=True)
    elif catalog_row_exists(psql, catalog_db, db):
        raise LabError(
            f"catalog has a row for {db!r} but no connection, so a pass would skip it. "
            "Re-run with --drop to clear it and start clean."
        )
    else:
        resumed_from = "fresh"
        ingest = _create_and_ingest(admin, psql, cfg, db, ro_password)

    began = time.monotonic()
    run = _compile(admin, psql, catalog_db)
    seconds = time.monotonic() - began
    return _result(resumed_from, ingest, run, seconds, psql, catalog_db, db)


def _require_finished_ingest(psql: Psql, cfg: LabConfig, db: str) -> None:
    ensure_no_ingest_running(cfg, db)
    pulled, embeddings = ingest_evidence(psql, cfg.catalog_db, db)
    if not pulled or embeddings == 0:
        # Upstream writes the catalog before embedding, so a compile could succeed on a broken ingest.
        raise LabError(
            f"connection {db!r} exists but its ingest did not finish (catalog pulled: {pulled}, "
            f"embedding rows: {embeddings}). Check the ingestion log, then re-run with --drop."
        )


def _create_and_ingest(admin: Gateway, psql: Psql, cfg: LabConfig, db: str, ro_password: str) -> IngestResult:
    connection = {
        "type": "postgresql",
        "host": cfg.containers.postgres,
        "port": _POSTGRES_PORT,
        "user": RO_ROLE,
        "password": ro_password,
        "database": db,
    }
    # The Postgres server's clock, because the Docker VM's can differ from the host's.
    since = psql.scalar(cfg.catalog_db, "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')")
    print(f"Registering {db!r} as {RO_ROLE}...", flush=True)
    admin.post(f"{_CONNECTIONS_PATH}/test", {"connection": connection, "replacing": None})
    admin.post(_CONNECTIONS_PATH, {"connection": connection})

    print("Waiting for ingest...", flush=True)
    result = wait_for_ingest(lambda: read_ingestion_logs(cfg.containers.ingestion, str(since)), db)
    if result.embedding_rows == 0:
        # Upstream logs this as success, but on a new connection it means the embed step is broken.
        raise LabError(
            f"ingest of {db!r} wrote 0 embedding rows on a new connection, so the embed step is broken; "
            "check the ingestion log and the EMBED_MODEL settings, then re-run with --drop"
        )
    return result


def _compile(admin: Gateway, psql: Psql, catalog_db: str) -> CompileRun:
    # Ingest can take long enough for a scheduled pass to start, and a trigger during it queues another.
    wait_until_idle(admin)
    baseline = latest_run_id(psql, catalog_db)
    print("Starting semantic compilation...", flush=True)
    admin.put(_COMPILE_CONFIG_PATH, {"enabled": True})  # This call is the trigger; never send a second one.
    return wait_for_compile(
        lambda after: fetch_run_after(psql, catalog_db, after),
        lambda: bool(admin.get(_STATUS_PATH).get("running")),
        baseline_id=baseline,
    )


def _result(
    resumed_from: Literal["fresh", "compile", "snapshot"],
    ingest: IngestResult | None,
    run: CompileRun | None,
    seconds: float | None,
    psql: Psql,
    catalog_db: str,
    db: str,
) -> RegisterResult:
    terms = count_terms(psql, catalog_db, db)
    if terms <= 0:
        # A pass covers every connection, so its success says nothing about this database.
        raise LabError(
            f"the compilation pass succeeded but {db!r} has no Terms; check the ingestion log for this "
            "database, or re-run with --drop to re-ingest it"
        )
    coverage = term_coverage(psql, catalog_db, db)
    return RegisterResult(
        resumed_from=resumed_from,
        ingest=ingest,
        compile_run_id=run.id if run else None,
        compile_seconds=seconds,
        terms=terms,
        catalog_tables=[table for table, _ in coverage],
        tables_without_terms=[table for table, covered in coverage if covered == "0"],
    )


def catalog_foreign_key_columns(psql: Psql, catalog_db: str, db: str) -> int:
    """Count the physical foreign key columns the catalog holds for this database."""
    count = psql.scalar(
        catalog_db,
        "SELECT count(*) FROM column__foreign_key f "
        "JOIN catalog_column c ON c.id = f.source_column_id "
        "JOIN catalog_table t ON t.id = c.table_id "
        "JOIN catalog_schema s ON s.id = t.schema_id "
        "JOIN catalog_database d ON d.id = s.database_id "
        f"WHERE d.name = {quote_literal(db)}",
    )
    return int(count or 0)


def term_coverage(psql: Psql, catalog_db: str, db: str) -> list[tuple[str, ...]]:
    """Return (schema.table, '1' if it has a Term else '0') for every catalog table of this database."""
    return psql.query(
        catalog_db,
        "SELECT s.name || '.' || t.name, "
        "CASE WHEN EXISTS (SELECT 1 FROM table__term tt WHERE tt.table_id = t.id) THEN '1' ELSE '0' END "
        "FROM catalog_database d "
        "JOIN catalog_schema s ON s.database_id = d.id "
        "JOIN catalog_table t ON t.schema_id = s.id "
        f"WHERE d.name = {quote_literal(db)} ORDER BY 1",
    )
