"""Text-to-SQL suite: run questions through each mode and score the results."""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from lab.common.errors import LabError
from lab.common.gateway import Gateway
from lab.measure.llm import LlmClient, build_prompt, extract_sql
from lab.measure.scoring import ExecResult, score

MAX_INFRASTRUCTURE_RATE = 0.05
_REQUIRED = ("id", "category", "question", "gold_sql")


@dataclass(frozen=True)
class Question:
    id: str
    category: str
    question: str
    gold_sql: str
    evidence: str | None = None
    wrong_sql_variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class Generated:
    """What a mode produced. `infrastructure` means the question was not really asked."""

    status: str
    sql: str = ""
    detail: str = ""


Generator = Callable[[Question], Generated]
Execute = Callable[[str], ExecResult]


def load_questions(path: Path) -> list[Question]:
    if not path.is_file():
        raise LabError(f"{path} not found; every question needs an executable gold_sql")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LabError(f"{path} is not valid JSON: {exc}") from exc
    questions: list[Question] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        missing = [key for key in _REQUIRED if not item.get(key)]
        if missing:
            raise LabError(f"{path}: question {index} is missing {', '.join(missing)}")
        if item["id"] in seen:
            raise LabError(f"{path}: duplicate question id {item['id']!r}")
        seen.add(item["id"])
        questions.append(
            Question(
                item["id"],
                item["category"],
                item["question"],
                item["gold_sql"],
                item.get("evidence"),
                tuple(item.get("wrong_sql_variants") or ()),
            )
        )
    return questions


def gateway_generator(viewer: Gateway, database: str) -> Generator:
    """The production path: one stateless chat turn per question."""

    def generate(question: Question) -> Generated:
        try:
            answer = viewer.chat(question.question, target_db=database, evidence=question.evidence)
        except (LabError, httpx.HTTPError) as exc:
            return Generated("infrastructure", "", str(exc))
        if answer.error is not None:
            return Generated("agent_error", answer.sql or answer.last_streamed_sql or "", answer.error)
        if answer.information_only:
            return Generated("no_sql")
        if answer.abstained:
            return Generated("abstained")
        return Generated("generated", answer.sql, "")

    return generate


def llm_generator(client: LlmClient, schema: str) -> Generator:
    """A baseline mode: one direct LLM call given only the schema text."""

    def generate(question: Question) -> Generated:
        try:
            reply = client.complete(build_prompt(schema, question.question, question.evidence))
        except (LabError, httpx.HTTPError) as exc:
            return Generated("infrastructure", "", str(exc))
        sql = extract_sql(reply)
        return Generated("generated", sql, "") if sql else Generated("no_sql")

    return generate


def run_ask(
    questions: list[Question],
    modes: dict[str, Generator],
    runs: int,
    execute: Execute,
    sink: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Ask every usable question in every mode and run. Each row goes to sink as soon as it exists."""
    usable: list[tuple[Question, ExecResult]] = []
    excluded: list[dict[str, str]] = []
    for question in questions:
        gold = execute(question.gold_sql)
        if gold.rows is None:
            excluded.append({"id": question.id, "reason": f"gold failed: {gold.error}"})
        elif not gold.rows:
            # Any empty or failed prediction would match, so it measures nothing.
            excluded.append({"id": question.id, "reason": "gold returned no rows"})
        else:
            usable.append((question, gold))

    rows: list[dict[str, Any]] = []
    for mode, generate in modes.items():
        for run in range(runs):
            for question, gold in usable:
                began = time.monotonic()
                generated = generate(question)
                seconds = round(time.monotonic() - began, 1)
                row = {**_score_one(mode, run, question, gold, generated, execute), "seconds": seconds}
                rows.append(row)
                sink(row)
    return {"rows": rows, "excluded": excluded, "usable": len(usable)}


def summarize(rows: list[dict[str, Any]], excluded: list[dict[str, str]], *, usable: int, runs: int) -> dict[str, Any]:
    modes = {}
    for mode in sorted({row["mode"] for row in rows}):
        modes[mode] = _summarize_mode([row for row in rows if row["mode"] == mode], usable=usable, runs=runs)
    return {
        "healthy": bool(modes) and all(mode["healthy"] for mode in modes.values()),
        "usable_questions": usable,
        "excluded_questions": excluded,
        "runs": runs,
        "modes": modes,
    }


def _score_one(
    mode: str, run: int, question: Question, gold: ExecResult, generated: Generated, execute: Execute
) -> dict[str, Any]:
    outcome = generated.status
    detail = generated.detail
    if generated.status == "generated":
        predicted = execute(generated.sql)
        outcome = score(gold, predicted)
        detail = predicted.error or ""
    return {
        "mode": mode,
        "run": run,
        "id": question.id,
        "category": question.category,
        "question": question.question,
        "gold_sql": question.gold_sql,
        "sql": generated.sql,
        "outcome": outcome,
        "detail": detail,
    }


def _summarize_mode(rows: list[dict[str, Any]], *, usable: int, runs: int) -> dict[str, Any]:
    expected = usable * runs
    infrastructure = sum(row["outcome"] == "infrastructure" for row in rows)
    healthy = expected > 0 and len(rows) == expected and infrastructure / expected <= MAX_INFRASTRUCTURE_RATE
    scored = [row for row in rows if row["outcome"] != "infrastructure"]
    flipped = _flipped(scored)
    result: dict[str, Any] = {
        "healthy": healthy,
        "seconds_per_question": _latency(rows),
        "expected_results": expected,
        "results": len(rows),
        "infrastructure_failures": infrastructure,
        "outcomes": dict(sorted(Counter(row["outcome"] for row in scored if row["outcome"] != "correct").items())),
        "flipped_questions": flipped,
        "flip_rate": len(flipped) / usable if usable else None,
        "accuracy_by_run": None,
        "accuracy_mean": None,
        "accuracy_std": None,
        "by_category": None,
    }
    if healthy:
        # No headline from a broken run: a rate-limited run looks exactly like a weak model.
        result.update(_accuracy(scored, runs))
        result["by_category"] = {
            category: _accuracy([row for row in scored if row["category"] == category], runs)
            for category in sorted({row["category"] for row in scored})
        }
    return result


def _latency(rows: list[dict[str, Any]]) -> dict[str, float] | None:
    seconds = sorted(row["seconds"] for row in rows if "seconds" in row)
    if not seconds:
        return None
    return {"p50": seconds[(len(seconds) - 1) // 2], "p95": seconds[math.ceil(len(seconds) * 0.95) - 1]}


def _accuracy(rows: list[dict[str, Any]], runs: int) -> dict[str, Any]:
    by_run = []
    for run in range(runs):
        in_run = [row for row in rows if row["run"] == run]
        if in_run:
            by_run.append(sum(row["outcome"] == "correct" for row in in_run) / len(in_run))
    return {
        "accuracy_by_run": by_run,
        "accuracy_mean": statistics.fmean(by_run) if by_run else None,
        "accuracy_std": statistics.stdev(by_run) if len(by_run) > 1 else 0.0,
    }


def _flipped(rows: list[dict[str, Any]]) -> list[str]:
    results: dict[str, set[bool]] = {}
    for row in rows:
        results.setdefault(row["id"], set()).add(row["outcome"] == "correct")
    return sorted(question for question, seen in results.items() if len(seen) > 1)
