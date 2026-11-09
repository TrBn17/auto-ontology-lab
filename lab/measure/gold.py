"""Check a question bank against the database, with no LLM involved."""

from __future__ import annotations

from typing import Any

from lab.measure.ask import Execute, Question


def check_gold(questions: list[Question], execute: Execute) -> dict[str, Any]:
    """Each gold must run and return rows, and a trap's wrong query must return something else.

    A wrong variant that matches the gold means the question cannot tell the two apart, so
    getting it right would prove nothing.
    """
    problems: list[dict[str, str]] = []
    for question in questions:
        gold = execute(question.gold_sql)
        if gold.rows is None:
            problems.append({"id": question.id, "problem": f"gold failed: {gold.error}"})
            continue
        if not gold.rows:
            problems.append({"id": question.id, "problem": "gold returned no rows"})
            continue
        for variant in question.wrong_sql_variants:
            wrong = execute(variant)
            if wrong.rows is None:
                problems.append({"id": question.id, "problem": f"wrong variant failed: {wrong.error}"})
            elif wrong.rows == gold.rows:
                problems.append({"id": question.id, "problem": f"wrong variant matches gold: {variant}"})
    return {"healthy": not problems, "questions": len(questions), "problems": problems}
