"""Execution planning: row count scaling (A4), topological FK sort, and cycle breaking (Appendix A)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class UnbreakableFKCycleError(RuntimeError):
    """Raised when an FK cycle contains no nullable edge to break the cycle (exit code 1)."""


@dataclass
class GenerationPlan:
    """Deterministic generation order and row targets derived from profile.json."""

    table_order: list[str]
    target_row_counts: dict[str, int]
    table_fks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    self_fks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    deferred_cycle_fks: list[dict[str, Any]] = field(default_factory=list)
    implicit_target_unique_cols: dict[str, list[list[str]]] = field(default_factory=dict)


def compute_target_row_count(source_rows: int, scale: float, max_rows: int = 50_000) -> int:
    """Compute target row count per A4: min(row_count, max(50, round(row_count * scale))), capped by max_rows."""
    if source_rows <= 0:
        return 0
    scaled = max(50, int(round(source_rows * scale)))
    return min(max_rows, min(source_rows, scaled))


def build_generation_plan(
    profile: dict[str, Any],
    *,
    scale: float = 0.1,
    max_rows: int = 50_000,
) -> GenerationPlan:
    """Build a deterministic topological generation plan from profile.json."""
    tables = profile.get("tables") or {}
    all_tables = sorted(tables.keys())

    target_counts = {
        tbl: compute_target_row_count(int(meta.get("row_count", 0)), scale, max_rows)
        for tbl, meta in tables.items()
    }

    self_fks: dict[str, list[dict[str, Any]]] = {t: [] for t in all_tables}
    inter_fks: list[dict[str, Any]] = []
    implicit_target_unique_cols: dict[str, list[list[str]]] = {t: [] for t in all_tables}

    for fk in profile.get("foreign_keys") or []:
        from_tbl = fk["from"]["table"]
        to_tbl = fk["to"]["table"]
        if from_tbl not in tables or to_tbl not in tables:
            continue
        if fk.get("kind") == "implicit":
            to_cols = list(fk["to"]["columns"])
            if to_cols not in implicit_target_unique_cols[to_tbl]:
                implicit_target_unique_cols[to_tbl].append(to_cols)

        if from_tbl == to_tbl:
            self_fks[from_tbl].append(fk)
        else:
            inter_fks.append(fk)

    # Active edges for topological sort: child -> parent
    active_fks = list(inter_fks)
    deferred_cycle_fks: list[dict[str, Any]] = []
    ordered: list[str] = []
    remaining = set(all_tables)

    while remaining:
        # Tables in `remaining` that have no active FK pointing to another table in `remaining`
        ready = sorted(
            t
            for t in remaining
            if not any(
                fk["from"]["table"] == t and fk["to"]["table"] in remaining
                for fk in active_fks
            )
        )
        if ready:
            for t in ready:
                ordered.append(t)
                remaining.remove(t)
            continue

        # A cycle exists among `remaining`. Find a cycle and break it at a nullable FK edge.
        cycle_edges = _find_cycle_edges(remaining, active_fks)
        nullable_candidates = [fk for fk in cycle_edges if fk.get("nullable", False)]
        if not nullable_candidates:
            cycle_desc = " -> ".join(
                f"{fk['from']['table']}({','.join(fk['from']['columns'])})" for fk in cycle_edges
            )
            raise UnbreakableFKCycleError(
                f"FK cycle with no nullable edge cannot be broken: {cycle_desc}"
            )

        # Deterministic selection of the nullable edge to defer
        nullable_candidates.sort(
            key=lambda fk: (fk["from"]["table"], fk.get("name", ""), fk["to"]["table"])
        )
        chosen = nullable_candidates[0]
        active_fks.remove(chosen)
        deferred_cycle_fks.append(chosen)

    table_fks: dict[str, list[dict[str, Any]]] = {t: [] for t in all_tables}
    for fk in active_fks:
        table_fks[fk["from"]["table"]].append(fk)

    return GenerationPlan(
        table_order=ordered,
        target_row_counts=target_counts,
        table_fks=table_fks,
        self_fks=self_fks,
        deferred_cycle_fks=deferred_cycle_fks,
        implicit_target_unique_cols=implicit_target_unique_cols,
    )


def _find_cycle_edges(
    remaining_tables: set[str],
    active_fks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find edges participating in a directed dependency cycle among remaining_tables."""
    adj: dict[str, list[dict[str, Any]]] = {t: [] for t in remaining_tables}
    for fk in active_fks:
        u = fk["from"]["table"]
        v = fk["to"]["table"]
        if u in remaining_tables and v in remaining_tables:
            adj[u].append(fk)

    visited: set[str] = set()
    in_stack: dict[str, int] = {}
    stack_nodes: list[str] = []
    stack_edges: list[dict[str, Any]] = []

    for start in sorted(remaining_tables):
        if start in visited:
            continue

        def _dfs(node: str) -> list[dict[str, Any]] | None:
            visited.add(node)
            in_stack[node] = len(stack_nodes)
            stack_nodes.append(node)
            for fk in adj.get(node, []):
                nxt = fk["to"]["table"]
                if nxt in in_stack:
                    idx = in_stack[nxt]
                    return stack_edges[idx:] + [fk]
                if nxt not in visited:
                    stack_edges.append(fk)
                    res = _dfs(nxt)
                    if res is not None:
                        return res
                    stack_edges.pop()
            stack_nodes.pop()
            del in_stack[node]
            return None

        cycle = _dfs(start)
        if cycle:
            return cycle

    # Fallback: all remaining edges
    return [
        fk
        for fk in active_fks
        if fk["from"]["table"] in remaining_tables and fk["to"]["table"] in remaining_tables
    ]
