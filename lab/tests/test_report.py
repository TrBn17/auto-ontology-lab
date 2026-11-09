from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab.report import bootstrap_ci, majority, mcnemar_exact, per_question, percentile, render, summarize_mode


def _row(mode: str, run: int, qid: str, outcome: str, category: str = "simple", seconds: float = 1.0) -> dict:
    return {"mode": mode, "run": run, "id": qid, "category": category, "question": f"q {qid}",
            "gold_sql": "SELECT 1", "sql": "SELECT 2", "outcome": outcome, "detail": "", "seconds": seconds}


def test_mcnemar_exact_matches_the_binomial() -> None:
    a = {str(i): True for i in range(6)}
    b = {str(i): False for i in range(6)}
    t = mcnemar_exact(a, b)
    assert (t["only_a"], t["only_b"]) == (6, 0)
    assert t["p"] == pytest.approx(2 / 64)
    assert mcnemar_exact({"x": True}, {"x": True})["p"] == 1.0


def test_bootstrap_interval_contains_the_mean_and_is_deterministic() -> None:
    scores = [1.0] * 7 + [0.0] * 3
    lo, hi = bootstrap_ci(scores, resamples=2000)
    assert lo < 0.7 < hi
    assert bootstrap_ci(scores, resamples=2000) == (lo, hi)
    assert bootstrap_ci([1.0] * 5, resamples=100) == (1.0, 1.0)


def test_per_question_ignores_infrastructure_and_majority_needs_more_than_half() -> None:
    rows = [_row("g", 1, "a", "correct"), _row("g", 0, "a", "wrong_result"), _row("g", 2, "a", "infrastructure")]
    assert per_question(rows) == {"a": [False, True]}
    assert majority([True, True, False]) and not majority([True, False])


def test_summary_flags_missing_results_and_reports_latency() -> None:
    rows = [_row("g", r, q, "correct", seconds=s) for r in range(3) for q, s in (("a", 1.0), ("b", 9.0))]
    m = summarize_mode(rows, 3)
    assert m["healthy"] and m["accuracy_mean"] == 1.0 and m["flipped"] == []
    assert (m["p50"], m["p95"]) == (1.0, 9.0)
    assert not summarize_mode(rows[:-1], 3)["healthy"]
    assert percentile([], 0.5) is None


def test_render_splits_soft_delete_and_lists_gateway_failures(tmp_path: Path) -> None:
    run = tmp_path / "20260101T000000Z"
    run.mkdir()
    rows = []
    for r in range(3):
        rows += [_row("gateway", r, "x01", "wrong_result", "soft_delete"), _row("gateway", r, "x04", "correct", "soft_delete")]
        rows += [_row("ddl", r, "x01", "wrong_result", "soft_delete"), _row("ddl", r, "x04", "wrong_result", "soft_delete")]
    (run / "ask.jsonl").write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    text, healthy = render([run], {"implicit_filter": ["x01"]})
    assert healthy
    assert "soft_delete (implicit)" in text and "soft_delete (explicit)" in text
    assert "**x01**" in text and "**x04**" not in text
    assert "| `ddl` | 2 | 1 | 0 |" in text


def test_evolve_takes_the_newest_result_of_each_mutation(tmp_path: Path) -> None:
    def write(run: str, healthy: bool) -> Path:
        d = tmp_path / run
        d.mkdir()
        stage = {"stage": "t2", "seconds": 5, "reflected": False, "edits_surviving": 6, "edits_total": 6,
                 "probes_regressed": None, "unexpected_relationships": [],
                 "assertions": [{"kind": "attribute_on_column", "column": "t.c", "passed": False}]}
        result = {"id": "m01", "title": "Rename", "healthy": healthy, "error": None if healthy else "t3: 404",
                  "baseline_probes": [{"id": "a", "outcome": "correct"}], "stages": [stage]}
        (d / "evolve_m01.json").write_text(json.dumps(result), encoding="utf-8")
        return d

    old, new = write("20260101T000000Z", False), write("20260102T000000Z", True)
    text, healthy = render([old, new], {})
    assert healthy and "t3: 404" not in text
    assert "✗ 0/1; edits 6/6; regressed —; 5s" in text and "| not run | not run |" in text
    assert "`attribute_on_column` t.c" in text

    rescored, _ = render([new], {}, {"m01": set()})
    assert "✓ 0/0" in rescored and "removed from its spec" in rescored


def test_render_without_gateway_is_not_healthy(tmp_path: Path) -> None:
    run = tmp_path / "r"
    run.mkdir()
    (run / "ask.jsonl").write_text(json.dumps(_row("ddl", 0, "a", "correct")), encoding="utf-8")
    text, healthy = render([run], {})
    assert not healthy and "Not a headline result" in text
