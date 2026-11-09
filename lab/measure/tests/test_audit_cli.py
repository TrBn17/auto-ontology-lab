from __future__ import annotations

import argparse
import json

import pytest

from lab.common.errors import LabError
from lab.common.gateway import ChatAnswer, GatewayError
from lab.measure import cli
from lab.measure.audit import classify_unanswerable, pii_findings, retry_infrastructure, run_unanswerable
from lab.measure.scoring import ExecResult


def _answer(sql="", error=None, info=False):
    return ChatAnswer(sql, "r", error, None, info, {}, [])


@pytest.mark.parametrize(
    ("answer", "result", "expected"),
    [
        (_answer(), None, "abstained"),
        (_answer(info=True), None, "information_answer"),
        (_answer(error="boom"), None, "agent_error"),
        (_answer("SELECT 1"), ExecResult({("1",)}, None), "invented_sql_executes"),
        (_answer("SELECT nope"), ExecResult(None, "err"), "invented_sql_fails"),
    ],
)
def test_classify_unanswerable(answer, result, expected) -> None:
    assert classify_unanswerable(answer, result) == expected


def test_unanswerable_records_coverage_and_counts_outcomes() -> None:
    class Viewer:
        def post(self, path, body):
            assert path == "/api/question-entity-coverage"
            return {"data": {"coverage": 0.25}}

        def chat(self, question, *, target_db, evidence=None):
            if question == "broken":
                raise GatewayError("502", 502)
            return _answer("SELECT 1")

    result = run_unanswerable(Viewer(), ["a", "broken"], "edu", lambda sql: ExecResult({("1",)}, None))
    assert result["counts"] == {"invented_sql_executes": 1, "infrastructure": 1}
    assert result["questions"][0]["coverage"] == 0.25
    assert result["healthy"] is False


def test_retry_infrastructure_reasks_only_failed_rows_and_keeps_order() -> None:
    asked = []

    class Viewer:
        def post(self, path, body):
            return {"data": {"coverage": None}}

        def chat(self, question, *, target_db, evidence=None):
            asked.append(question)
            return _answer()

    previous = {
        "questions": [
            {"question": "a", "outcome": "invented_sql_executes", "sql": "SELECT 1", "coverage": None, "detail": ""},
            {"question": "b", "outcome": "infrastructure", "sql": "", "coverage": None, "detail": "timeout"},
        ]
    }
    result = retry_infrastructure(previous, Viewer(), "edu", lambda sql: ExecResult({("1",)}, None))
    assert asked == ["b"]
    assert [row["outcome"] for row in result["questions"]] == ["invented_sql_executes", "abstained"]
    assert result["retried"] == ["b"]
    assert result["healthy"] is True


def test_pii_findings_report_columns_with_raw_samples_and_vector_hits() -> None:
    samples = [("public.student.email", '["a@example.test", "b@example.test"]'), ("public.student.note", "[]"), ("public.student.phone", "not json")]
    found = pii_findings(samples, lambda value: 2 if value == "a@example.test" else 0)
    assert found == [{"column": "public.student.email", "raw_samples": 2, "vector_rows_containing_a_sample": 2}]


def test_patch_refuses_until_it_exists() -> None:
    args = argparse.Namespace(patch=True, dataset="x", only=None, runs=3, limit=None, mutation=None, modes=None, freeze=None, questions=None)
    with pytest.raises(LabError, match="vanilla"):
        cli.run(args)


def test_exit_code_reports_unhealthy_runs_separately() -> None:
    assert cli.exit_code({"discover": {"healthy": True}}) == 0
    assert cli.exit_code({"discover": {"healthy": True}, "ask": {"healthy": False}}) == cli.EXIT_UNHEALTHY
    assert cli.EXIT_UNHEALTHY == 3


def test_questions_digest_changes_with_content(tmp_path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps([1]), encoding="utf-8")
    first = cli.file_digest(path)
    path.write_text(json.dumps([2]), encoding="utf-8")
    assert cli.file_digest(path) != first
