from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lab.common.errors import LabError
from lab.importer.dataset import Dataset
from lab.measure import preflight
from lab.measure.preflight import check_input, parse_skipped_steps, skipped_rate_ok

CFG = SimpleNamespace(catalog_db="catalog")
DATASET = Dataset(Path("."), "x", "db", [], [], ["public.a", "public.b"], None)


def _world(monkeypatch: pytest.MonkeyPatch, *, target: int, catalog: int, tables: list[str]) -> None:
    monkeypatch.setattr(preflight, "_verify_row_counts", lambda psql, dataset: ({"public.a": 3}, "matched"))
    monkeypatch.setattr(preflight, "count_foreign_key_columns", lambda psql, db: target)
    monkeypatch.setattr(preflight.register, "catalog_foreign_key_columns", lambda psql, c, db: catalog)
    monkeypatch.setattr(preflight.register, "term_coverage", lambda psql, c, db: [(t, "1") for t in tables])


def test_matching_input_is_summarized(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, target=2, catalog=2, tables=["public.a", "public.b"])
    result = check_input(None, CFG, DATASET)  # type: ignore[arg-type]
    assert result["foreign_key_columns"] == 2
    assert result["tables"] == 2


def test_lost_foreign_keys_stop_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, target=18, catalog=0, tables=["public.a", "public.b"])
    with pytest.raises(LabError, match="0 foreign key columns"):
        check_input(None, CFG, DATASET)  # type: ignore[arg-type]


def test_missing_tables_stop_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, target=0, catalog=0, tables=["public.a"])
    with pytest.raises(LabError, match="public.b"):
        check_input(None, CFG, DATASET)  # type: ignore[arg-type]


def test_only_final_failures_count_as_skipped_steps() -> None:
    log = """
    WARNING LLM returned None for QuestionIntentModel on attempt 1/3 — retrying.
    WARNING LLM returned None for QuestionIntentModel on attempt 3/3 — retrying.
    ERROR Reasoning LLM also failed for TableRelevanceModel: ReadTimeout
    WARNING Non-reasoning LLM exhausted 3 retries for SQLGenerationModel
    """
    assert parse_skipped_steps(log) == {"QuestionIntentModel": 1, "TableRelevanceModel": 1}


def test_the_bolt_pattern_fails_and_a_stray_failure_passes() -> None:
    assert not skipped_rate_ok({"QuestionIntentModel": 5, "TableRelevanceModel": 5}, 5)
    assert skipped_rate_ok({"QuestionIntentModel": 1}, 60)
    assert skipped_rate_ok({}, 0)
