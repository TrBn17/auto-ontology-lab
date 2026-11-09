"""`lab report`: turn raw results in reports/runs/ into reports/RESULTS.md.

Raw metrics only. Statistics are computed per question, because the 3 runs of one question are not
independent samples: the confidence interval resamples questions, and McNemar pairs questions.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from lab.common.errors import LabError

RUNS_DIR = Path("reports/runs")
DATASETS_DIR = Path("datasets")
EXIT_UNHEALTHY = 3
MAX_INFRASTRUCTURE_RATE = 0.05
BASELINES = ("ddl", "ddl+meta")
_BOOTSTRAP = 10_000
_UPSTREAM_DEFAULT_NON_REASONING = "nvidia/nemotron-3.5-lightning-30b-a3b"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", default="edu-public")
    parser.add_argument("--run", action="append", type=Path, default=None, help="Run directory to include (repeatable). Default: the newest run of each suite.")
    parser.add_argument("--out", type=Path, default=Path("reports/RESULTS.md"))


def run(args: argparse.Namespace) -> int:
    runs = args.run or _newest_runs(RUNS_DIR)
    if not runs:
        raise LabError(f"no runs found in {RUNS_DIR}")
    tags = _load_tags(DATASETS_DIR / args.dataset / "question_tags.json")
    text, healthy = render(runs, tags, _load_expectations(DATASETS_DIR / args.dataset / "mutations"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Wrote {args.out} from {', '.join(str(r) for r in runs)}")
    return 0 if healthy else EXIT_UNHEALTHY


# ---------------------------------------------------------------- statistics


def per_question(rows: list[dict[str, Any]]) -> dict[str, list[bool]]:
    """Question id -> correctness in each run, in run order. Infrastructure failures are left out."""
    result: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    for row in rows:
        if row["outcome"] != "infrastructure":
            result[row["id"]].append((row["run"], row["outcome"] == "correct"))
    return {qid: [ok for _, ok in sorted(seen)] for qid, seen in result.items()}


def bootstrap_ci(scores: list[float], *, seed: int = 0, resamples: int = _BOOTSTRAP) -> tuple[float, float]:
    """95% percentile interval of the mean, resampling questions."""
    if not scores:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    n = len(scores)
    means = sorted(sum(rng.choice(scores) for _ in range(n)) / n for _ in range(resamples))
    return (means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1])


def mcnemar_exact(a: dict[str, bool], b: dict[str, bool]) -> dict[str, Any]:
    """Two-sided exact McNemar on questions both modes answered. `only_a`: a right and b wrong."""
    shared = sorted(set(a) & set(b))
    only_a = sum(a[q] and not b[q] for q in shared)
    only_b = sum(b[q] and not a[q] for q in shared)
    n = only_a + only_b
    p = 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2**n)
    return {"questions": len(shared), "only_a": only_a, "only_b": only_b, "p": p}


def majority(correct_by_run: list[bool]) -> bool:
    return sum(correct_by_run) * 2 > len(correct_by_run)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]


def summarize_mode(rows: list[dict[str, Any]], runs: int) -> dict[str, Any]:
    questions = per_question(rows)
    infra = sum(row["outcome"] == "infrastructure" for row in rows)
    expected = len({row["id"] for row in rows}) * runs
    by_run = []
    for r in range(runs):
        in_run = [row for row in rows if row["run"] == r and row["outcome"] != "infrastructure"]
        if in_run:
            by_run.append(sum(row["outcome"] == "correct" for row in in_run) / len(in_run))
    scores = [sum(v) / len(v) for v in questions.values()]
    mean = sum(scores) / len(scores) if scores else math.nan
    std = _stdev(by_run)
    seconds = [row["seconds"] for row in rows if "seconds" in row]
    return {
        "results": len(rows),
        "expected": expected,
        "infrastructure": infra,
        "healthy": expected > 0 and len(rows) == expected and infra / expected <= MAX_INFRASTRUCTURE_RATE,
        "accuracy_by_run": by_run,
        "accuracy_mean": mean,
        "accuracy_std": std,
        "ci95": bootstrap_ci(scores),
        "flipped": sorted(q for q, v in questions.items() if len(set(v)) > 1),
        "questions": questions,
        "p50": percentile(seconds, 0.5),
        "p95": percentile(seconds, 0.95),
        "outcomes": Counter(row["outcome"] for row in rows if row["outcome"] != "correct"),
    }


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


# ---------------------------------------------------------------- rendering


def render(
    run_dirs: list[Path], tags: dict[str, list[str]], expectations: dict[str, set[str]] | None = None
) -> tuple[str, bool]:
    measure = _first_json(run_dirs, "measure.json")
    out = ["# Auto-Ontology Lab — Results", ""]
    healthy = True
    out += _render_freeze(measure)
    out += _render_disclosures(measure)
    discover = _first_json(run_dirs, "discover.json")
    if discover:
        out += _render_discover(discover)
        healthy &= bool(discover.get("healthy", True))
    rows = _first_jsonl(run_dirs, "ask.jsonl")
    if rows:
        section, ok = _render_ask(rows, tags)
        out += section
        healthy &= ok
    mutations = _apply_expectations(_mutation_results(run_dirs), expectations or {})
    if mutations:
        out += _render_evolve(mutations)
        healthy &= all(m.get("healthy") for m in mutations)
    data = _first_json(run_dirs, "audit.json")
    if data:
        out += _render_audit(data)
        healthy &= bool(data.get("healthy", True))
    out += ["## Sources", ""] + [f"- `{d.as_posix()}`" for d in run_dirs] + [""]
    if not healthy:
        out.insert(2, "> **Not a headline result:** at least one suite failed its health check or is incomplete.\n")
    return "\n".join(out), healthy


def _render_audit(data: dict[str, Any]) -> list[str]:
    lines = ["## audit", ""]
    unanswerable = data.get("unanswerable") or {}
    rows = unanswerable.get("questions") or []
    if rows:
        counts = unanswerable.get("counts") or {}
        summary = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
        lines += [f"**Unanswerable questions** ({len(rows)}, out of the schema): {summary}.", ""]
        if unanswerable.get("retried"):
            lines += [
                f"Re-asked after an infrastructure failure in `{unanswerable.get('retried_from', '?')}`: "
                + "; ".join(unanswerable["retried"]),
                "",
            ]
        lines += ["| Question | Outcome | SQL |", "|---|---|---|"]
        for row in rows:
            sql = (row.get("sql") or row.get("detail") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {row['question']} | {row['outcome']} | `{sql[:160]}` |" if sql else f"| {row['question']} | {row['outcome']} | |")
        lines.append("")
    pii = data.get("pii") or []
    if pii:
        lines += ["**PII left in the catalog** (columns named like PII):", "", "| Column | Raw samples stored | Vector rows repeating one |", "|---|---|---|"]
        lines += [f"| `{p['column']}` | {p['raw_samples']} | {p['vector_rows_containing_a_sample']} |" for p in pii]
        lines.append("")
    return lines


def _render_freeze(measure: dict[str, Any] | None) -> list[str]:
    if not measure:
        return []
    lines = ["## Freeze", "", "| Item | Value |", "|---|---|"]
    lines.append(f"| Upstream commit | `{measure.get('upstream_commit', '?')}` |")
    lines.append(f"| Alembic head | `{measure.get('alembic_head', '?')}` |")
    for role, info in sorted((measure.get("models") or {}).items()):
        lines.append(f"| Model: {role} | `{info.get('model')}` |")
    lines.append(f"| Dataset | `{measure.get('dataset')}` (manifest `{str(measure.get('manifest_sha256', ''))[:12]}`) |")
    lines.append(f"| Questions | `{str(measure.get('questions_sha256', ''))[:12]}` |")
    flags = measure.get("flags") or {}
    lines.append("| Flags | " + ", ".join(f"`{k}={v}`" for k, v in sorted(flags.items())) + " |")
    return lines + [""]


def _render_disclosures(measure: dict[str, Any] | None) -> list[str]:
    measure = measure or {}
    lines = ["## Deviations from a stock install", ""]
    if (measure.get("flags") or {}).get("llm_proxy"):
        lines.append("- Chat models go through `lab/proxy/llm_proxy.py`, which only rewrites the structured-output request format (upstream issue #440, `docs/FINDINGS.md` F2).")
    model = ((measure.get("models") or {}).get("non_reasoning") or {}).get("model")
    if model and model != _UPSTREAM_DEFAULT_NON_REASONING:
        lines.append(f"- `NON_REASONING_MODEL` is `{model}`, not upstream's default `{_UPSTREAM_DEFAULT_NON_REASONING}`, which exhausts its token limit on most calls (F4).")
    lines.append("- Tables are owned by a `NOLOGIN` role granted to `lab_ro`, so upstream's connector can read foreign keys (F6). A read-only role alone yields 0 of 18 FKs.")
    if measure.get("internal_port_calls"):
        lines.append(f"- Internal port calls: {', '.join(measure['internal_port_calls'])}.")
    if measure.get("patch"):
        lines.append("- **Patched run** (`--patch`).")
    return lines + [""]


def _render_discover(d: dict[str, Any]) -> list[str]:
    lines = ["## discover", "", "| Target | Precision | Recall | F1 | Missed |", "|---|---|---|---|---|"]
    for key, label in (("entities", "Entities"), ("physical_fk", "Physical FK"), ("semantic_fk", "Semantic FK")):
        m = d.get(key) or {}
        missed = ", ".join(_name(x) for x in m.get("missed") or []) or "—"
        lines.append(f"| {label} | {_pct(m.get('precision'))} | {_pct(m.get('recall'))} | {_pct(m.get('f1'))} | {missed} |")
    invented = d.get("invented_relationships") or []
    lines += ["", f"Invented relationships: {len(invented)}." + ("" if not invented else " " + ", ".join(_name(x) for x in invented)), ""]
    return lines


def _render_ask(rows: list[dict[str, Any]], tags: dict[str, list[str]]) -> tuple[list[str], bool]:
    runs = max(row["run"] for row in rows) + 1
    modes = {m: summarize_mode([r for r in rows if r["mode"] == m], runs) for m in sorted({r["mode"] for r in rows})}
    healthy = all(m["healthy"] for m in modes.values()) and "gateway" in modes
    n_questions = len({r["id"] for r in rows})
    lines = [
        "## ask (Text-to-SQL)",
        "",
        f"{n_questions} questions × {runs} runs. Accuracy is the mean over questions of the share of runs answered correctly; "
        "the 95% interval resamples questions (bootstrap, 10,000 draws).",
        "",
        "| Mode | Accuracy | 95% CI | By run | Flipped | P50 s | P95 s | Healthy |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, m in modes.items():
        lo, hi = m["ci95"]
        by_run = " / ".join(_pct(x) for x in m["accuracy_by_run"])
        health = "yes" if m["healthy"] else f"**no** ({m['results']}/{m['expected']}, {m['infrastructure']} infra)"
        lines.append(
            f"| `{name}` | {_pct(m['accuracy_mean'])} ± {_pct(m['accuracy_std'])} | {_pct(lo)}–{_pct(hi)} | {by_run} "
            f"| {len(m['flipped'])} | {_num(m['p50'])} | {_num(m['p95'])} | {health} |"
        )

    if "gateway" in modes:
        gate = {q: majority(v) for q, v in modes["gateway"]["questions"].items()}
        lines += ["", "**Gateway vs baselines** (exact McNemar on per-question majority over runs):", "",
                  "| Baseline | Questions | Only gateway right | Only baseline right | p |", "|---|---|---|---|---|"]
        for base in BASELINES:
            if base in modes:
                t = mcnemar_exact(gate, {q: majority(v) for q, v in modes[base]["questions"].items()})
                lines.append(f"| `{base}` | {t['questions']} | {t['only_a']} | {t['only_b']} | {t['p']:.3f} |")

    lines += ["", "**By category** (4–5 questions each: directional only):", ""]
    lines += _category_table(rows, modes, tags)
    lines += _failures(rows, modes)
    return lines + [""], healthy


def _category_table(rows: list[dict[str, Any]], modes: dict[str, Any], tags: dict[str, list[str]]) -> list[str]:
    implicit = set(tags.get("implicit_filter", []))
    group: dict[str, str] = {}
    for row in rows:
        cat = row["category"]
        if cat == "soft_delete" and implicit:
            cat = "soft_delete (implicit)" if row["id"] in implicit else "soft_delete (explicit)"
        group[row["id"]] = cat
    names = list(modes)
    lines = ["| Category | n | " + " | ".join(f"`{m}`" for m in names) + " |", "|---|---|" + "---|" * len(names)]
    for cat in sorted(set(group.values())):
        ids = [q for q, c in group.items() if c == cat]
        cells = []
        for m in names:
            scores = [sum(v) / len(v) for q, v in modes[m]["questions"].items() if q in ids]
            cells.append(_pct(sum(scores) / len(scores)) if scores else "—")
        lines.append(f"| {cat} | {len(ids)} | " + " | ".join(cells) + " |")
    if implicit:
        lines += ["", "*Implicit* soft-delete questions say \"students\" without saying \"current\"; the gold excludes deleted rows. "
                  "Read them as a test of an unstated convention, not of SQL skill."]
    return lines


def _failures(rows: list[dict[str, Any]], modes: dict[str, Any]) -> list[str]:
    if "gateway" not in modes:
        return []
    lines = ["", "### Gateway failures", ""]
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["mode"] == "gateway":
            by_q[row["id"]].append(row)
    for qid in sorted(by_q):
        wrong = [r for r in sorted(by_q[qid], key=lambda r: r["run"]) if r["outcome"] != "correct"]
        if not wrong:
            continue
        first = wrong[0]
        kinds = ", ".join(sorted({r["outcome"] for r in wrong}))
        lines += [
            f"**{qid}** ({first['category']}) — wrong in {len(wrong)}/{len(by_q[qid])} runs ({kinds}): {first['question']}",
            "",
            "```sql",
            f"-- gold\n{first['gold_sql'].strip()}\n-- predicted (run {first['run']})\n{(first.get('sql') or '(none)').strip()}",
            "```",
        ]
        if first.get("detail"):
            lines.append(f"Detail: `{str(first['detail'])[:300]}`")
        lines.append("")
    return lines


def _mutation_results(dirs: list[Path]) -> list[dict[str, Any]]:
    """The newest result of each mutation across runs, since mutations may run one per invocation."""
    newest: dict[str, dict[str, Any]] = {}
    for d in sorted(dirs):
        for path in sorted(d.glob("evolve_m*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            newest[data["id"]] = data
    return [newest[k] for k in sorted(newest)]


def _assertion_key(a: dict[str, Any]) -> str:
    return json.dumps({k: a[k] for k in sorted(a) if k != "passed"}, sort_keys=True)


def _load_expectations(mutations_dir: Path) -> dict[str, set[str]]:
    result = {}
    for path in sorted(mutations_dir.glob("*.json")) if mutations_dir.is_dir() else []:
        item = json.loads(path.read_text(encoding="utf-8"))
        result[item["id"]] = {_assertion_key(a) for a in item.get("expect") or []}
    return result


def _apply_expectations(mutations: list[dict[str, Any]], expectations: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Score against the current spec: assertions removed from it since the run are dropped and listed."""
    for m in mutations:
        keep = expectations.get(m["id"])
        if keep is None:
            continue
        dropped: list[dict[str, Any]] = []
        for s in m.get("stages") or []:
            assertions = s.get("assertions") or []
            dropped += [a for a in assertions if _assertion_key(a) not in keep]
            s["assertions"] = [a for a in assertions if _assertion_key(a) in keep]
            s["reflected"] = all(a.get("passed") for a in s["assertions"])
        m["dropped_assertions"] = sorted({_assertion_key(a) for a in dropped})
    return mutations


def _render_evolve(mutations: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## evolve (vanilla)",
        "",
        "T2 = re-ingest, T3 = compile trigger, T4 = semantic reset. Each cell: schema change reflected "
        "(assertions passed), edits kept out of 6, probes that regressed, seconds.",
        "",
        "| Mutation | Probes before | T2 | T3 | T4 |",
        "|---|---|---|---|---|",
    ]
    for m in mutations:
        base = m.get("baseline_probes") or []
        before = f"{sum(p['outcome'] == 'correct' for p in base)}/{len(base)}" if base else "—"
        stages = {s["stage"]: s for s in m.get("stages") or []}
        cells = [_stage_cell(stages.get(name)) for name in ("t2", "t3", "t4")]
        title = f"`{m['id']}` {m.get('title', '')}"
        lines.append(f"| {title} | {before} | " + " | ".join(cells) + " |")
    for m in mutations:
        if m.get("dropped_assertions"):
            lines.append(f"\n`{m['id']}`: scored without assertions removed from its spec after the run: "
                         + "; ".join(f"`{d}`" for d in m["dropped_assertions"]))
    errors = [m for m in mutations if m.get("error")]
    if errors:
        lines += ["", "**Failed mutations:**"] + [f"- `{m['id']}`: {str(m['error'])[:200]}" for m in errors]
    lines += ["", "<details><summary>Failed assertions</summary>", ""]
    for m in mutations:
        for s in m.get("stages") or []:
            failed = [a for a in s.get("assertions") or [] if not a.get("passed")]
            for a in failed:
                target = a.get("column") or a.get("table") or _name(a.get("columns"))
                lines.append(f"- `{m['id']}` {s['stage'].upper()}: `{a.get('kind')}` {target}")
            for rel in s.get("unexpected_relationships") or []:
                lines.append(f"- `{m['id']}` {s['stage'].upper()}: unexpected relationship {_name(rel)}")
    return lines + ["", "</details>", ""]


def _stage_cell(stage: dict[str, Any] | None) -> str:
    if stage is None:
        return "not run"
    assertions = stage.get("assertions") or []
    passed = sum(bool(a.get("passed")) for a in assertions)
    mark = "✓" if stage.get("reflected") else "✗"
    regressed = stage.get("probes_regressed")
    probes = "—" if regressed is None else (", ".join(regressed) or "none")
    return (
        f"{mark} {passed}/{len(assertions)}; edits {stage.get('edits_surviving')}/{stage.get('edits_total')}; "
        f"regressed {probes}; {_num(stage.get('seconds'))}s"
    )


# ---------------------------------------------------------------- helpers


def _newest_runs(root: Path) -> list[Path]:
    """The newest run directory holding each suite's output."""
    if not root.is_dir():
        return []
    chosen: list[Path] = []
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    for name in ("discover.json", "ask.jsonl", "audit.json"):
        found = next((d for d in dirs if (d / name).is_file()), None)
        if found and found not in chosen:
            chosen.append(found)
    # Mutations may be spread over several runs; take every run that holds one.
    chosen += [d for d in dirs if any(d.glob("evolve_m*.json")) and d not in chosen]
    return sorted(chosen)


def _first_json(dirs: list[Path], name: str) -> dict[str, Any] | None:
    for d in sorted(dirs, reverse=True):
        if (d / name).is_file():
            return json.loads((d / name).read_text(encoding="utf-8"))
    return None


def _first_jsonl(dirs: list[Path], name: str) -> list[dict[str, Any]]:
    for d in sorted(dirs, reverse=True):
        if (d / name).is_file():
            return [json.loads(line) for line in (d / name).read_text(encoding="utf-8").splitlines() if line.strip()]
    return []


def _load_tags(path: Path) -> dict[str, list[str]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _name(x: Any) -> str:
    return " → ".join(x) if isinstance(x, (list, tuple)) else str(x)


def _pct(x: float | None) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.0f}%"


def _num(x: float | None) -> str:
    return "—" if x is None else f"{x:.0f}"
