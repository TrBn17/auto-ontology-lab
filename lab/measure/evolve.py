"""Schema evolution suite: apply one mutation, then see what each upstream stage reflects.

Stages: T2 re-ingests through :3002, T3 runs a compile pass, T4 resets the database's semantic
layer and rebuilds it. T2 and T4 use the internal port because no public route exists
(UPSTREAM_ARCHITECTURE.md §0.5, §0.6), and every such call is recorded for the report.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from lab.common.config import LabConfig
from lab.common.errors import LabError
from lab.common.gateway import Gateway
from lab.common.pg import Psql
from lab.common.readiness import (
    fetch_run_after,
    latest_run_id,
    read_ingestion_logs,
    wait_for_compile,
    wait_for_ingest,
)
from lab.common.snapshot import restore_catalog, restore_target
from lab.importer import register
from lab.importer.dataset import Dataset
from lab.importer.target import ensure_ro_role
from lab.measure.ask import Question, gateway_generator
from lab.measure.model import Ontology, fetch_model, pair_as_list
from lab.measure.scoring import ExecResult, execute, score

STAGES = ("t2", "t3", "t4")
PROBE_STAGES = ("t3", "t4")
_EDIT_COUNT = 6
_REQUIRED = ("id", "title", "sql", "expect")


@dataclass(frozen=True)
class EvolveSpec:
    probes: list[str]
    edits: dict[str, Any]


@dataclass(frozen=True)
class Mutation:
    id: str
    title: str
    sql: list[str]
    expect: list[dict[str, Any]]
    gold_overrides: dict[str, str]
    # Probes that touch the mutated tables; empty means evolve.json's shared list.
    probes: list[str] = field(default_factory=list)


class Stack(Protocol):
    def restore_t0(self) -> None: ...
    def apply_edits(self, spec: EvolveSpec) -> dict[str, Any]: ...
    def apply_mutation(self, mutation: Mutation) -> None: ...
    def run_stage(self, stage: str) -> float: ...
    def model(self) -> Ontology: ...
    def edits_surviving(self, applied: dict[str, Any]) -> int: ...
    def probe(self, spec: EvolveSpec, mutation: Mutation, label: str) -> list[dict[str, Any]]: ...


def load_evolve_spec(root: Path) -> tuple[EvolveSpec, list[Mutation]]:
    spec_path = root / "evolve.json"
    if not spec_path.is_file():
        raise LabError(f"{spec_path} not found; it lists the probe question ids and the edits to apply")
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    mutations = []
    for path in sorted((root / "mutations").glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        missing = [key for key in _REQUIRED if key not in item]
        if missing:
            raise LabError(f"{path.name} (mutation {path.stem}) is missing {', '.join(missing)}")
        mutations.append(
            Mutation(
                item["id"], item["title"], item["sql"], item["expect"],
                item.get("gold_overrides") or {}, list(item.get("probes") or []),
            )
        )
    return EvolveSpec(list(raw.get("probes") or []), dict(raw.get("edits") or {})), mutations


def check_assertions(ontology: Ontology, assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**assertion, "passed": _holds(ontology, assertion)} for assertion in assertions]


def unexpected_relationships(before: Ontology, after: Ontology, expect: list[dict[str, Any]]) -> list[list[str]]:
    """Relationships that appeared at this stage and that the mutation does not explain."""
    expected = {frozenset(a["columns"]) for a in expect if a.get("kind") in ("relationship", "physical_fk")}
    existing = before.fks | before.semantic_fks
    return sorted(pair_as_list(pair) for pair in (after.fks | after.semantic_fks) - existing - expected)


def run_mutation(stack: Stack, spec: EvolveSpec, mutation: Mutation) -> dict[str, Any]:
    """Run the whole protocol for one mutation. A failed stage ends it and marks it unhealthy."""
    result: dict[str, Any] = {"id": mutation.id, "title": mutation.title, "healthy": True, "error": None, "stages": []}
    label = "setup"
    try:
        stack.restore_t0()
        applied = stack.apply_edits(spec)
        before = stack.model()
        baseline = stack.probe(spec, mutation, "before")
        result["baseline_probes"] = baseline
        stack.apply_mutation(mutation)
        for label in STAGES:
            result["stages"].append(_run_stage(stack, spec, mutation, label, applied, before, baseline))
    except (LabError, httpx.HTTPError) as exc:
        result["healthy"] = False
        result["error"] = f"{label}: {exc}"
    return result


def _run_stage(
    stack: Stack,
    spec: EvolveSpec,
    mutation: Mutation,
    stage: str,
    applied: dict[str, Any],
    before: Ontology,
    baseline: list[dict[str, Any]],
) -> dict[str, Any]:
    seconds = stack.run_stage(stage)
    ontology = stack.model()
    assertions = check_assertions(ontology, mutation.expect)
    probes = stack.probe(spec, mutation, stage) if stage in PROBE_STAGES else None
    return {
        "stage": stage,
        "seconds": seconds,
        "reflected": all(a["passed"] for a in assertions),
        "assertions": assertions,
        "unexpected_relationships": unexpected_relationships(before, ontology, mutation.expect),
        "edits_surviving": stack.edits_surviving(applied),
        "edits_total": _EDIT_COUNT,
        "probes": probes,
        "probes_regressed": _regressed(baseline, probes) if probes is not None else None,
    }


def _regressed(baseline: list[dict[str, Any]], probes: list[dict[str, Any]]) -> list[str]:
    was_correct = {row["id"] for row in baseline if row["outcome"] == "correct"}
    return sorted(row["id"] for row in probes if row["id"] in was_correct and row["outcome"] != "correct")


def _holds(ontology: Ontology, assertion: dict[str, Any]) -> bool:
    kind = assertion.get("kind")
    column = ontology.columns.get(str(assertion.get("column")))
    if kind == "column_present":
        return column is not None
    if kind == "column_absent":
        return column is None
    if kind == "column_type":
        return column is not None and str(column.get("type")).lower() == str(assertion["type"]).lower()
    if kind == "column_description":
        return column is not None and str(assertion["contains"]) in str(column.get("description") or "")
    if kind == "sample_value":
        return column is not None and str(assertion["value"]) in {str(v) for v in column.get("sample_values") or []}
    if kind == "attribute_on_column":
        return bool(ontology.attributes.get(str(assertion["column"])))
    if kind == "no_attribute_on_column":
        return not ontology.attributes.get(str(assertion["column"]))
    if kind == "term_for_table":
        return str(assertion["table"]) in ontology.term_tables
    if kind == "relationship":
        return frozenset(assertion["columns"]) in ontology.fks | ontology.semantic_fks
    if kind == "physical_fk":
        return frozenset(assertion["columns"]) in ontology.fks
    raise LabError(f"unknown assertion kind {kind!r}")


class LiveStack:
    """The protocol's operations against the running Auto-Ontology stack."""

    def __init__(
        self,
        *,
        admin: Gateway,
        viewer: Gateway,
        psql: Psql,
        psql_ro: Psql,
        cfg: LabConfig,
        dataset: Dataset,
        ro_password: str,
        questions: dict[str, Question],
        catalog_t0: Path,
        target_t0: Path,
    ) -> None:
        self._admin = admin
        self._psql = psql
        self._psql_ro = psql_ro
        self._cfg = cfg
        self._dataset = dataset
        self._ro_password = ro_password
        self._questions = questions
        self._catalog_t0 = catalog_t0
        self._target_t0 = target_t0
        self._generate = gateway_generator(viewer, dataset.db_name)
        self._internal = httpx.Client(base_url=cfg.ingestion_url, timeout=30.0)
        self.internal_calls: list[str] = []

    def restore_t0(self) -> None:
        register.wait_until_idle(self._admin)
        restore_catalog(self._psql, self._cfg.catalog_db, self._catalog_t0, is_idle=self._is_idle)
        restore_target(self._psql, self._dataset.db_name, self._target_t0, catalog_db=self._cfg.catalog_db)

    def apply_edits(self, spec: EvolveSpec) -> dict[str, Any]:
        """Apply 3 certifications and 3 content edits, each through the public write routes."""
        edits = spec.edits
        ontology = self.model()
        for table in edits["certify_terms"]:
            self._admin.patch(
                f"/api/terms/{_term(ontology, table)['id']}", {"name_certified": True, "description_certified": True}
            )
        description = edits["description"]
        self._admin.patch(f"/api/terms/{_term(ontology, description['table'])['id']}", {"description": description["text"]})
        samples = edits["sample_values"]
        attributes = ontology.attributes.get(samples["column"])
        if not attributes:
            raise LabError(f"no attribute on {samples['column']} to edit; pick another column in evolve.json")
        attribute = attributes[0]
        self._admin.patch(
            f"/api/terms/{attribute['term_id']}/column-attributes/{attribute['id']}", {"sample_values": samples["values"]}
        )
        sql = edits["sql_attribute"]
        self._admin.post(
            "/api/sql-attributes",
            {
                "name": sql["name"],
                "description": sql.get("description", ""),
                "expression": sql["expression"],
                "term_id": _term(ontology, sql["table"])["id"],
                "source": "manual",
            },
        )
        return edits

    def edits_surviving(self, applied: dict[str, Any]) -> int:
        ontology = self.model()
        count = sum(self._is_certified(ontology, table) for table in applied["certify_terms"])
        description = applied["description"]
        term = ontology.terms_by_table.get(description["table"]) or {}
        count += description["text"] == term.get("description")
        samples = applied["sample_values"]
        column = ontology.columns.get(samples["column"]) or {}
        have = {str(value) for value in column.get("sample_values") or []}
        count += {str(value) for value in samples["values"]} <= have
        name = applied["sql_attribute"]["name"]
        count += any(attribute.get("name") == name for attribute in ontology.manual_sql_attributes)
        return count

    def apply_mutation(self, mutation: Mutation) -> None:
        self._psql.execute(self._dataset.db_name, ";\n".join(mutation.sql) + ";", single_transaction=True)
        # m08's new table would otherwise be invisible to the connection's role.
        ensure_ro_role(self._psql, self._dataset, self._ro_password)

    def run_stage(self, stage: str) -> float:
        register.wait_until_idle(self._admin)
        started = time.monotonic()
        database = self._dataset.db_name
        if stage == "t2":
            since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._post_internal("/ingest", json={"database": database})
            wait_for_ingest(lambda: read_ingestion_logs(self._cfg.containers.ingestion, since), database)
        elif stage == "t3":
            baseline = latest_run_id(self._psql, self._cfg.catalog_db)
            # The gateway has no trigger route; enabling the setting is the public way to start a pass.
            self._admin.put(register._COMPILE_CONFIG_PATH, {"enabled": True})
            self._wait_for_pass(baseline)
        elif stage == "t4":
            baseline = latest_run_id(self._psql, self._cfg.catalog_db)
            self._post_internal("/semantic/reset", params={"database_name": database})
            self._wait_for_pass(baseline)
        else:
            raise LabError(f"unknown stage {stage!r}")
        return time.monotonic() - started

    def model(self) -> Ontology:
        return fetch_model(self._admin, self._psql, self._cfg.catalog_db, self._dataset.db_name)

    def probe(self, spec: EvolveSpec, mutation: Mutation, label: str) -> list[dict[str, Any]]:
        return [self._probe_one(qid, mutation, label) for qid in mutation.probes or spec.probes]

    def _probe_one(self, qid: str, mutation: Mutation, label: str) -> dict[str, Any]:
        question = self._questions[qid]
        gold_sql = question.gold_sql if label == "before" else mutation.gold_overrides.get(qid, question.gold_sql)
        gold = self._execute(gold_sql)
        if not gold.rows:
            # A mutation can invalidate a gold query; without an override this question cannot be scored.
            return {"id": qid, "outcome": "gold_unusable", "sql": "", "detail": gold.error or "gold returned no rows"}
        generated = self._generate(question)
        if generated.status != "generated":
            return {"id": qid, "outcome": generated.status, "sql": generated.sql, "detail": generated.detail}
        predicted = self._execute(generated.sql)
        return {"id": qid, "outcome": score(gold, predicted), "sql": generated.sql, "detail": predicted.error or ""}

    def _execute(self, sql: str) -> ExecResult:
        return execute(self._psql_ro, self._dataset.db_name, sql)

    def _is_certified(self, ontology: Ontology, table: str) -> bool:
        term = ontology.terms_by_table.get(table)
        if term is None:
            return False
        detail = self._admin.get(f"/api/terms/{term['id']}")
        return bool((detail.get("data") or detail).get("name_certified"))

    def _is_idle(self) -> bool:
        return not self._is_running()

    def _is_running(self) -> bool:
        status = self._admin.get("/api/semantic-compilation/status")
        return bool((status.get("data") or status).get("running"))

    def _wait_for_pass(self, baseline: int) -> None:
        wait_for_compile(
            lambda after: fetch_run_after(self._psql, self._cfg.catalog_db, after), self._is_running, baseline_id=baseline
        )

    def _post_internal(self, path: str, **kwargs: Any) -> None:
        self.internal_calls.append(path)
        response = self._internal.post(path, **kwargs)
        if not response.is_success:
            raise LabError(f"ingestion service {path} returned {response.status_code}: {response.text[:300]}")


def _term(ontology: Ontology, table: str) -> dict[str, Any]:
    term = ontology.terms_by_table.get(table)
    if term is None:
        raise LabError(f"{table} has no Term to edit; pick another table in evolve.json")
    return term
