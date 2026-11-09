"""Score the discovered ontology against the dataset manifest."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from lab.measure.model import Ontology, Pair, pair_as_list

Rows = set[tuple[str, ...]]


def score_discovery(manifest: dict[str, Any], ontology: Ontology) -> dict[str, Any]:
    """Compare tables and relationships. The manifest is the ground truth."""
    gold_tables = {str(entity["table"]) for entity in manifest.get("entities") or []}
    gold_fk = _gold_pairs(manifest, "fk")
    gold_implicit = _gold_pairs(manifest, "implicit")

    # A semantic FK that repeats a physical FK is neither a find nor an invention.
    redundant = ontology.semantic_fks & (gold_fk | ontology.fks)
    predicted_semantic = ontology.semantic_fks - redundant
    invented = (predicted_semantic - gold_implicit) | (ontology.fks - gold_fk - gold_implicit)

    return {
        "entities": _prf(gold_tables, ontology.term_tables),
        "physical_fk": _pair_prf(gold_fk, ontology.fks),
        "semantic_fk": _pair_prf(gold_implicit, predicted_semantic),
        "redundant_semantic_fks": _sorted_pairs(redundant),
        "invented_relationships": _sorted_pairs(invented),
    }


def score_business_terms(
    terms: list[dict[str, Any]], ontology: Ontology, run: Callable[[str], Rows | None]
) -> dict[str, Any]:
    """Find, for each gold term, a generated SQL attribute that returns the same rows.

    Reports recall only. Upstream generates attributes nobody listed and many are legitimate,
    so counting them as false positives would not measure anything.
    """
    candidates = [(attr, run(str(attr.get("sql") or ""))) for attr in ontology.sql_attributes if attr.get("sql")]
    found: dict[str, str] = {}
    unusable: list[str] = []
    for term in terms:
        gold = run(str(term["gold_sql"]))
        if not gold:
            unusable.append(str(term["name"]))
            continue
        match = next((attr for attr, rows in candidates if rows == gold), None)
        if match is not None:
            found[str(term["name"])] = str(match.get("name"))
    scored = len(terms) - len(unusable)
    return {
        "recall": len(found) / scored if scored else None,
        "found": found,
        "missed": sorted(str(t["name"]) for t in terms if t["name"] not in found and t["name"] not in unusable),
        "unusable_gold": unusable,
        "generated_attributes": len(ontology.sql_attributes),
    }


def _gold_pairs(manifest: dict[str, Any], kind: str) -> set[Pair]:
    pairs: set[Pair] = set()
    for relationship in manifest.get("relationships") or []:
        if relationship.get("kind") != kind:
            continue
        source, target = relationship["from"], relationship["to"]
        for left, right in zip(source["columns"], target["columns"], strict=True):
            pairs.add(frozenset({f"{source['table']}.{left}", f"{target['table']}.{right}"}))
    return pairs


def _prf(gold: set[str], predicted: set[str]) -> dict[str, Any]:
    result = _counts(gold, predicted)
    result["missed"] = sorted(gold - predicted)
    result["extra"] = sorted(predicted - gold)
    return result


def _pair_prf(gold: set[Pair], predicted: set[Pair]) -> dict[str, Any]:
    result = _counts(gold, predicted)
    result["missed"] = _sorted_pairs(gold - predicted)
    return result


def _counts(gold: set[Any], predicted: set[Any]) -> dict[str, Any]:
    true_positive = len(gold & predicted)
    precision = true_positive / len(predicted) if predicted else None
    recall = true_positive / len(gold) if gold else None
    if precision is None or recall is None:
        f1 = None  # Nothing to score must not read as a perfect result.
    else:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positive": true_positive,
        "false_positive": len(predicted - gold),
        "false_negative": len(gold - predicted),
    }


def _sorted_pairs(pairs: Iterable[Pair]) -> list[list[str]]:
    return sorted(pair_as_list(pair) for pair in pairs)
