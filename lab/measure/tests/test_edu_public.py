"""The public dataset's hand-written files must agree with each other and with its schema."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from lab.common.config import REPO_ROOT
from lab.common.pg import Psql
from lab.measure.ask import Question, load_questions
from lab.measure.evolve import load_evolve_spec
from lab.measure.gold import check_gold
from lab.measure.scoring import ExecResult, execute

ROOT = REPO_ROOT / "datasets" / "edu-public"
CATEGORIES = {"simple", "join", "metric", "ambiguous_date", "soft_delete", "no_fk_join"}
_TABLE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\);", re.DOTALL)
_COLUMN = re.compile(r"^\s+(\w+) ", re.MULTILINE)
_CONSTRAINT_WORDS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}


def _schema() -> dict[str, set[str]]:
    text = (ROOT / "source" / "schema.sql").read_text(encoding="utf-8")
    return {
        name: {column for column in _COLUMN.findall(body) if column not in _CONSTRAINT_WORDS}
        for name, body in _TABLE.findall(text)
    }


def test_schema_has_the_planned_size_and_traps() -> None:
    schema = _schema()
    assert len(schema) == 15
    assert {"enrolled_at", "created_at"} <= schema["enrollment"]
    assert "rec_sts" in schema["std_mst"]
    assert "ref_code" in schema["payment"]
    text = (ROOT / "source" / "schema.sql").read_text(encoding="utf-8")
    payment_body = dict(_TABLE.findall(text))["payment"]
    assert "REFERENCES" not in payment_body  # The join to invoice must stay undeclared.


def test_sixty_questions_ten_per_category() -> None:
    questions = load_questions(ROOT / "questions.json")
    assert len(questions) == 60
    assert Counter(q.category for q in questions) == {category: 10 for category in CATEGORIES}


def test_gold_only_names_tables_in_the_schema() -> None:
    schema = _schema()
    for question in load_questions(ROOT / "questions.json"):
        for sql in (question.gold_sql, *question.wrong_sql_variants):
            # extract(month FROM col) names a column, not a table.
            without_extract = re.sub(r"extract\(\w+ FROM \w+\)", "", sql)
            for table in re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", without_extract):
                if table != "x":
                    assert table in schema, f"{question.id}: {table}"


def test_clone_allowlist_names_real_columns() -> None:
    schema = _schema()
    text = (ROOT / "clone.yaml").read_text(encoding="utf-8")
    columns = re.findall(r"- public\.(\w+)\.(\w+)", text)
    assert columns
    for table, column in columns:
        assert column in schema[table]


def test_metadata_covers_every_column() -> None:
    schema = _schema()
    metadata = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    assert set(metadata) == set(schema)
    for table, columns in schema.items():
        assert set(metadata[table]["columns"]) == columns, table


def test_evolve_spec_refers_to_real_questions_and_columns() -> None:
    spec, mutations = load_evolve_spec(ROOT)
    ids = {q.id for q in load_questions(ROOT / "questions.json")}
    schema = _schema()
    assert len(spec.probes) == 10 and set(spec.probes) <= ids
    assert [m.id for m in mutations] == [f"m0{i}" for i in range(1, 9)]
    assert len(spec.edits["certify_terms"]) == 3
    for table in spec.edits["certify_terms"]:
        assert table.removeprefix("public.") in schema
    for mutation in mutations:
        assert set(mutation.gold_overrides) <= set(spec.probes), mutation.id


def test_probes_touching_a_renamed_column_have_a_gold_override() -> None:
    spec, mutations = load_evolve_spec(ROOT)
    questions = {q.id: q for q in load_questions(ROOT / "questions.json")}
    m01 = next(m for m in mutations if m.id == "m01")
    for qid in spec.probes:
        sql = questions[qid].gold_sql
        if re.search(r"\benrollment\b", sql) and re.search(r"\bstd_id\b", sql):
            assert qid in m01.gold_overrides, qid


def test_check_gold_reports_each_kind_of_problem() -> None:
    results = {"G": ExecResult({("1",)}, None), "E": ExecResult(set(), None), "B": ExecResult(None, "boom"), "SAME": ExecResult({("1",)}, None)}
    questions = [
        Question("ok", "c", "q", "G"),
        Question("empty", "c", "q", "E"),
        Question("bad", "c", "q", "B"),
        Question("trap", "c", "q", "G", wrong_sql_variants=("SAME",)),
        Question("trap2", "c", "q", "G", wrong_sql_variants=("B",)),
    ]
    report = check_gold(questions, lambda sql: results[sql])
    assert report["healthy"] is False
    assert [p["id"] for p in report["problems"]] == ["empty", "bad", "trap", "trap2"]
    assert check_gold([questions[0]], lambda sql: results[sql])["healthy"] is True


@pytest.mark.docker
def test_every_gold_query_works_on_the_seeded_source(scratch: Psql) -> None:
    database = "edu_public_source"
    scratch.execute("postgres", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
    scratch.execute("postgres", f"CREATE DATABASE {database}")
    try:
        scratch.run_file(database, ROOT / "source" / "schema.sql")
        scratch.run_file(database, ROOT / "source" / "seed.sql")
        report = check_gold(load_questions(ROOT / "questions.json"), lambda sql: execute(scratch, database, sql))
        assert report["problems"] == []
    finally:
        scratch.execute("postgres", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")


@pytest.mark.docker
def test_every_mutation_applies_to_the_seeded_source(scratch: Psql) -> None:
    _, mutations = load_evolve_spec(ROOT)
    for mutation in mutations:
        database = f"edu_public_{mutation.id}"
        scratch.execute("postgres", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
        scratch.execute("postgres", f"CREATE DATABASE {database}")
        try:
            scratch.run_file(database, ROOT / "source" / "schema.sql")
            scratch.run_file(database, ROOT / "source" / "seed.sql")
            scratch.execute(database, ";\n".join(mutation.sql) + ";", single_transaction=True)
            for sql in mutation.gold_overrides.values():
                assert execute(scratch, database, sql).rows
        finally:
            scratch.execute("postgres", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")


def test_dataset_files_are_not_gitignored() -> None:
    gitignore = Path(REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!/datasets/edu-public/" in gitignore
