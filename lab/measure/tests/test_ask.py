from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from lab.common.errors import LabError
from lab.common.gateway import ChatAnswer, GatewayError
from lab.common.pg import Psql
from lab.measure.ask import (
    Generated,
    Question,
    gateway_generator,
    llm_generator,
    load_questions,
    run_ask,
    summarize,
)
from lab.measure.llm import LlmClient, extract_sql
from lab.measure.scoring import ExecResult, execute, normalize_row, score


def _q(qid: str, category: str = "simple", gold: str = "GOLD") -> Question:
    return Question(id=qid, category=category, question=f"q {qid}", gold_sql=gold)


def _exec(mapping):
    def run(sql: str) -> ExecResult:
        value = mapping.get(sql, ExecResult(None, "no such query"))
        return value if isinstance(value, ExecResult) else ExecResult(value, None)

    return run


def test_normalize_treats_equal_numbers_alike_but_keeps_text() -> None:
    assert normalize_row(("1.0", "abc")) == normalize_row(("1", "abc"))
    assert normalize_row(("0.3333333", "x")) == normalize_row(("0.33333333", "x"))
    assert normalize_row(("0.33", "x")) != normalize_row(("0.34", "x"))
    assert normalize_row(("NaN",)) == ("NaN",)
    assert normalize_row(("007",)) != normalize_row(("7a",))


def test_score_uses_set_comparison_like_the_upstream_harness() -> None:
    assert score(ExecResult({("1",)}, None), ExecResult({("1",)}, None)) == "correct"
    assert score(ExecResult({("1",)}, None), ExecResult({("2",)}, None)) == "wrong_result"
    # Extra columns fail, matching auto-ontology-eval's scorer.
    assert score(ExecResult({("1",)}, None), ExecResult({("1", "x")}, None)) == "wrong_result"
    assert score(ExecResult({("1",)}, None), ExecResult(None, "boom")) == "sql_error"


def test_load_questions_validates(tmp_path: Path) -> None:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps([{"id": "a", "category": "simple", "question": "q", "gold_sql": "SELECT 1"}]), encoding="utf-8")
    assert load_questions(path)[0].id == "a"

    path.write_text(json.dumps([{"id": "a", "category": "simple", "question": "q"}]), encoding="utf-8")
    with pytest.raises(LabError, match="gold_sql"):
        load_questions(path)

    row = {"id": "a", "category": "simple", "question": "q", "gold_sql": "S"}
    path.write_text(json.dumps([row, row]), encoding="utf-8")
    with pytest.raises(LabError, match="duplicate"):
        load_questions(path)


def test_gold_that_fails_or_is_empty_is_excluded_not_scored() -> None:
    questions = [_q("ok"), _q("empty", gold="EMPTY"), _q("bad", gold="BAD")]
    run = _exec({"GOLD": {("1",)}, "EMPTY": set(), "PRED": {("1",)}})
    results = run_ask(questions, {"gateway": lambda q: Generated("generated", "PRED")}, 1, run, lambda r: None)
    summary = summarize(results["rows"], results["excluded"], usable=1, runs=1)
    assert {e["id"]: e["reason"] for e in results["excluded"]} == {"empty": "gold returned no rows", "bad": "gold failed: no such query"}
    assert summary["modes"]["gateway"]["accuracy_mean"] == 1.0


def test_gold_is_checked_once_and_every_row_reaches_the_sink() -> None:
    calls: list[str] = []

    def run(sql: str) -> ExecResult:
        calls.append(sql)
        return ExecResult({("1",)}, None)

    sunk: list[dict] = []
    run_ask([_q("a")], {"gateway": lambda q: Generated("generated", "PRED")}, 3, run, sunk.append)
    assert calls.count("GOLD") == 1
    assert [row["run"] for row in sunk] == [0, 1, 2]


def test_accuracy_std_and_flips_across_runs() -> None:
    # Runs go in order and each asks both questions: run 0 gets "flip" wrong and "stable" right.
    outcomes = iter(["PRED_WRONG", "PRED_RIGHT", "PRED_RIGHT", "PRED_RIGHT", "PRED_RIGHT", "PRED_RIGHT"])
    run = _exec({"GOLD": {("1",)}, "PRED_RIGHT": {("1",)}, "PRED_WRONG": {("2",)}})
    results = run_ask([_q("flip"), _q("stable", "join")], {"gateway": lambda q: Generated("generated", next(outcomes))}, 3, run, lambda r: None)
    mode = summarize(results["rows"], results["excluded"], usable=2, runs=3)["modes"]["gateway"]
    assert mode["accuracy_by_run"] == [0.5, 1.0, 1.0]
    assert mode["accuracy_mean"] == pytest.approx(5 / 6)
    assert mode["accuracy_std"] > 0
    assert mode["flipped_questions"] == ["flip"]
    assert mode["flip_rate"] == 0.5
    assert mode["by_category"]["join"]["accuracy_mean"] == 1.0


def test_statuses_are_counted_as_failures_of_their_own_kind() -> None:
    gens = iter([Generated("abstained", ""), Generated("agent_error", "x", "boom"), Generated("no_sql", ""), Generated("generated", "BAD")])
    run = _exec({"GOLD": {("1",)}})
    questions = [_q(str(i)) for i in range(4)]
    results = run_ask(questions, {"gateway": lambda q: next(gens)}, 1, run, lambda r: None)
    mode = summarize(results["rows"], results["excluded"], usable=4, runs=1)["modes"]["gateway"]
    assert mode["outcomes"] == {"abstained": 1, "agent_error": 1, "no_sql": 1, "sql_error": 1}
    assert mode["accuracy_mean"] == 0.0


def test_infrastructure_failures_trip_the_health_check_and_hide_the_headline() -> None:
    gens = iter([Generated("infrastructure", "", "429")] + [Generated("generated", "OK")] * 9)
    run = _exec({"GOLD": {("1",)}, "OK": {("1",)}})
    questions = [_q(str(i)) for i in range(10)]
    results = run_ask(questions, {"gateway": lambda q: next(gens)}, 1, run, lambda r: None)
    summary = summarize(results["rows"], results["excluded"], usable=10, runs=1)
    mode = summary["modes"]["gateway"]
    assert mode["healthy"] is False
    assert mode["accuracy_mean"] is None
    assert summary["healthy"] is False


def test_infrastructure_within_limit_is_excluded_from_the_denominator() -> None:
    gens = iter([Generated("infrastructure", "", "429")] + [Generated("generated", "OK")] * 19 + [])
    run = _exec({"GOLD": {("1",)}, "OK": {("1",)}})
    questions = [_q(str(i)) for i in range(20)]
    results = run_ask(questions, {"gateway": lambda q: next(gens)}, 1, run, lambda r: None)
    mode = summarize(results["rows"], results["excluded"], usable=20, runs=1)["modes"]["gateway"]
    assert mode["healthy"] is True
    assert mode["accuracy_mean"] == 1.0


def test_missing_results_trip_the_health_check() -> None:
    run = _exec({"GOLD": {("1",)}, "OK": {("1",)}})
    results = run_ask([_q("a")], {"gateway": lambda q: Generated("generated", "OK")}, 1, run, lambda r: None)
    assert summarize(results["rows"], results["excluded"], usable=2, runs=1)["healthy"] is False


def test_one_infrastructure_exception_does_not_stop_the_run() -> None:
    class Viewer:
        def __init__(self):
            self.n = 0

        def chat(self, question, *, target_db, evidence=None):
            self.n += 1
            if self.n == 1:
                raise GatewayError("502", 502)
            if self.n == 2:
                raise httpx.ReadTimeout("slow")
            return ChatAnswer("SELECT 1", "r", None, None, False, {}, [])

    gen = gateway_generator(Viewer(), "edu")
    assert gen(_q("a")).status == "infrastructure"
    assert gen(_q("b")).status == "infrastructure"
    assert gen(_q("c")) == Generated("generated", "SELECT 1", "")


def test_gateway_generator_classifies_non_sql_answers() -> None:
    def viewer_with(answer):
        class Viewer:
            def chat(self, question, *, target_db, evidence=None):
                return answer

        return Viewer()

    abstain = ChatAnswer("", "no", None, None, False, {}, [])
    info = ChatAnswer("", "text", None, None, True, {}, [])
    err = ChatAnswer("", "", "boom", "SELECT broken", False, {}, [])
    assert gateway_generator(viewer_with(abstain), "edu")(_q("a")).status == "abstained"
    assert gateway_generator(viewer_with(info), "edu")(_q("a")).status == "no_sql"
    failed = gateway_generator(viewer_with(err), "edu")(_q("a"))
    assert (failed.status, failed.sql) == ("agent_error", "SELECT broken")


@pytest.mark.parametrize(
    ("text", "sql"),
    [
        ("SELECT 1", "SELECT 1"),
        ("Here:\n```sql\nSELECT 2;\n```\nDone", "SELECT 2"),
        ("```\nSELECT 3\n```", "SELECT 3"),
        ("first\n```sql\nSELECT 4\n```\nthen\n```sql\nSELECT 5\n```", "SELECT 5"),
    ],
)
def test_extract_sql(text: str, sql: str) -> None:
    assert extract_sql(text) == sql


def test_llm_client_sends_schema_question_and_fixed_temperature() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "```sql\nSELECT 1\n```"}}]})

    client = LlmClient("http://llm/v1", "model-x", "key", transport=httpx.MockTransport(handler))
    gen = llm_generator(client, "CREATE TABLE t (id int);")
    assert gen(Question("a", "simple", "how many?", "G", evidence="hint")) == Generated("generated", "SELECT 1", "")
    assert seen["auth"] == "Bearer key"
    assert seen["body"]["model"] == "model-x"
    assert seen["body"]["temperature"] == 0
    content = seen["body"]["messages"][-1]["content"]
    assert "CREATE TABLE t" in content and "how many?" in content and "hint" in content


def test_llm_client_waits_out_rate_limits() -> None:
    replies = [
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(429),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
    ]
    waits: list[float] = []
    client = LlmClient(
        "http://llm/v1", "m", "k", transport=httpx.MockTransport(lambda r: replies.pop(0)), sleep=waits.append
    )
    assert client.complete("q") == "ok"
    assert 7.0 in waits and 10.0 in waits


def test_llm_client_gives_up_after_repeated_rate_limits() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="slow down")

    client = LlmClient("http://llm/v1", "m", "k", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(LabError, match="429"):
        client.complete("q")
    assert len(calls) == 6


def test_llm_errors_are_infrastructure_and_hide_the_key() -> None:
    client = LlmClient("http://llm/v1", "m", "secret-key", transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")))
    result = llm_generator(client, "ddl")(_q("a"))
    assert result.status == "infrastructure"
    assert "429" in result.detail and "secret-key" not in result.detail


@pytest.mark.docker
def test_execute_runs_read_only_and_reports_errors(scratch: Psql) -> None:
    assert execute(scratch, "postgres", "SELECT 1, 'x'").rows == {(normalize_row(("1", "x")))}
    assert execute(scratch, "postgres", "SELECT * FROM nope").error is not None


def test_execute_rejects_psql_meta_commands_without_running_them() -> None:
    class Never:
        def query(self, *a):
            raise AssertionError("must not run")

    assert "backslash" in execute(Never(), "db", "SELECT 1; \\! whoami").error
