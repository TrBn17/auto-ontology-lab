"""Task 4: the call order is the design, so these tests record it against fakes."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lab.common.errors import LabError
from lab.common.gateway import GatewayError
from lab.common.readiness import CompileFailed, CompileRun, IngestResult, wait_for_compile
from lab.common.snapshot import SnapshotMeta
from lab.importer import cli, register
from lab.importer.dataset import Dataset
from lab.importer.target import LoadResult

PASSWORD = "s3cret-Value"
TOKEN = "auto_ontology_tokenvalue"
DB = "hahlex_edu"


class FakeGateway:
    def __init__(self, world: SimpleNamespace) -> None:
        self.world = world
        self.events = world.events
        self.calls: list[tuple[str, str, Any]] = []
        self.running: list[bool] = []

    def get(self, path: str, **params: Any) -> Any:
        if path == "/api/connections":
            rows = [{"database_name": DB}] if self.world.registered else []
            return {"data": rows, "count": len(rows)}
        assert path == "/api/semantic-compilation/status"
        self.events.append("idle")
        return {"running": self.running.pop(0) if self.running else False}

    def post(self, path: str, body: Any = None) -> Any:
        self.calls.append(("POST", path, body))
        self.events.append(f"POST {path}")
        return {}

    def put(self, path: str, body: Any = None) -> Any:
        self.calls.append(("PUT", path, body))
        self.events.append(f"PUT {path}")
        return {"enabled": True}

    def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None))
        self.events.append(f"DELETE {path}")
        if not self.world.registered:
            raise GatewayError(f"DELETE {path} returned 404", 404)
        self.world.registered = False
        if not self.world.row_survives_delete:
            self.world.row_exists = False
        return {}


class FakePsql:
    def __init__(self, world: SimpleNamespace) -> None:
        self.world = world

    def scalar(self, database: str, sql: str) -> str | None:
        if "column__foreign_key" in sql or "pg_constraint" in sql:
            return "0"
        if "catalog_database" in sql:
            self.world.events.append("catalog-row?")
            return "1" if self.world.row_exists else None
        if "now()" in sql:
            return "2026-11-07T01:02:03Z"
        raise AssertionError(sql)

    def query(self, database: str, sql: str) -> list[tuple[str, ...]]:
        assert "catalog_table" in sql
        return [(table, covered) for table, covered in self.world.coverage]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    events: list[str] = []
    state = SimpleNamespace(
        events=events,
        registered=False,
        row_exists=False,
        row_survives_delete=False,
        logs="",
        pulled=True,
        data_embeddings=7,
        last_status="succeeded",
        stored_password_mismatch=[],
        terms=3,
        ingest=IngestResult(2, 5, 7, 1.5),
        compile_error=None,
        coverage=[("public.a", "1"), ("public.link", "0")],
        snapshots=tmp_path / "snapshots",
        target_snapshot_error=None,
    )
    state.admin = FakeGateway(state)
    state.psql = FakePsql(state)
    state.cfg = SimpleNamespace(
        catalog_db="auto_ontology",
        upstream_commit="abc123",
        gateway_url="http://gateway",
        pg_superuser="postgres",
        containers=SimpleNamespace(postgres="postgres", ingestion="ingest"),
    )
    state.dataset = Dataset(
        root=tmp_path / "hahlex-edu",
        dataset_id="hahlex-edu",
        db_name=DB,
        load_order=[],
        schemas=[],
        tables=["public.a", "public.link"],
        row_counts=None,
    )

    def wait_for_ingest(read_logs: Any, database: str, **_: Any) -> IngestResult:
        events.append(f"ingest {database}")
        return state.ingest

    def wait_for_compile(fetch: Any, is_running: Any, *, baseline_id: int, **_: Any) -> CompileRun:
        events.append(f"compile after {baseline_id}")
        if state.compile_error:
            raise state.compile_error
        return CompileRun(baseline_id + 1, "succeeded", "s", "f")

    def latest_run_id(psql: Any, catalog_db: str) -> int:
        events.append("baseline")
        return 41

    def count_terms(psql: Any, catalog_db: str, database: str) -> int:
        events.append(f"terms {database}")
        return state.terms

    monkeypatch.setattr(register, "wait_for_ingest", wait_for_ingest)
    monkeypatch.setattr(register, "wait_for_compile", wait_for_compile)
    monkeypatch.setattr(register, "latest_run_id", latest_run_id)
    monkeypatch.setattr(register, "count_terms", count_terms)
    monkeypatch.setattr(register, "read_logs_since_boot", lambda container: state.logs)
    monkeypatch.setattr(register, "latest_run_status", lambda psql, catalog_db: state.last_status)
    monkeypatch.setattr(register, "ingest_evidence", lambda psql, catalog_db, db: (state.pulled, state.data_embeddings))
    monkeypatch.setattr(register, "connections_with_other_ro_password", lambda *_: state.stored_password_mismatch)

    def prepare_target(psql: Any, dataset: Dataset, *, drop: bool) -> LoadResult:
        events.append(f"prepare drop={drop}")
        return LoadResult("loaded", 1.0, {"public.a": 1}, "skipped")

    def ensure_droppable(psql: Any, dataset: Dataset) -> None:
        events.append("droppable?")

    def ensure_ro_role(psql: Any, dataset: Dataset, password: str) -> None:
        events.append("ro-role")

    def snapshot_catalog(psql: Any, catalog_db: str, dest: Path) -> SnapshotMeta:
        events.append(f"snapshot catalog {dest.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("new-catalog", encoding="utf-8")
        return SnapshotMeta("catalog-sha", "head", [], {})

    def snapshot_target(psql: Any, database: str, dest: Path) -> None:
        events.append(f"snapshot target {dest.name}")
        if state.target_snapshot_error:
            raise state.target_snapshot_error
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("new-target", encoding="utf-8")
        dest.with_suffix(".json").write_text(json.dumps({"sha256": "target-sha"}), encoding="utf-8")

    monkeypatch.setattr(cli, "prepare_target", prepare_target)
    monkeypatch.setattr(cli, "ensure_droppable", ensure_droppable)
    monkeypatch.setattr(cli, "ensure_ro_role", ensure_ro_role)
    monkeypatch.setattr(cli, "snapshot_catalog", snapshot_catalog)
    monkeypatch.setattr(cli, "snapshot_target", snapshot_target)
    return state


def execute(world: SimpleNamespace, *, drop: bool = False, resnapshot: bool = False) -> dict[str, Any]:
    return cli.execute(
        world.admin,
        world.psql,
        world.cfg,
        world.dataset,
        PASSWORD,
        drop=drop,
        resnapshot=resnapshot,
        snapshots_dir=world.snapshots,
    )


def trimmed(events: list[str]) -> list[str]:
    return [e for e in events if e not in ("catalog-row?",)]


def test_steps_run_in_required_order(world: SimpleNamespace) -> None:
    execute(world)
    assert trimmed(world.events) == [
        "idle",
        "prepare drop=False",
        "ro-role",
        "POST /api/connections/test",
        "POST /api/connections",
        f"ingest {DB}",
        "idle",
        "baseline",
        "PUT /api/configurations/semantic-compilation",
        "compile after 41",
        f"terms {DB}",
        "idle",
        "snapshot catalog T0_hahlex-edu.dump",
        "snapshot target T0_hahlex-edu.target.dump",
    ]


def test_waits_while_a_pass_is_running(world: SimpleNamespace) -> None:
    world.admin.running = [True, True, False]
    register.wait_until_idle(world.admin, sleep=lambda _: None)
    assert world.events.count("idle") == 3


def test_idle_wait_times_out(world: SimpleNamespace) -> None:
    world.admin.running = [True] * 10
    clock = iter(range(0, 100, 10))
    with pytest.raises(LabError, match="still running"):
        register.wait_until_idle(world.admin, timeout=15.0, sleep=lambda _: None, monotonic=lambda: next(clock))


def test_baseline_is_read_before_the_trigger(world: SimpleNamespace) -> None:
    execute(world)
    assert world.events.index("baseline") < world.events.index("PUT /api/configurations/semantic-compilation")


def test_only_one_trigger_is_sent(world: SimpleNamespace) -> None:
    execute(world)
    assert [c for c in world.admin.calls if c[0] == "PUT"] == [
        ("PUT", "/api/configurations/semantic-compilation", {"enabled": True})
    ]
    assert not [c for c in world.admin.calls if "trigger" in c[1]]


def test_all_calls_use_the_gateway_and_lab_ro(world: SimpleNamespace) -> None:
    execute(world)
    bodies = [c[2]["connection"] for c in world.admin.calls if c[1].startswith("/api/connections")]
    assert len(bodies) == 2
    for body in bodies:
        assert body == {
            "type": "postgresql",
            "host": "postgres",
            "port": "5432",
            "user": "lab_ro",
            "password": PASSWORD,
            "database": DB,
        }


def test_zero_embedding_rows_on_new_connection_fails(world: SimpleNamespace) -> None:
    world.ingest = IngestResult(2, 5, 0, 1.5)
    with pytest.raises(LabError, match="0 embedding"):
        execute(world)
    assert "baseline" not in world.events


def test_zero_terms_after_successful_pass_fails(world: SimpleNamespace) -> None:
    world.terms = 0
    with pytest.raises(LabError, match="no Terms"):
        execute(world)
    assert not [e for e in world.events if e.startswith("snapshot")]


def test_failed_pass_message_points_to_the_log(world: SimpleNamespace) -> None:
    with pytest.raises(CompileFailed) as real_failure:
        wait_for_compile(lambda _: CompileRun(42, "failed", "s", None), lambda: False, baseline_id=41)
    world.compile_error = real_failure.value
    with pytest.raises(CompileFailed, match="ingestion log"):
        execute(world)
    assert not [e for e in world.events if e.startswith("snapshot")]


def test_tables_without_terms_are_reported(world: SimpleNamespace) -> None:
    report = execute(world)
    assert report["tables_total"] == 2
    assert report["tables_without_terms"] == ["public.link"]


def reuse_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "prepare_target", lambda *_, **__: LoadResult("reused", 0.0, {}, "skipped"))


def existing_connection(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    reuse_target(monkeypatch)
    world.registered = True
    world.row_exists = True


def test_resume_after_a_succeeded_pass_only_takes_snapshot(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing_connection(world, monkeypatch)
    report = execute(world)
    assert report["resumed_from"] == "snapshot"
    assert not world.admin.calls
    assert "snapshot catalog T0_hahlex-edu.dump" in world.events


@pytest.mark.parametrize("last_status", ["failed", None])
def test_terms_from_an_unfinished_pass_resume_at_compile(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, last_status: str | None
) -> None:
    existing_connection(world, monkeypatch)
    world.last_status = last_status
    report = execute(world)
    assert report["resumed_from"] == "compile"
    assert [c[1] for c in world.admin.calls] == ["/api/configurations/semantic-compilation"]


def test_resume_without_terms_starts_at_compile(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    existing_connection(world, monkeypatch)
    counts = iter([0, 3])
    monkeypatch.setattr(register, "count_terms", lambda *_: next(counts))
    report = execute(world)
    assert report["resumed_from"] == "compile"
    assert [c[1] for c in world.admin.calls] == ["/api/configurations/semantic-compilation"]


@pytest.mark.parametrize(
    ("pulled", "embeddings"),
    [(False, 0), (True, 0)],
    ids=["never-pulled", "no-embeddings"],
)
def test_resume_refuses_when_ingest_left_no_evidence(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, pulled: bool, embeddings: int
) -> None:
    existing_connection(world, monkeypatch)
    world.pulled, world.data_embeddings = pulled, embeddings
    world.terms = 0
    with pytest.raises(LabError, match="--drop"):
        execute(world)
    assert not world.admin.calls


def test_resume_refuses_while_ingest_is_still_running(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    existing_connection(world, monkeypatch)
    world.logs = f"Data ingestion started for database {DB}"
    with pytest.raises(LabError, match="still running"):
        execute(world)
    assert not world.admin.calls


def test_row_without_a_connection_is_refused_with_a_way_forward(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    reuse_target(monkeypatch)
    world.row_exists = True
    with pytest.raises(LabError, match="--drop"):
        execute(world)
    assert not world.admin.calls


def test_existing_connection_with_a_freshly_loaded_database_is_refused(world: SimpleNamespace) -> None:
    world.registered = True
    world.row_exists = True
    with pytest.raises(LabError, match="--drop"):
        execute(world)  # the prepare_target fake reports "loaded"
    assert not world.admin.calls


def test_drop_checks_marker_before_touching_the_connection(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.registered = True
    world.row_exists = True

    def refuse(psql: Any, dataset: Dataset) -> None:
        raise LabError("not a lab database")

    monkeypatch.setattr(cli, "ensure_droppable", refuse)
    with pytest.raises(LabError, match="not a lab database"):
        execute(world, drop=True)
    assert not world.admin.calls


def test_drop_refuses_while_ingest_is_still_running(world: SimpleNamespace) -> None:
    world.registered = True
    world.row_exists = True
    world.logs = f"Data ingestion started for database {DB}"
    with pytest.raises(LabError, match="still running"):
        execute(world, drop=True)
    assert not world.admin.calls


def test_drop_waits_for_catalog_row_before_dropping_database(world: SimpleNamespace) -> None:
    world.registered = True
    world.row_exists = True
    world.row_survives_delete = True
    polls = iter([True, True, False])

    def later(_: float) -> None:
        world.row_exists = next(polls)

    assert register.remove_connection(world.admin, world.psql, "catalog", DB, sleep=later) is True
    assert world.events[0] == f"DELETE /api/connections/{DB}"
    assert world.row_exists is False


def test_drop_full_order(world: SimpleNamespace) -> None:
    world.registered = True
    world.row_exists = True
    execute(world, drop=True)
    order = trimmed(world.events)
    assert order[:4] == ["idle", "droppable?", f"DELETE /api/connections/{DB}", "prepare drop=True"]


def test_drop_times_out_if_catalog_row_stays(world: SimpleNamespace) -> None:
    world.registered = True
    world.row_exists = True
    world.row_survives_delete = True
    clock = iter(range(0, 1000, 100))
    with pytest.raises(LabError, match="still has"):
        register.remove_connection(
            world.admin, world.psql, "catalog", DB, timeout=250.0, sleep=lambda _: None, monotonic=lambda: next(clock)
        )


def test_drop_retry_after_a_stuck_row_sends_no_second_delete(world: SimpleNamespace) -> None:
    world.row_exists = True  # upstream cleared the connection, but the row stayed
    clock = iter(range(0, 1000, 100))
    with pytest.raises(LabError, match="still has"):
        register.remove_connection(
            world.admin, world.psql, "catalog", DB, timeout=250.0, sleep=lambda _: None, monotonic=lambda: next(clock)
        )
    assert not world.admin.calls


def test_remove_connection_without_a_connection_sends_nothing(world: SimpleNamespace) -> None:
    assert register.remove_connection(world.admin, world.psql, "catalog", DB) is False
    assert not world.admin.calls


def test_ingest_logs_are_read_since_the_postgres_clock(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    def read_ingestion_logs(container: str, since: str) -> str:
        seen.append((container, since))
        return ""

    def wait_for_ingest(read_logs: Any, database: str, **_: Any) -> IngestResult:
        read_logs()
        return world.ingest

    monkeypatch.setattr(register, "read_ingestion_logs", read_ingestion_logs)
    monkeypatch.setattr(register, "wait_for_ingest", wait_for_ingest)
    execute(world)
    assert seen == [("ingest", "2026-11-07T01:02:03Z")]


def test_existing_t0_is_not_overwritten_without_being_asked(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing_connection(world, monkeypatch)
    world.snapshots.mkdir()
    (world.snapshots / "T0_hahlex-edu.dump").write_text("old", encoding="utf-8")
    with pytest.raises(LabError, match="--resnapshot"):
        execute(world)
    assert world.events == []
    assert (world.snapshots / "T0_hahlex-edu.dump").read_text(encoding="utf-8") == "old"


def test_resnapshot_replaces_t0(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    existing_connection(world, monkeypatch)
    world.snapshots.mkdir()
    (world.snapshots / "T0_hahlex-edu.dump").write_text("old", encoding="utf-8")
    execute(world, resnapshot=True)
    assert (world.snapshots / "T0_hahlex-edu.dump").read_text(encoding="utf-8") == "new-catalog"
    assert (world.snapshots / "T0_hahlex-edu.target.dump").read_text(encoding="utf-8") == "new-target"
    assert not [p for p in world.snapshots.iterdir() if p.is_dir()]


def test_failed_target_dump_keeps_the_old_pair(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    existing_connection(world, monkeypatch)
    world.snapshots.mkdir()
    (world.snapshots / "T0_hahlex-edu.dump").write_text("old", encoding="utf-8")
    world.target_snapshot_error = LabError("pg_dump failed")
    with pytest.raises(LabError, match="pg_dump failed"):
        execute(world, resnapshot=True)
    assert (world.snapshots / "T0_hahlex-edu.dump").read_text(encoding="utf-8") == "old"
    assert not [p for p in world.snapshots.iterdir() if p.is_dir()]


def test_changed_ro_password_is_refused_before_it_breaks_other_connections(world: SimpleNamespace) -> None:
    world.stored_password_mismatch = ["other_db"]
    with pytest.raises(LabError, match="other_db"):
        execute(world)
    assert "ro-role" not in world.events


def test_tables_missing_from_catalog_are_reported(world: SimpleNamespace) -> None:
    world.dataset = replace(world.dataset, tables=["public.a", "public.link", "public.never_ingested"])
    report = execute(world)
    assert report["tables_missing_from_catalog"] == ["public.never_ingested"]


def run_cli(monkeypatch: pytest.MonkeyPatch, world: SimpleNamespace, tmp_path: Path) -> int:
    monkeypatch.setattr(cli, "load_dataset", lambda ref: world.dataset)
    monkeypatch.setattr(cli, "load_config", lambda path: world.cfg)
    monkeypatch.setattr(cli, "require_token", lambda role: TOKEN)
    def gateway(url: str, token: str) -> FakeGateway:
        assert url == "http://gateway"
        return world.admin

    monkeypatch.setattr(cli, "Gateway", gateway)
    monkeypatch.setattr(cli, "Psql", lambda container, user: world.psql)
    monkeypatch.setattr(cli, "SNAPSHOTS_DIR", world.snapshots)
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("LAB_RO_PASSWORD", PASSWORD)
    return cli.run(argparse.Namespace(dataset="hahlex-edu", drop=False, resnapshot=False, freeze=None))


def test_import_json_has_no_password_or_token(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert run_cli(monkeypatch, world, tmp_path) == 0
    written = next((tmp_path / "runs").glob("*/import.json")).read_text(encoding="utf-8")
    assert PASSWORD not in written and TOKEN not in written
    report = json.loads(written)
    assert report["database"] == DB
    assert report["ingest"]["embedding_rows"] == 7
    assert report["snapshots"]["catalog"]["sha256"] == "catalog-sha"
    assert report["snapshots"]["target"]["sha256"] == "target-sha"
    assert report["upstream_commit"] == "abc123"


def test_failure_exits_1_with_message_and_no_traceback(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(path: str, body: Any = None) -> Any:
        raise GatewayError(f"POST returned 422: bad password {PASSWORD}", 422)

    world.admin.post = fail
    assert run_cli(monkeypatch, world, tmp_path) == 1
    err = capsys.readouterr().err
    assert "422" in err
    assert "Traceback" not in err
    assert PASSWORD not in err
    assert not list((tmp_path / "runs").glob("*/import.json"))


def test_missing_ro_password_fails_before_any_change(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_dataset", lambda ref: world.dataset)
    monkeypatch.setattr(cli, "load_config", lambda path: world.cfg)
    monkeypatch.setattr(cli, "require_token", lambda role: TOKEN)
    monkeypatch.delenv("LAB_RO_PASSWORD", raising=False)
    assert cli.run(argparse.Namespace(dataset="hahlex-edu", drop=False, resnapshot=False, freeze=None)) == 1
    assert "LAB_RO_PASSWORD" in capsys.readouterr().err
    assert world.events == []


def test_coverage_is_printed_for_the_operator(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, world, tmp_path) == 0
    assert "Tables without a Term: public.link" in capsys.readouterr().out
