"""`lab measure`: run the suites from T0 and write raw results to reports/runs/<ts>/."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from lab.common.config import LabConfig, load_config, require_token
from lab.common.errors import LabError
from lab.common.gateway import Gateway
from lab.common.pg import Psql
from lab.common.readiness import latest_run_id
from lab.common.runlog import RunDir
from lab.common.snapshot import restore_catalog, restore_target
from lab.importer import register
from lab.importer.cli import SNAPSHOTS_DIR, _t0_paths
from lab.importer.dataset import Dataset, load_dataset
from lab.importer.target import RO_ROLE
from lab.measure.ask import Generator, gateway_generator, llm_generator, load_questions, run_ask, summarize
from lab.measure.audit import count_vector_rows, pii_findings, read_pii_samples, retry_infrastructure, run_unanswerable
from lab.measure.discover import score_business_terms, score_discovery
from lab.measure.evolve import LiveStack, load_evolve_spec, run_mutation
from lab.measure.gold import check_gold
from lab.measure.llm import LlmClient
from lab.measure.model import fetch_model
from lab.measure.preflight import check_input, skipped_rate_ok, skipped_steps_since
from lab.measure.scoring import execute

EXIT_UNHEALTHY = 3
SUITES = ("discover", "ask", "evolve", "audit")
ALL_MODES = ("ddl", "ddl+meta", "gateway")
_LLM_KEY_ENV = "LAB_LLM_API_KEY"
_RO_PASSWORD_ENV = "LAB_RO_PASSWORD"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", required=True, help="Dataset folder name under datasets/.")
    parser.add_argument("--only", choices=SUITES, default=None, help="Run one suite (default: all, in order).")
    parser.add_argument("--patch", action="store_true", help="Run `evolve` with the sidecar patch.")
    parser.add_argument("--runs", type=int, default=3, help="Runs of each mode for `ask` (default: 3).")
    parser.add_argument("--limit", type=int, default=None, help="Ask only the first N questions, as a smoke test.")
    parser.add_argument("--modes", default=None, help=f"Comma-separated `ask` modes (default: {','.join(ALL_MODES)}).")
    parser.add_argument("--mutation", default=None, help="Run one mutation id for `evolve`, such as m01.")
    parser.add_argument("--questions", type=Path, default=None, help="Questions file (default: <dataset>/questions.json).")
    parser.add_argument("--freeze", type=Path, default=None, help="Freeze file (default: config/freeze.yaml).")
    parser.add_argument(
        "--retry-from", type=Path, default=None,
        help="For `audit`: a run dir whose audit.json to reuse, re-asking only questions that failed for infrastructure reasons.",
    )
    parser.add_argument(
        "--check-gold", action="store_true", help="Check every gold query against the target database, using no LLM."
    )


def run(args: argparse.Namespace) -> int:
    if args.patch:
        raise LabError("--patch is not available yet: it is built only after the vanilla matrix exists (PLAN.md Step 2)")
    dataset = load_dataset(args.dataset)
    cfg = load_config(args.freeze)
    if getattr(args, "check_gold", False):
        return _run_check_gold(args, dataset, cfg)
    context = _Context(args, dataset, cfg)
    suites = (args.only,) if args.only else SUITES

    run_dir = RunDir.create()
    results: dict[str, dict[str, Any]] = {}
    try:
        context.restore_t0()
        run_dir.write_json("input_checks.json", check_input(context.psql, cfg, dataset))
        for suite in suites:
            print(f"Running {suite}...", flush=True)
            results[suite] = _SUITE_RUNNERS[suite](context, run_dir)
        run_dir.write_json("measure.json", _provenance(args, dataset, cfg, context, results))
    except httpx.HTTPError as exc:
        raise LabError(f"could not reach the stack: {exc}. Partial results are in {run_dir.path}") from exc
    print(f"Results: {run_dir.path}")
    code = exit_code(results)
    if code == EXIT_UNHEALTHY:
        unhealthy = [suite for suite, result in results.items() if not result.get("healthy")]
        print(f"Not usable as headline numbers; failed the health check: {', '.join(unhealthy)}")
    return code


def exit_code(results: dict[str, dict[str, Any]]) -> int:
    return 0 if all(result.get("healthy") for result in results.values()) else EXIT_UNHEALTHY


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_check_gold(args: argparse.Namespace, dataset: Dataset, cfg: LabConfig) -> int:
    psql_ro = Psql(cfg.containers.postgres, RO_ROLE)
    questions = load_questions(args.questions or dataset.root / "questions.json")
    report = check_gold(questions, lambda sql: execute(psql_ro, dataset.db_name, sql))
    for problem in report["problems"]:
        print(f"{problem['id']}: {problem['problem']}")
    print(f"{report['questions'] - len({p['id'] for p in report['problems']})}/{report['questions']} questions are usable")
    return 0 if report["healthy"] else 1


class _Context:
    def __init__(self, args: argparse.Namespace, dataset: Dataset, cfg: LabConfig) -> None:
        self.args = args
        self.dataset = dataset
        self.cfg = cfg
        self.admin = Gateway(cfg.gateway_url, require_token("admin"), source="lab-measure")
        self.viewer = Gateway(cfg.gateway_url, require_token("viewer"), source="lab-measure")
        self.psql = Psql(cfg.containers.postgres, cfg.pg_superuser)
        self.psql_ro = Psql(cfg.containers.postgres, RO_ROLE)
        self.catalog_t0, self.target_t0 = _t0_paths(dataset, SNAPSHOTS_DIR)
        if not self.catalog_t0.is_file() or not self.target_t0.is_file():
            raise LabError(f"no T0 snapshot for {dataset.root.name}; run `lab import` first")
        self.questions_path = args.questions or dataset.root / "questions.json"
        self.internal_calls: list[str] = []

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.dataset.root / "manifest.json").read_text(encoding="utf-8"))

    def restore_t0(self) -> None:
        register.wait_until_idle(self.admin)
        restore_catalog(self.psql, self.cfg.catalog_db, self.catalog_t0, is_idle=lambda: not _running(self.admin))
        restore_target(self.psql, self.dataset.db_name, self.target_t0, catalog_db=self.cfg.catalog_db)

    def execute(self, sql: str):
        return execute(self.psql_ro, self.dataset.db_name, sql)


def _run_discover(context: _Context, run_dir: RunDir) -> dict[str, Any]:
    context.restore_t0()
    ontology = fetch_model(context.admin, context.psql, context.cfg.catalog_db, context.dataset.db_name)
    manifest = context.manifest()
    result = {"healthy": True, **score_discovery(manifest, ontology)}
    terms = (manifest.get("semantic") or {}).get("terms")
    if terms:
        result["business_terms"] = score_business_terms(terms, ontology, lambda sql: context.execute(sql).rows)
    run_dir.write_json("discover.json", result)
    return result


def _run_ask(context: _Context, run_dir: RunDir) -> dict[str, Any]:
    context.restore_t0()
    questions = load_questions(context.questions_path)
    if context.args.limit:
        questions = questions[: context.args.limit]
    modes = _build_modes(context)
    with (run_dir.path / "ask.jsonl").open("a", encoding="utf-8", newline="\n") as raw:

        def sink(row: dict[str, Any]) -> None:
            # Written row by row, so an interrupted paid run still leaves evidence.
            raw.write(json.dumps(row, ensure_ascii=False) + "\n")
            raw.flush()

        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        results = run_ask(questions, modes, context.args.runs, context.execute, sink)
    summary = summarize(results["rows"], results["excluded"], usable=results["usable"], runs=context.args.runs)
    skipped = skipped_steps_since(context.cfg.containers.backend, started)
    answers = sum(row["mode"] == "gateway" for row in results["rows"])
    summary["skipped_steps"] = skipped
    # A pipeline that skips its steps can still return plausible SQL, so accuracy alone would hide it.
    summary["healthy"] = summary["healthy"] and skipped_rate_ok(skipped, answers)
    run_dir.write_json("ask.json", summary)
    return summary


def _run_evolve(context: _Context, run_dir: RunDir) -> dict[str, Any]:
    root = context.dataset.root
    spec, mutations = load_evolve_spec(root)
    if context.args.mutation:
        mutations = [m for m in mutations if m.id == context.args.mutation]
    if not mutations:
        raise LabError(f"no mutations to run in {root / 'mutations'}")
    questions = {q.id: q for q in load_questions(context.questions_path)}
    probe_ids = list(spec.probes) + [qid for m in mutations for qid in m.probes]
    unknown = sorted({qid for qid in probe_ids if qid not in questions})
    if unknown:
        raise LabError(f"evolve.json lists unknown probe ids: {', '.join(unknown)}")
    stack = LiveStack(
        admin=context.admin,
        viewer=context.viewer,
        psql=context.psql,
        psql_ro=context.psql_ro,
        cfg=context.cfg,
        dataset=context.dataset,
        ro_password=_require_env(_RO_PASSWORD_ENV),
        questions=questions,
        catalog_t0=context.catalog_t0,
        target_t0=context.target_t0,
    )
    results = []
    for mutation in mutations:
        print(f"  {mutation.id}: {mutation.title}", flush=True)
        result = run_mutation(stack, spec, mutation)
        results.append(result)
        # Saved per mutation: each one can take a full rebuild, and a later failure must not lose it.
        run_dir.write_json(f"evolve_{mutation.id}.json", result)
    context.internal_calls.extend(stack.internal_calls)
    # Leave the stack as measurement found it, not in the last mutation's state.
    context.restore_t0()
    summary = {
        "healthy": all(result["healthy"] for result in results),
        "patch": False,
        "mutations": [result["id"] for result in results],
    }
    run_dir.write_json("evolve.json", summary)
    return summary


def _run_audit(context: _Context, run_dir: RunDir) -> dict[str, Any]:
    root = context.dataset.root
    path = root / "unanswerable.json"
    if not path.is_file():
        raise LabError(f"{path} not found; it lists out-of-schema questions as a JSON array of strings")
    context.restore_t0()
    retry_from = getattr(context.args, "retry_from", None)
    if retry_from is not None:
        # Re-ask only infrastructure failures; the PII audit is deterministic, so its findings are carried over.
        previous = json.loads((retry_from / "audit.json").read_text(encoding="utf-8"))
        unanswerable = retry_infrastructure(previous["unanswerable"], context.viewer, context.dataset.db_name, context.execute)
        unanswerable["retried_from"] = retry_from.name
        result = {"healthy": unanswerable["healthy"], "unanswerable": unanswerable, "pii": previous["pii"]}
        run_dir.write_json("audit.json", result)
        return result
    unanswerable = run_unanswerable(context.viewer, json.loads(path.read_text(encoding="utf-8")), context.dataset.db_name, context.execute)

    # PII changes the stack's flag and re-ingests, so it runs last and T0 is restored after it.
    context.admin.put("/api/configurations/pii-detection", {"enabled": True})
    stack = _live_stack_for_ingest(context)
    stack.run_stage("t2")
    context.internal_calls.extend(stack.internal_calls)
    samples = read_pii_samples(context.psql, context.cfg.catalog_db, context.dataset.db_name)
    findings = pii_findings(samples, lambda value: count_vector_rows(context.psql, context.cfg.catalog_db, value))
    context.restore_t0()
    result = {"healthy": unanswerable["healthy"], "unanswerable": unanswerable, "pii": findings}
    run_dir.write_json("audit.json", result)
    return result


def _live_stack_for_ingest(context: _Context) -> LiveStack:
    return LiveStack(
        admin=context.admin,
        viewer=context.viewer,
        psql=context.psql,
        psql_ro=context.psql_ro,
        cfg=context.cfg,
        dataset=context.dataset,
        ro_password="",
        questions={},
        catalog_t0=context.catalog_t0,
        target_t0=context.target_t0,
    )


def _build_modes(context: _Context) -> dict[str, Generator]:
    names = context.args.modes.split(",") if context.args.modes else list(ALL_MODES)
    unknown = [name for name in names if name not in ALL_MODES]
    if unknown:
        raise LabError(f"unknown ask mode: {', '.join(unknown)}")
    modes: dict[str, Generator] = {}
    for name in names:
        if name == "gateway":
            modes[name] = gateway_generator(context.viewer, context.dataset.db_name)
        else:
            schema = _schema_text(context.dataset, with_metadata=name == "ddl+meta")
            modes[name] = llm_generator(_llm_client(context.cfg), schema)
    return modes


def _llm_client(cfg: LabConfig) -> LlmClient:
    role = cfg.models.get("reasoning") or {}
    if not role.get("endpoint") or not role.get("model"):
        raise LabError("models.reasoning needs endpoint and model in the freeze file for the baseline modes")
    return LlmClient(role["endpoint"], role["model"], _require_env(_LLM_KEY_ENV))


def _schema_text(dataset: Dataset, *, with_metadata: bool) -> str:
    files = sorted((dataset.root / "ddl").glob("*.sql"))
    if not files:
        raise LabError(f"{dataset.root / 'ddl'} has no SQL files")
    schema = "\n".join(path.read_text(encoding="utf-8") for path in files)
    if not with_metadata:
        return schema
    metadata = dataset.root / "metadata.json"
    if not metadata.is_file():
        raise LabError(f"{metadata} not found; the ddl+meta mode needs column descriptions")
    return f"{schema}\n\nColumn descriptions (JSON):\n{metadata.read_text(encoding='utf-8')}"


def _running(admin: Gateway) -> bool:
    status = admin.get("/api/semantic-compilation/status")
    return bool((status.get("data") or status).get("running"))


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise LabError(f"{name} is not set")
    return value


def _provenance(
    args: argparse.Namespace, dataset: Dataset, cfg: LabConfig, context: _Context, results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    questions = context.questions_path
    return {
        "dataset": dataset.dataset_id,
        "manifest_sha256": file_digest(dataset.root / "manifest.json"),
        "questions_sha256": file_digest(questions) if questions.is_file() else None,
        "upstream_commit": cfg.upstream_commit,
        "alembic_head": cfg.alembic_head,
        "models": cfg.models,
        "flags": cfg.flags,
        "patch": False,
        "runs": args.runs,
        "limit": args.limit,
        "suites": {suite: bool(result.get("healthy")) for suite, result in results.items()},
        # PLAN.md principle 1: every use of the internal port is disclosed.
        "internal_port_calls": sorted(set(context.internal_calls)),
        "last_compile_run_id": latest_run_id(context.psql, cfg.catalog_db),
    }


_SUITE_RUNNERS = {"discover": _run_discover, "ask": _run_ask, "evolve": _run_evolve, "audit": _run_audit}
