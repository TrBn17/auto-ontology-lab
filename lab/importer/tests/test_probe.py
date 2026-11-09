from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import lab.ask
from lab.ask import probe_questions, run_probe, validate_sql
from lab.common.gateway import ChatAnswer, GatewayError
from lab.common.pg import Psql, quote_ident
from lab.importer.dataset import Dataset
from lab.importer.target import ensure_ro_role


def make_dataset(tables: list[str], row_counts: dict[str, int] | None) -> Dataset:
    return Dataset(
        root=Path("."),
        dataset_id="edu",
        db_name="edu",
        load_order=[],
        schemas=[],
        tables=tables,
        row_counts=row_counts,
    )


def answer(sql: str = "", error: str | None = None, information_only: bool = False) -> ChatAnswer:
    return ChatAnswer(
        sql=sql,
        response="",
        error=error,
        last_streamed_sql=None,
        information_only=information_only,
        answer={},
        events=[],
    )


class FakeGateway:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def chat(self, question: str, *, target_db: str) -> ChatAnswer:
        self.calls.append((question, target_db))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakePsql:
    """Accepts every EXPLAIN; only reached when the agent produced SQL."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.seen: list[str] = []

    def query(self, database: str, sql: str) -> list[tuple[str, ...]]:
        self.seen.append(sql)
        if self.error:
            raise lab.ask.LabError(self.error)
        return [("Seq Scan",)]


FIVE_TABLES = make_dataset(
    ["public.a", "public.b", "public.c", "public.d", "public.e", "public.f"],
    {"public.a": 1, "public.b": 50, "public.c": 50, "public.d": 7, "public.e": 90, "public.f": 3},
)


def test_questions_are_deterministic_and_use_largest_tables() -> None:
    questions = probe_questions(FIVE_TABLES)
    assert questions == [
        "Show 5 rows from public.e.",
        "Show 5 rows from public.b.",
        "Show 5 rows from public.c.",
        "Show 5 rows from public.d.",
        "Show 5 rows from public.f.",
    ]
    assert probe_questions(FIVE_TABLES) == questions


def test_questions_handle_fewer_tables_than_count() -> None:
    questions = probe_questions(make_dataset(["public.a", "public.b"], None))
    assert questions == ["Show 5 rows from public.a.", "Show 5 rows from public.b."]


@pytest.fixture
def explain_db(scratch: Psql) -> Iterator[tuple[Psql, str]]:
    name = f"p_{uuid.uuid4().hex[:12]}"
    scratch.execute("postgres", f"CREATE DATABASE {quote_ident(name)}")
    scratch.execute(name, "CREATE TABLE public.a (id int)")
    ensure_ro_role(scratch, make_named(name), "pw")
    yield Psql(scratch.container, "lab_ro"), name
    scratch.execute("postgres", f"DROP DATABASE {quote_ident(name)} WITH (FORCE)")


def make_named(name: str) -> Dataset:
    return Dataset(Path("."), "x", name, [], [], [], None)


@pytest.mark.docker
def test_valid_sql_passes_explain(explain_db: tuple[Psql, str]) -> None:
    psql, name = explain_db
    assert validate_sql(psql, name, "SELECT count(*) FROM public.a;") is None


@pytest.mark.docker
def test_invalid_sql_returns_error(explain_db: tuple[Psql, str]) -> None:
    psql, name = explain_db
    error = validate_sql(psql, name, "SELECT nope FROM public.a")
    assert error is not None and "nope" in error


@pytest.mark.docker
def test_dml_is_rejected_for_lab_ro(explain_db: tuple[Psql, str]) -> None:
    psql, name = explain_db
    assert validate_sql(psql, name, "INSERT INTO public.a VALUES (1)") is not None


def test_psql_meta_commands_never_reach_psql() -> None:
    psql = FakePsql()
    error = validate_sql(psql, "edu", "SELECT 1 \\! touch /tmp/x")  # type: ignore[arg-type]
    assert error is not None
    assert psql.seen == []


def test_abstention_counts_as_probe_failure() -> None:
    results = run_probe(FakeGateway([answer()]), FakePsql(), make_dataset(["public.a"], None))  # type: ignore[arg-type]
    assert [r.status for r in results] == ["abstained"]


def test_information_only_answer_counts_as_probe_failure() -> None:
    gateway = FakeGateway([answer(information_only=True)])
    results = run_probe(gateway, FakePsql(), make_dataset(["public.a"], None))  # type: ignore[arg-type]
    assert [r.status for r in results] == ["no_sql"]


def test_gateway_error_is_reported_as_infrastructure_not_bad_sql() -> None:
    gateway = FakeGateway([GatewayError("boom", 502)])
    results = run_probe(gateway, FakePsql(), make_dataset(["public.a"], None))  # type: ignore[arg-type]
    assert results[0].status == "infrastructure"
    assert "boom" in results[0].detail


def test_agent_error_event_is_not_bad_sql() -> None:
    gateway = FakeGateway([answer(error="agent broke")])
    results = run_probe(gateway, FakePsql(), make_dataset(["public.a"], None))  # type: ignore[arg-type]
    assert results[0].status == "agent_error"


def test_probe_targets_the_normalised_database_in_order() -> None:
    gateway = FakeGateway([answer(sql="SELECT 1"), answer(sql="SELECT 2")])
    dataset = replace(make_dataset(["public.a", "public.b"], None), dataset_id="hahlex-edu", db_name="hahlex_edu")
    results = run_probe(gateway, FakePsql(), dataset)  # type: ignore[arg-type]
    assert [r.status for r in results] == ["valid", "valid"]
    assert gateway.calls == [
        ("Show 5 rows from public.a.", "hahlex_edu"),
        ("Show 5 rows from public.b.", "hahlex_edu"),
    ]


def _run_cli(monkeypatch: pytest.MonkeyPatch, replies: list[Any], dataset: Dataset) -> int:
    class Cfg:
        gateway_url = "http://gateway"

        class containers:
            postgres = "postgres"

    monkeypatch.setattr(lab.ask, "load_config", lambda path=None: Cfg)
    monkeypatch.setattr(lab.ask, "require_token", lambda role: "auto_ontology_x")
    monkeypatch.setattr(lab.ask, "load_dataset", lambda ref: dataset)
    monkeypatch.setattr(lab.ask, "Gateway", lambda url, token, source: FakeGateway(replies))
    monkeypatch.setattr(lab.ask, "Psql", lambda container, user: FakePsql())
    return lab.ask.run(argparse.Namespace(dataset="edu", probe=True, freeze=None))


def test_exit_code_nonzero_unless_all_five_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    good = [answer(sql="SELECT 1") for _ in range(5)]
    assert _run_cli(monkeypatch, good, FIVE_TABLES) == 0
    assert _run_cli(monkeypatch, [*good[:4], answer()], FIVE_TABLES) == 1


def test_exit_code_zero_when_every_question_of_a_small_dataset_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = make_dataset(["public.a", "public.b"], None)
    assert _run_cli(monkeypatch, [answer(sql="SELECT 1"), answer(sql="SELECT 1")], dataset) == 0


def test_without_probe_flag_names_the_missing_step(capsys: pytest.CaptureFixture[str]) -> None:
    assert lab.ask.run(argparse.Namespace(dataset="edu", probe=False, freeze=None)) == 1
    assert "--probe" in capsys.readouterr().err
