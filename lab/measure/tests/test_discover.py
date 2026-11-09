from __future__ import annotations

import pytest
import yaml

from lab.measure.discover import score_business_terms, score_discovery
from lab.common.errors import LabError
from lab.measure.model import fetch_model, parse_model

DOC = {
    "data_layer": {
        "databases": [
            {
                "id": "db",
                "schemas": [
                    {
                        "id": "s",
                        "name": "public",
                        "tables": [
                            {
                                "id": "t_student",
                                "name": "student",
                                "columns": [
                                    {"id": "c_sid", "name": "id", "type": "integer", "description": "pk", "sample_values": [1]},
                                    {"id": "c_org", "name": "org", "type": "text"},
                                ],
                            },
                            {
                                "id": "t_enroll",
                                "name": "enrollment",
                                "columns": [
                                    {"id": "c_esid", "name": "student_id", "type": "integer"},
                                    {"id": "c_eorg", "name": "org", "type": "text"},
                                    {"id": "c_code", "name": "code", "type": "text"},
                                ],
                            },
                            {"id": "t_course", "name": "course", "columns": [{"id": "c_ccode", "name": "code", "type": "text"}]},
                        ],
                    }
                ],
            }
        ],
        "foreign_keys": [
            {"source_column_id": "c_esid", "target_column_id": "c_sid"},
            {"source_column_id": "c_eorg", "target_column_id": "c_org"},
        ],
    },
    "semantic_layer": {
        "terms": [
            {
                "id": "term1",
                "name": "Student",
                "represents": ["t_student"],
                "columns_attributes": [{"id": "a1", "name": "sid", "column_id": "c_sid"}],
            },
            {
                "id": "term2",
                "name": "Course",
                "represents": ["t_course"],
                "columns_attributes": [{"id": "a2", "name": "course code", "column_id": "c_ccode"}],
            },
        ],
        "semantic_fks": [
            {"column_attribute_id": "a2", "column_id": "c_code"},
            {"column_attribute_id": "a1", "column_id": "c_esid"},
            {"column_attribute_id": "a2", "column_id": "c_org"},
        ],
        "sql_attributes": {"manual": [{"id": "m", "name": "headcount", "sql": "SELECT count(*) FROM student"}]},
    },
}


def _ontology():
    return parse_model(yaml.safe_dump(DOC))


def _rel(kind, table_a, cols_a, table_b, cols_b):
    return {"kind": kind, "from": {"table": table_a, "columns": cols_a}, "to": {"table": table_b, "columns": cols_b}}


MANIFEST = {
    "entities": [{"table": "public.student"}, {"table": "public.enrollment"}, {"table": "public.course"}],
    "relationships": [
        _rel("fk", "public.enrollment", ["student_id", "org"], "public.student", ["id", "org"]),
        _rel("implicit", "public.enrollment", ["code"], "public.course", ["code"]),
    ],
}


class _ExportGateway:
    def __init__(self) -> None:
        self.requested: list[list[str]] = []

    def post_text(self, path: str, body: dict) -> str:
        self.requested.append(body["databases"])
        return yaml.safe_dump({"data_layer": {"databases": []}})


class _Catalog:
    def scalar(self, database: str, sql: str) -> str | None:
        return "uuid-b" if "'wanted'" in sql else None


def test_fetch_model_exports_by_id_not_by_name() -> None:
    gateway = _ExportGateway()
    fetch_model(gateway, _Catalog(), "catalog", "wanted")  # type: ignore[arg-type]
    assert gateway.requested == [["uuid-b"]]


def test_fetch_model_reports_an_unregistered_database() -> None:
    gateway = _ExportGateway()
    with pytest.raises(LabError, match="missing"):
        fetch_model(gateway, _Catalog(), "catalog", "missing")  # type: ignore[arg-type]
    assert gateway.requested == []


def test_model_resolves_ids_to_qualified_names() -> None:
    onto = _ontology()
    assert onto.term_tables == {"public.student", "public.course"}
    assert frozenset({"public.enrollment.code", "public.course.code"}) in onto.semantic_fks
    assert onto.columns["public.student.id"]["type"] == "integer"
    assert [a["name"] for a in onto.attributes["public.student.id"]] == ["sid"]


def test_entities_score_against_tables_with_a_term() -> None:
    entities = score_discovery(MANIFEST, _ontology())["entities"]
    assert entities["precision"] == 1.0
    assert entities["recall"] == 2 / 3
    assert entities["missed"] == ["public.enrollment"]


def test_composite_fk_expands_to_column_pairs_and_direction_is_ignored() -> None:
    physical = score_discovery(MANIFEST, _ontology())["physical_fk"]
    assert physical["true_positive"] == 2
    assert physical["f1"] == 1.0


def test_semantic_fk_scored_against_implicit_relationships_only() -> None:
    semantic = score_discovery(MANIFEST, _ontology())["semantic_fk"]
    assert semantic["true_positive"] == 1
    assert semantic["recall"] == 1.0
    # c_esid-c_sid repeats a physical FK, so it is redundant rather than invented.
    assert score_discovery(MANIFEST, _ontology())["redundant_semantic_fks"] == [["public.enrollment.student_id", "public.student.id"]]


def test_invented_relationship_is_listed_as_false_positive() -> None:
    result = score_discovery(MANIFEST, _ontology())
    assert result["semantic_fk"]["false_positive"] == 1
    assert result["invented_relationships"] == [["public.course.code", "public.student.org"]]


def test_no_gold_and_no_prediction_scores_none_rather_than_perfect() -> None:
    result = score_discovery({"entities": [], "relationships": []}, parse_model(yaml.safe_dump({})))
    assert result["physical_fk"]["f1"] is None


def test_business_terms_match_by_execution_not_by_text() -> None:
    results = {"gold": {("3",)}, "SELECT count(*) FROM student": {("3",)}}

    def run(sql):
        return results.get(sql)

    terms = [{"name": "headcount", "gold_sql": "gold"}, {"name": "absent", "gold_sql": "gold2"}]
    onto = _ontology()
    scored = score_business_terms(terms, onto, run)
    assert scored["unusable_gold"] == ["absent"]
    assert scored["recall"] == 1.0
    assert scored["found"] == {"headcount": "headcount"}
