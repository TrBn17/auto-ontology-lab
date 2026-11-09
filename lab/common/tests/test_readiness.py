from __future__ import annotations

from collections.abc import Callable

import pytest

from lab.common.readiness import (
    CompileAbandoned,
    CompileFailed,
    CompileNotStarted,
    CompileRun,
    IngestFailed,
    ReadinessTimeout,
    wait_for_compile,
    ingest_state,
    wait_for_ingest,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _ingest(clock: FakeClock, read_logs: Callable[[], str], database: str = "edu", **kw):
    return wait_for_ingest(read_logs, database, sleep=clock.sleep, monotonic=clock.monotonic, **kw)


# Real line format observed from the ingestion container at the pinned stack.
FINISHED = (
    "2026-11-06 20:52:28,877 INFO ingestion_service.ingest: Data ingestion finished successfully "
    "for database edu — 45 table(s), 487 column(s), 532 embedding row(s) in 12.8s"
)
STARTED = "2026-11-06 20:52:16,035 INFO ingestion_service.ingest: Data ingestion started for database edu"


def test_ingest_returns_parsed_counts_when_finished() -> None:
    result = _ingest(FakeClock(), lambda: f"{STARTED}\n{FINISHED}\n")
    assert (result.tables, result.columns, result.embedding_rows) == (45, 487, 532)
    assert result.seconds == pytest.approx(12.8)


def test_ingest_waits_through_started_until_finished() -> None:
    clock = FakeClock()
    logs = iter([f"{STARTED}\n", f"{STARTED}\n", f"{STARTED}\n{FINISHED}\n"])
    result = _ingest(clock, lambda: next(logs), poll=5)
    assert result.tables == 45
    assert clock.t == 10


def test_ingest_ignores_other_databases_sharing_a_prefix() -> None:
    other = FINISHED.replace("database edu ", "database edu_archive ")
    clock = FakeClock()
    with pytest.raises(IngestFailed, match="never started"):
        _ingest(clock, lambda: other, start_grace=30, poll=5)


@pytest.mark.parametrize(
    "line",
    [
        "ERROR x: Background ingest failed for database edu",
        "ERROR x: Ingest skipped: no loaded connector for database edu",
    ],
)
def test_ingest_failure_lines_raise_immediately(line: str) -> None:
    clock = FakeClock()
    with pytest.raises(IngestFailed):
        _ingest(clock, lambda: f"{STARTED}\n{line}\n")
    assert clock.t == 0


def test_ingest_never_started_fails_at_grace_because_trigger_is_best_effort() -> None:
    clock = FakeClock()
    with pytest.raises(IngestFailed, match="never started"):
        _ingest(clock, lambda: "", start_grace=30, poll=5)
    assert clock.t == 30


def test_ingest_stuck_after_start_times_out() -> None:
    clock = FakeClock()
    with pytest.raises(ReadinessTimeout):
        _ingest(clock, lambda: STARTED, timeout=60, poll=5)


def _run(status: str | None, run_id: int = 3) -> CompileRun:
    return CompileRun(id=run_id, status=status, started_at="s", finished_at="f" if status else None)


def _compile(clock: FakeClock, fetch: Callable[[int], CompileRun | None], running: Callable[[], bool], **kw):
    return wait_for_compile(fetch, running, baseline_id=2, sleep=clock.sleep, monotonic=clock.monotonic, **kw)


def test_compile_succeeds_when_a_row_newer_than_baseline_succeeds() -> None:
    clock = FakeClock()
    seen: list[int] = []

    def fetch(baseline: int) -> CompileRun | None:
        seen.append(baseline)
        return _run("succeeded")

    assert _compile(clock, fetch, lambda: False).id == 3
    assert seen == [2]


def test_compile_does_not_complete_on_old_success_while_waiting_for_new_row() -> None:
    clock = FakeClock()
    rows = iter([None, None, _run(None), _run("succeeded")])
    assert _compile(clock, lambda b: next(rows), lambda: True, poll=10).status == "succeeded"
    assert clock.t == 30


def test_compile_failed_row_raises() -> None:
    with pytest.raises(CompileFailed):
        _compile(FakeClock(), lambda b: _run("failed"), lambda: False)


def test_compile_no_new_row_within_grace_raises_not_started() -> None:
    # The PUT/trigger path swallows errors, and a disabled flag or zero connections writes no row.
    clock = FakeClock()
    with pytest.raises(CompileNotStarted):
        _compile(clock, lambda b: None, lambda: False, start_grace=60, poll=10)
    assert clock.t == 60


def test_compile_null_row_while_idle_is_abandoned_not_waited_on_forever() -> None:
    # status stays NULL forever after a stop or crash; `running` also degrades to False if ingestion is down.
    clock = FakeClock()
    with pytest.raises(CompileAbandoned):
        _compile(clock, lambda b: _run(None), lambda: False, abandon_after=30, poll=10, timeout=9999)
    assert clock.t == 30


def test_compile_transient_idle_blip_does_not_abandon() -> None:
    clock = FakeClock()
    states = iter([False, False, True, False, True])
    rows = iter([_run(None)] * 5 + [_run("succeeded")])
    run = _compile(clock, lambda b: next(rows), lambda: next(states), abandon_after=30, poll=10, timeout=9999)
    assert run.status == "succeeded"


def test_compile_long_running_pass_times_out() -> None:
    clock = FakeClock()
    with pytest.raises(ReadinessTimeout):
        _compile(clock, lambda b: _run(None), lambda: True, timeout=100, poll=10)


def test_finished_line_without_a_started_line_is_ignored() -> None:
    """A thread left over from an earlier run must not end the wait for the new ingest."""
    clock = iter(range(0, 1000, 60))
    with pytest.raises(IngestFailed, match="never started"):
        wait_for_ingest(lambda: FINISHED, "edu", start_grace=120.0, sleep=lambda _: None, monotonic=lambda: next(clock))


def test_ingest_state_follows_the_last_line() -> None:
    assert ingest_state("", "edu") == "none"
    assert ingest_state(STARTED, "edu") == "running"
    assert ingest_state(f"{STARTED}\n{FINISHED}", "edu") == "finished"
    assert ingest_state(f"{STARTED}\n{FINISHED}\n{STARTED}", "edu") == "running"
    assert ingest_state(f"{STARTED}\nBackground ingest failed for database edu", "edu") == "failed"
    assert ingest_state(f"{STARTED}\n{FINISHED}".replace("database edu", "database edu_archive"), "edu") == "none"
