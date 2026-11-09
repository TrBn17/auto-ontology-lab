"""Regressions found by cloning a real database (education_db_test), not caught by the fixture."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest
from faker import Faker

from lab.check_parser import TableCheckHints, generate_string_from_regex
from lab.cli import _cmd_generate, build_parser
from lab.generate import _apply_split_part_prefix, _enforce_uniqueness_on_table


@pytest.mark.parametrize(
    "pattern",
    [
        r"^(listening|reading|grammar)\.[a-z][a-z0-9_]{1,39}$",
        r"^(?:draft|live)$",
        r"^[A-Z]{2}-(a|b)-\d{3}$",
    ],
)
def test_generated_values_match_patterns_with_alternation(pattern: str) -> None:
    rng = np.random.default_rng(1)
    for index in range(50):
        value = generate_string_from_regex(pattern, rng, index)
        assert re.fullmatch(pattern.strip("^$"), value), value


def test_unanchored_pattern_still_gets_a_unique_suffix() -> None:
    value = generate_string_from_regex(r"^(a|b)-", np.random.default_rng(1), 4)
    assert value.endswith("-5")


def _hints() -> TableCheckHints:
    return TableCheckHints(split_part_prefix=[("subskill_code", ".", "domain")])


def test_fk_target_is_kept_and_primary_key_prefix_follows_it() -> None:
    rows = [{"domain": "speaking", "code": "x1", "subskill_code": "grammar.abc"}]
    _apply_split_part_prefix(rows, _hints(), fk_cols={"subskill_code"}, pk_cols={"domain", "code"})
    assert rows == [{"domain": "grammar", "code": "x1", "subskill_code": "grammar.abc"}]


def test_null_fk_target_leaves_the_prefix_alone() -> None:
    rows = [{"domain": "speaking", "code": "x1", "subskill_code": None}]
    _apply_split_part_prefix(rows, _hints(), fk_cols={"subskill_code"}, pk_cols={"domain", "code"})
    assert rows[0]["domain"] == "speaking"


def test_fk_value_is_never_rewritten_when_both_columns_are_fks() -> None:
    rows = [{"domain": "speaking", "subskill_code": "grammar.abc"}]
    _apply_split_part_prefix(rows, _hints(), fk_cols={"subskill_code", "domain"}, pk_cols=set())
    assert rows == [{"domain": "speaking", "subskill_code": "grammar.abc"}]


def test_plain_target_still_takes_its_prefix_from_the_other_column() -> None:
    rows = [{"domain": "reading", "subskill_code": "other.abc"}]
    _apply_split_part_prefix(rows, _hints(), fk_cols=set(), pk_cols=set())
    assert rows[0]["subskill_code"] == "reading.abc"


def test_regen_mode_reaches_the_profile_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "dataset"
    args = build_parser().parse_args(["clone", "--mode", "regen", "--out", str(out)])
    assert _cmd_generate(args) == 1
    assert "profile.json not found" in capsys.readouterr().err


def test_uniqueness_keeps_a_prefix_derived_from_an_fk() -> None:
    rows = [
        {"domain": "grammar", "code": "same", "subskill_code": "grammar.a"},
        {"domain": "grammar", "code": "same", "subskill_code": "grammar.a"},
    ]
    meta = {
        "pk": ["domain", "code"],
        "columns": {
            "domain": {"type": "text"},
            "code": {"type": "text"},
            "subskill_code": {"type": "text"},
        },
    }
    _enforce_uniqueness_on_table(
        "public.t", meta, [], rows, {"subskill_code"}, [], {}, _hints(), np.random.default_rng(1), Faker()
    )
    assert [row["domain"] for row in rows] == ["grammar", "grammar"]
    assert rows[0]["code"] != rows[1]["code"]
