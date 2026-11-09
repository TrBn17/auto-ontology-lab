"""Decide when ingest and semantic compilation have really finished.

Upstream's status endpoint is not enough on its own:

- ``catalog_database.pulled`` is written before embedding, and no API reports an ingest's
  outcome. Only the ingestion service's log says so.
- ``GET /api/semantic-compilation/status`` reports ``running=False`` when the ingestion
  service is unreachable, and ``calculated`` is true if *any* database has a Term.
- A history row keeps ``status`` NULL forever if its pass was stopped or crashed.
- A pass covers every connection, so its status says nothing about one database.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from lab.common.errors import LabError
from lab.common.pg import Psql, quote_literal

_FINISHED_COUNTS = re.compile(
    r"(\d+) table\(s\), (\d+) column\(s\), (\d+) embedding row\(s\) in ([\d.]+)s"
)


IngestState = Literal["none", "running", "finished", "failed"]


class ReadinessTimeout(LabError):
    pass


class IngestFailed(LabError):
    pass


class CompileFailed(LabError):
    pass


class CompileNotStarted(LabError):
    pass


class CompileAbandoned(LabError):
    pass


@dataclass(frozen=True)
class IngestResult:
    tables: int
    columns: int
    embedding_rows: int
    seconds: float


@dataclass(frozen=True)
class CompileRun:
    id: int
    status: str | None
    started_at: str
    finished_at: str | None


def wait_for_ingest(
    read_logs: Callable[[], str],
    database: str,
    *,
    timeout: float = 1800.0,
    start_grace: float = 120.0,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> IngestResult:
    """Wait for this database's ingest outcome in logs captured since the trigger."""
    started, finished, failed = _ingest_patterns(database)

    began = monotonic()
    while True:
        seen_start = False
        for line in read_logs().splitlines():
            if failed.search(line):
                raise IngestFailed(f"ingest failed for {database}; see the ingestion container log: {line.strip()}")
            # Needs its own start: an ingest thread from an earlier run keeps going and logs here too.
            if seen_start and finished.search(line):
                return _parse_finished(line)
            if started.search(line):
                seen_start = True
        elapsed = monotonic() - began
        if not seen_start and elapsed >= start_grace:
            # The connection create route swallows trigger errors, so a lost trigger only shows up here.
            raise IngestFailed(f"ingest for {database} never started within {start_grace:.0f}s")
        if elapsed >= timeout:
            raise ReadinessTimeout(f"ingest for {database} did not finish within {timeout:.0f}s")
        sleep(poll)


def wait_for_compile(
    fetch_run_after: Callable[[int], CompileRun | None],
    is_running: Callable[[], bool],
    *,
    baseline_id: int,
    timeout: float = 7200.0,
    start_grace: float = 180.0,
    abandon_after: float = 120.0,
    poll: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> CompileRun:
    """Wait for a pass newer than baseline_id to reach a verdict.

    Take baseline_id before triggering, so an earlier success can never satisfy this.
    The default timeout is generous because a real first compile on this stack took an hour.
    """
    began = monotonic()
    idle_since: float | None = None
    while True:
        now = monotonic()
        run = fetch_run_after(baseline_id)
        if run is not None and run.status == "succeeded":
            return run
        if run is not None and run.status == "failed":
            raise CompileFailed(
                f"compilation pass {run.id} failed. A pass covers every connection, so check the "
                "ingestion log for which database failed."
            )
        if run is None:
            if now - began >= start_grace:
                raise CompileNotStarted(
                    f"no compilation pass started within {start_grace:.0f}s. Triggers are best-effort, and a "
                    "disabled flag or zero connections writes no history row."
                )
        elif is_running():
            idle_since = None
        else:
            idle_since = now if idle_since is None else idle_since
            if now - idle_since >= abandon_after:
                raise CompileAbandoned(
                    f"pass {run.id} has no verdict and nothing has been running for {abandon_after:.0f}s: it was "
                    "stopped or crashed, or the ingestion service is unreachable."
                )
        if now - began >= timeout:
            raise ReadinessTimeout(f"compilation did not finish within {timeout:.0f}s")
        sleep(poll)


def ingest_state(logs: str, database: str) -> IngestState:
    """Return what the last ingest line for this database says."""
    started, finished, failed = _ingest_patterns(database)
    state: IngestState = "none"
    for line in logs.splitlines():
        if failed.search(line):
            state = "failed"
        elif finished.search(line):
            state = "finished"
        elif started.search(line):
            state = "running"
    return state


def read_logs_since_boot(container: str) -> str:
    """Return the log of the running container. Ingest threads do not survive a restart."""
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", container], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise LabError(f"docker inspect {container} failed: {proc.stderr.strip()}")
    return read_ingestion_logs(container, proc.stdout.strip())


def read_ingestion_logs(container: str, since: str) -> str:
    """Return the container's log since an RFC 3339 UTC timestamp."""
    proc = subprocess.run(["docker", "logs", "--since", since, container], capture_output=True)
    if proc.returncode != 0:
        raise LabError(f"docker logs {container} failed: {proc.stderr.decode('utf-8', errors='replace').strip()}")
    # uvicorn and the app logger write to different streams.
    return (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")


def latest_run_id(psql: Psql, catalog_db: str) -> int:
    return int(psql.scalar(catalog_db, "SELECT COALESCE(max(id), 0) FROM semantic_compilation_history") or 0)


def fetch_run_after(psql: Psql, catalog_db: str, baseline_id: int) -> CompileRun | None:
    """Return the newest pass with an id above the baseline."""
    rows = psql.query(
        catalog_db,
        "SELECT id, COALESCE(status, ''), started_at::text, COALESCE(finished_at::text, '') "
        f"FROM semantic_compilation_history WHERE id > {int(baseline_id)} ORDER BY id DESC LIMIT 1",
    )
    if not rows:
        return None
    run_id, status, started_at, finished_at = rows[0]
    return CompileRun(int(run_id), status or None, started_at, finished_at or None)


def count_terms(psql: Psql, catalog_db: str, database: str) -> int:
    """Count Terms in one database. Chat's own 409 check only asks whether any database has one."""
    return int(
        psql.scalar(
            catalog_db,
            "SELECT count(DISTINCT tt.term_id) FROM catalog_database d "
            "JOIN catalog_schema s ON s.database_id = d.id "
            "JOIN catalog_table t ON t.schema_id = s.id "
            "JOIN table__term tt ON tt.table_id = t.id "
            f"WHERE d.name = {quote_literal(database)}",
        )
        or 0
    )


def latest_run_status(psql: Psql, catalog_db: str) -> str | None:
    """Return the newest pass's verdict, or None if it has none or no pass has run."""
    return psql.scalar(catalog_db, "SELECT status FROM semantic_compilation_history ORDER BY id DESC LIMIT 1") or None


def ingest_evidence(psql: Psql, catalog_db: str, database: str) -> tuple[bool, int]:
    """Return whether the catalog was pulled and how many data embeddings exist for this database."""
    literal = quote_literal(database)
    pulled = psql.scalar(catalog_db, f"SELECT 1 FROM catalog_database WHERE name = {literal} AND pulled IS NOT NULL")
    rows = psql.scalar(catalog_db, f"SELECT count(*) FROM vdb.data_objects_layer WHERE database_name = {literal}")
    return pulled is not None, int(rows or 0)


def _ingest_patterns(database: str) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    # Followed by end of line, whitespace or punctuation, so 'edu' does not match 'edu_archive'.
    name = re.escape(database) + r"(?![\w-])"
    return (
        re.compile(rf"Data ingestion started for database {name}"),
        re.compile(rf"Data ingestion finished successfully for database {name}"),
        re.compile(rf"(?:Background ingest failed|Ingest skipped: no loaded connector) for database {name}"),
    )


def _parse_finished(line: str) -> IngestResult:
    match = _FINISHED_COUNTS.search(line)
    if match is None:
        raise IngestFailed(f"ingest finished but its summary could not be parsed: {line.strip()}")
    return IngestResult(int(match[1]), int(match[2]), int(match[3]), float(match[4]))
