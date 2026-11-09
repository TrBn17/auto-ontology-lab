from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from lab.common.errors import LabError
from lab.measure.evolve import (
    PROBE_STAGES,
    STAGES,
    EvolveSpec,
    Mutation,
    check_assertions,
    load_evolve_spec,
    run_mutation,
    unexpected_relationships,
)
from lab.measure.model import parse_model


def _doc(*, column="enrolled_at", attr_column=None, desc="", samples=None, extra_fk=False):
    columns = [
        {"id": "c1", "name": column, "type": "date", "description": desc, "sample_values": samples or []},
        {"id": "c2", "name": "status", "type": "text"},
    ]
    attrs = [{"id": "a1", "name": "enroll date", "column_id": attr_column or "c1"}]
    doc = {
        "data_layer": {
            "databases": [{"id": "d", "schemas": [{"id": "s", "name": "public", "tables": [{"id": "t", "name": "enrollment", "columns": columns}]}]}],
            "foreign_keys": [{"source_column_id": "c1", "target_column_id": "c2"}] if extra_fk else [],
        },
        "semantic_layer": {"terms": [{"id": "T", "name": "Enrollment", "description": desc, "represents": ["t"], "columns_attributes": attrs}]},
    }
    return parse_model(yaml.safe_dump(doc))


@pytest.mark.parametrize(
    ("assertion", "expected"),
    [
        ({"kind": "column_present", "column": "public.enrollment.enrolled_at"}, True),
        ({"kind": "column_present", "column": "public.enrollment.gone"}, False),
        ({"kind": "column_absent", "column": "public.enrollment.gone"}, True),
        ({"kind": "column_type", "column": "public.enrollment.enrolled_at", "type": "date"}, True),
        ({"kind": "column_type", "column": "public.enrollment.enrolled_at", "type": "timestamp"}, False),
        ({"kind": "attribute_on_column", "column": "public.enrollment.enrolled_at"}, True),
        ({"kind": "attribute_on_column", "column": "public.enrollment.status"}, False),
        ({"kind": "no_attribute_on_column", "column": "public.enrollment.status"}, True),
        ({"kind": "term_for_table", "table": "public.enrollment"}, True),
        ({"kind": "term_for_table", "table": "public.other"}, False),
    ],
)
def test_assertion_kinds(assertion: dict, expected: bool) -> None:
    assert check_assertions(_doc(), [assertion])[0]["passed"] is expected


def test_sample_value_and_description_assertions() -> None:
    onto = _doc(desc="when enrolled", samples=["excused", 3])
    results = check_assertions(
        onto,
        [
            {"kind": "sample_value", "column": "public.enrollment.enrolled_at", "value": "excused"},
            {"kind": "sample_value", "column": "public.enrollment.enrolled_at", "value": "nope"},
            {"kind": "column_description", "column": "public.enrollment.enrolled_at", "contains": "enrolled"},
        ],
    )
    assert [r["passed"] for r in results] == [True, False, True]


def test_physical_fk_is_stricter_than_relationship() -> None:
    pair = ["public.enrollment.enrolled_at", "public.enrollment.status"]
    assertion = {"kind": "physical_fk", "columns": pair}
    assert check_assertions(_doc(extra_fk=True), [assertion])[0]["passed"] is True
    assert check_assertions(_doc(), [assertion])[0]["passed"] is False
    assert unexpected_relationships(_doc(), _doc(extra_fk=True), [assertion]) == []


def test_unknown_assertion_kind_fails_loudly() -> None:
    with pytest.raises(LabError, match="unknown assertion"):
        check_assertions(_doc(), [{"kind": "nope"}])


def test_unexpected_relationship_ignores_those_the_mutation_expects() -> None:
    before, after = _doc(), _doc(extra_fk=True)
    pair = ["public.enrollment.enrolled_at", "public.enrollment.status"]
    assert unexpected_relationships(before, after, []) == [pair]
    assert unexpected_relationships(before, after, [{"kind": "relationship", "columns": pair}]) == []


class FakeStack:
    """Records the order of protocol operations; every mutation of state is logged."""

    def __init__(self, survive_until: str = "t3", fail_at: str | None = None):
        self.calls: list[str] = []
        self.stage: str | None = None
        self.survive_until = survive_until
        self.fail_at = fail_at

    def restore_t0(self):
        self.calls.append("restore")

    def apply_edits(self, spec):
        self.calls.append("edits")
        return {"edits": 6}

    def apply_mutation(self, mutation):
        self.calls.append("mutate")

    def run_stage(self, stage):
        self.calls.append(stage)
        self.stage = stage
        if stage == self.fail_at:
            raise LabError("compile failed")
        return 12.5

    def model(self):
        self.calls.append("model")
        return _doc()

    def edits_surviving(self, applied):
        return 0 if self.stage == "t4" else 6

    def probe(self, spec, mutation, label):
        self.calls.append(f"probe:{label}")
        return [{"id": "q1", "outcome": "correct" if label != "t3" else "wrong_result"}]


SPEC = EvolveSpec(probes=["q1"], edits={"certify_terms": []})
MUT = Mutation(id="m01", title="rename", sql=["ALTER"], expect=[{"kind": "column_present", "column": "public.enrollment.enrolled_at"}], gold_overrides={})


def test_protocol_order_baselines_before_mutating() -> None:
    stack = FakeStack()
    run_mutation(stack, SPEC, MUT)
    assert stack.calls[:5] == ["restore", "edits", "model", "probe:before", "mutate"]


def test_every_stage_records_structure_and_only_probe_stages_ask_questions() -> None:
    stack = FakeStack()
    result = run_mutation(stack, SPEC, MUT)
    assert [s["stage"] for s in result["stages"]] == list(STAGES)
    assert PROBE_STAGES == ("t3", "t4")
    assert result["stages"][0]["probes"] is None
    assert result["stages"][1]["probes"] == [{"id": "q1", "outcome": "wrong_result"}]
    assert result["stages"][0]["reflected"] is True
    assert result["stages"][0]["seconds"] == 12.5


def test_edit_survival_and_probe_regression_are_reported_per_stage() -> None:
    result = run_mutation(FakeStack(), SPEC, MUT)
    assert [s["edits_surviving"] for s in result["stages"]] == [6, 6, 0]
    assert result["stages"][1]["probes_regressed"] == ["q1"]
    assert result["stages"][2]["probes_regressed"] == []


def test_stage_failure_is_recorded_and_later_stages_are_skipped_as_unhealthy() -> None:
    result = run_mutation(FakeStack(fail_at="t3"), SPEC, MUT)
    assert result["healthy"] is False
    assert result["error"] == "t3: compile failed"
    assert [s["stage"] for s in result["stages"]] == ["t2"]


def test_load_spec_reads_mutations_and_validates(tmp_path: Path) -> None:
    (tmp_path / "evolve.json").write_text(json.dumps({"probes": ["q1"], "edits": {}}), encoding="utf-8")
    mutations = tmp_path / "mutations"
    mutations.mkdir()
    (mutations / "m02.json").write_text(json.dumps({"id": "m02", "title": "t", "sql": ["S"], "expect": []}), encoding="utf-8")
    (mutations / "m01.json").write_text(json.dumps({"id": "m01", "title": "t", "sql": ["S"], "expect": [], "gold_overrides": {"q1": "G"}}), encoding="utf-8")
    spec, loaded = load_evolve_spec(tmp_path)
    assert spec.probes == ["q1"]
    assert [m.id for m in loaded] == ["m01", "m02"]
    assert loaded[0].gold_overrides == {"q1": "G"}

    (mutations / "m03.json").write_text(json.dumps({"id": "m03"}), encoding="utf-8")
    with pytest.raises(LabError, match="m03"):
        load_evolve_spec(tmp_path)


def test_missing_spec_says_what_is_needed(tmp_path: Path) -> None:
    with pytest.raises(LabError, match="evolve.json"):
        load_evolve_spec(tmp_path)
