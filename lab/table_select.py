"""Choose which tables `lab clone --auto-select` keeps: those with data, plus the parents they need.

Every table costs LLM calls during compile, and real schemas often hold many empty ones. Selecting
changes what is measured, since Auto-Ontology then has fewer tables to confuse, so a result on a
selected dataset must say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TableSelection:
    with_data: frozenset[str]
    required_parents: frozenset[str]
    dropped: frozenset[str]

    @property
    def kept(self) -> frozenset[str]:
        return self.with_data | self.required_parents

    def summary(self) -> str:
        total = len(self.kept) + len(self.dropped)
        return (
            f"Auto-selected {len(self.kept)} of {total} tables: {len(self.with_data)} with data, "
            f"{len(self.required_parents)} empty but required by a foreign key."
        )


def select_tables(profile: dict[str, Any]) -> TableSelection:
    """Keep non-empty tables and, transitively, every table their foreign keys point to."""
    tables = profile.get("tables") or {}
    with_data = {name for name, meta in tables.items() if meta.get("row_count")}
    parents_of: dict[str, set[str]] = {}
    for fk in profile.get("foreign_keys") or []:
        parents_of.setdefault(fk["from"]["table"], set()).add(fk["to"]["table"])

    kept = set(with_data)
    pending = list(with_data)
    while pending:
        for parent in parents_of.get(pending.pop(), ()):
            if parent in tables and parent not in kept:
                kept.add(parent)
                pending.append(parent)
    return TableSelection(frozenset(with_data), frozenset(kept - with_data), frozenset(set(tables) - kept))


def exclusion_regex(selection: TableSelection, user_regex: str | None = None) -> str | None:
    """Build the `--exclude` value that drops exactly the unselected tables."""
    parts = [f"^(?:{'|'.join(re.escape(name) for name in sorted(selection.dropped))})$"] if selection.dropped else []
    if user_regex:
        parts.append(f"(?:{user_regex})")
    return "|".join(parts) or None
