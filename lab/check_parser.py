"""Parser and constructive synthesizer for PostgreSQL CHECK constraints (§1.1, Appendix A)."""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
import numpy as np


_ANY_ARRAY_RE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*ANY\s*\(\s*ARRAY\[(.*?)\]\s*(?:::[a-zA-Z0-9_\[\]\s]+)?\)",
    re.IGNORECASE | re.DOTALL,
)
_IN_LIST_RE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+IN\s*\(([^()]+)\)",
    re.IGNORECASE,
)
_SUBARRAY_RE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*<@\s*ARRAY\[(.*?)\]",
    re.IGNORECASE | re.DOTALL,
)
_CARDINALITY_BETWEEN_RE = re.compile(
    r"cardinality\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)\s*BETWEEN\s*(\d+)\s*AND\s*(\d+)",
    re.IGNORECASE,
)
_CARDINALITY_CMP_RE = re.compile(
    r"cardinality\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)\s*(>=|>|<=|<|=)\s*(\d+)",
    re.IGNORECASE,
)
_CHAR_LEN_BETWEEN_RE = re.compile(
    r"(?:char_length|length)\(\s*(?:btrim\(\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\)?\s*\)\s*BETWEEN\s*(\d+)\s*AND\s*(\d+)",
    re.IGNORECASE,
)
_CHAR_LEN_CMP_RE = re.compile(
    r"(?:char_length|length)\(\s*(?:btrim\(\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\)?\s*\)\s*(>=|>|<=|<|=)\s*(\d+)",
    re.IGNORECASE,
)
_REGEX_MATCH_RE = re.compile(
    r"(?<![!~])\b([a-zA-Z_][a-zA-Z0-9_]*)\s*~\s*'([^']+)'",
    re.IGNORECASE,
)
_SPLIT_PART_EQ_RE = re.compile(
    r"split_part\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*'([^']+)'(?:::[a-zA-Z0-9_\s]+)?\s*,\s*1\s*\)\s*=\s*([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)
_SIMPLE_COL_CMP_NUM_RE = re.compile(
    r"^\s*CHECK\s*\(\s*\(*\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(>=|>|<=|<)\s*(-?\d+(?:\.\d+)?)\s*(?:::[a-zA-Z0-9_\s]+)?\)*\s*\)\s*$",
    re.IGNORECASE,
)
_SIMPLE_COL_BETWEEN_NUM_RE = re.compile(
    r"^\s*CHECK\s*\(\s*\(*\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:>=|>)\s*(-?\d+(?:\.\d+)?)\s*(?:::[a-zA-Z0-9_\s]+)?\)*\s+AND\s+\(*\s*\1\s*(?:<=|<)\s*(-?\d+(?:\.\d+)?)\s*(?:::[a-zA-Z0-9_\s]+)?\)*\s*\)\s*$",
    re.IGNORECASE,
)
_SIMPLE_COL_BETWEEN_KW_RE = re.compile(
    r"^\s*CHECK\s*\(\s*\(*\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+BETWEEN\s+(-?\d+(?:\.\d+)?)\s+AND\s+(-?\d+(?:\.\d+)?)\s*\)*\s*\)\s*$",
    re.IGNORECASE,
)
_PAIR_CMP_RE = re.compile(
    r"^\s*CHECK\s*\(\s*\(*\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(>=|>|<=|<|<>|!=)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)*\s*\)\s*$",
    re.IGNORECASE,
)


def _extract_array_literals(raw_inner: str) -> list[str]:
    """Parse string or numeric literals inside ARRAY[...] or IN (...)."""
    literals: list[str] = []
    for match in re.finditer(r"'((?:''|[^'])*)'(?:::[a-zA-Z0-9_\s]+)?|(?<![a-zA-Z0-9_'])(-?\d+(?:\.\d+)?)(?![a-zA-Z0-9_'])", raw_inner):
        if match.group(1) is not None:
            literals.append(match.group(1).replace("''", "'"))
        elif match.group(2) is not None:
            literals.append(match.group(2))
    return literals


@dataclass
class TableCheckHints:
    """Constructive constraints extracted from a table's CHECK definitions."""

    in_values: dict[str, list[str]] = field(default_factory=dict)
    array_elements: dict[str, list[str]] = field(default_factory=dict)
    array_card_bounds: dict[str, tuple[int, int]] = field(default_factory=dict)
    char_len_bounds: dict[str, tuple[int, int]] = field(default_factory=dict)
    regex_patterns: dict[str, str] = field(default_factory=dict)
    numeric_bounds: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    pair_comparisons: list[tuple[str, str, str]] = field(default_factory=list)  # (col_a, op, col_b)
    split_part_prefix: list[tuple[str, str, str]] = field(default_factory=list)  # (target_col, sep, prefix_col)


def parse_table_checks(checks: list[dict[str, str]], column_names: set[str]) -> TableCheckHints:
    """Extract constructive generation hints from a list of {'name': ..., 'expr': ...} checks."""
    hints = TableCheckHints()

    for chk in checks:
        raw_expr = chk.get("expr", "").strip()
        if not raw_expr:
            continue

        # Strip benign '<col> IS NULL OR' guards on nullable columns
        expr = re.sub(
            r"\(\s*[a-zA-Z_][a-zA-Z0-9_]*\s+IS\s+NULL\s*\)\s*OR\s+|\b[a-zA-Z_][a-zA-Z0-9_]*\s+IS\s+NULL\s+OR\s+",
            "",
            raw_expr,
            flags=re.IGNORECASE,
        )

        has_or = bool(re.search(r"\bOR\b", expr, re.IGNORECASE))
        has_not = bool(re.search(r"\bNOT\b", expr, re.IGNORECASE))

        # 1. Extract = ANY (ARRAY[...]) and IN (...) when not inside an OR/NOT branch
        if not has_or and not has_not:
            for m in _ANY_ARRAY_RE.finditer(expr):
                col = m.group(1)
                if col in column_names:
                    vals = _extract_array_literals(m.group(2))
                    if vals:
                        hints.in_values[col] = vals
            for m in _IN_LIST_RE.finditer(expr):
                col = m.group(1)
                if col in column_names and col not in hints.in_values:
                    vals = _extract_array_literals(m.group(2))
                    if vals:
                        hints.in_values[col] = vals

            # 2. Array <@ ARRAY[...] and cardinality(col)
            for m in _SUBARRAY_RE.finditer(expr):
                col = m.group(1)
                if col in column_names:
                    vals = _extract_array_literals(m.group(2))
                    if vals:
                        hints.array_elements[col] = vals

            for m in _CARDINALITY_BETWEEN_RE.finditer(expr):
                col = m.group(1)
                if col in column_names:
                    lo, hi = int(m.group(2)), int(m.group(3))
                    cur_lo, cur_hi = hints.array_card_bounds.get(col, (0, 10))
                    hints.array_card_bounds[col] = (max(cur_lo, lo), min(cur_hi, hi))

            for m in _CARDINALITY_CMP_RE.finditer(expr):
                col, op, val_s = m.group(1), m.group(2), int(m.group(3))
                if col in column_names:
                    cur_lo, cur_hi = hints.array_card_bounds.get(col, (0, 10))
                    if op == ">=":
                        cur_lo = max(cur_lo, val_s)
                    elif op == ">":
                        cur_lo = max(cur_lo, val_s + 1)
                    elif op == "<=":
                        cur_hi = min(cur_hi, val_s)
                    elif op == "<":
                        cur_hi = min(cur_hi, val_s - 1)
                    elif op == "=":
                        cur_lo = cur_hi = val_s
                    hints.array_card_bounds[col] = (cur_lo, max(cur_lo, cur_hi))

            # 3. char_length(btrim(col)) bounds
            for m in _CHAR_LEN_BETWEEN_RE.finditer(expr):
                col = m.group(1)
                if col in column_names:
                    lo, hi = int(m.group(2)), int(m.group(3))
                    cur_lo, cur_hi = hints.char_len_bounds.get(col, (1, 200))
                    hints.char_len_bounds[col] = (max(cur_lo, lo), min(cur_hi, hi))

            for m in _CHAR_LEN_CMP_RE.finditer(expr):
                col, op, val_s = m.group(1), m.group(2), int(m.group(3))
                if col in column_names:
                    cur_lo, cur_hi = hints.char_len_bounds.get(col, (1, 200))
                    if op == ">=":
                        cur_lo = max(cur_lo, val_s)
                    elif op == ">":
                        cur_lo = max(cur_lo, val_s + 1)
                    elif op == "<=":
                        cur_hi = min(cur_hi, val_s)
                    elif op == "<":
                        cur_hi = min(cur_hi, val_s - 1)
                    elif op == "=":
                        cur_lo = cur_hi = val_s
                    hints.char_len_bounds[col] = (cur_lo, max(cur_lo, cur_hi))

            # 4. Regex match: col ~ 'pattern'
            for m in _REGEX_MATCH_RE.finditer(expr):
                col, pat = m.group(1), m.group(2)
                if col in column_names:
                    hints.regex_patterns[col] = pat

            # 5. split_part(colA, '.', 1) = colB
            for m in _SPLIT_PART_EQ_RE.finditer(expr):
                col_a, sep, col_b = m.group(1), m.group(2), m.group(3)
                if col_a in column_names and col_b in column_names:
                    hints.split_part_prefix.append((col_a, sep, col_b))

        # 6. Simple single-column numeric bounds
        m_cmp = _SIMPLE_COL_CMP_NUM_RE.match(expr)
        if m_cmp:
            col, op, num_s = m_cmp.group(1), m_cmp.group(2), float(m_cmp.group(3))
            if col in column_names:
                lo, hi = hints.numeric_bounds.get(col, (None, None))
                if op == ">=":
                    lo = num_s if lo is None else max(lo, num_s)
                elif op == ">":
                    lo = (num_s + 1) if lo is None else max(lo, num_s + 1)
                elif op == "<=":
                    hi = num_s if hi is None else min(hi, num_s)
                elif op == "<":
                    hi = (num_s - 1) if hi is None else min(hi, num_s - 1)
                hints.numeric_bounds[col] = (lo, hi)

        m_bet = _SIMPLE_COL_BETWEEN_NUM_RE.match(expr) or _SIMPLE_COL_BETWEEN_KW_RE.match(expr)
        if m_bet:
            col, lo_s, hi_s = m_bet.group(1), float(m_bet.group(2)), float(m_bet.group(3))
            if col in column_names:
                lo, hi = hints.numeric_bounds.get(col, (None, None))
                lo = lo_s if lo is None else max(lo, lo_s)
                hi = hi_s if hi is None else min(hi, hi_s)
                hints.numeric_bounds[col] = (lo, hi)

        # 7. Simple pairwise column comparison: colA >= colB, colA <> colB
        m_pair = _PAIR_CMP_RE.match(expr)
        if m_pair:
            col_a, op, col_b = m_pair.group(1), m_pair.group(2), m_pair.group(3)
            if col_a in column_names and col_b in column_names:
                hints.pair_comparisons.append((col_a, op, col_b))

    return hints


def extract_referenced_columns(expr: str, column_names: set[str]) -> list[str]:
    """Return the sorted list of table columns referenced in a CHECK expression."""
    found: set[str] = set()
    for match in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", expr):
        ident = match.group(1)
        if ident in column_names:
            found.add(ident)
    return sorted(found)


def _expand_char_class(cls_spec: str) -> str:
    """Expand a regex character class like 'a-z0-9_-' into a string of characters."""
    chars: list[str] = []
    i = 0
    n = len(cls_spec)
    while i < n:
        if i + 2 < n and cls_spec[i + 1] == "-":
            start_c, end_c = cls_spec[i], cls_spec[i + 2]
            for code in range(ord(start_c), ord(end_c) + 1):
                chars.append(chr(code))
            i += 3
        elif cls_spec[i] == "\\" and i + 1 < n:
            nxt = cls_spec[i + 1]
            if nxt == "d":
                chars.extend(string.digits)
            elif nxt == "w":
                chars.extend(string.ascii_lowercase + string.digits + "_")
            else:
                chars.append(nxt)
            i += 2
        else:
            chars.append(cls_spec[i])
            i += 1
    return "".join(dict.fromkeys(chars)) or string.ascii_lowercase


def generate_string_from_regex(pattern: str, rng: np.random.Generator, seq_idx: int = 0) -> str:
    """Deterministically synthesize a string matching common PostgreSQL ~ regex constraints."""
    pat = pattern.strip()
    if pat.startswith("^"):
        pat = pat[1:]
    if pat.endswith("$"):
        pat = pat[:-1]

    out: list[str] = []
    i = 0
    n = len(pat)

    while i < n:
        token_chars: list[str] | None = None
        token_alts: list[str] | None = None

        if pat[i] == "(":
            # Find matching ')'
            depth = 0
            j = i
            while j < n:
                if pat[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if pat[j] == "(":
                    depth += 1
                elif pat[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            group_body = pat[i + 1 : j]
            if group_body.startswith("?:"):
                group_body = group_body[2:]
            token_alts = group_body.split("|")
            i = j + 1
        elif pat[i] == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                j = n
            cls_str = _expand_char_class(pat[i + 1 : j])
            token_chars = list(cls_str)
            i = j + 1
        elif pat[i] == "\\" and i + 1 < n:
            nxt = pat[i + 1]
            if nxt == "d":
                token_chars = list(string.digits)
            elif nxt == "w":
                token_chars = list(string.ascii_lowercase + string.digits + "_")
            else:
                token_chars = [nxt]
            i += 2
        elif pat[i] == ".":
            token_chars = list(string.ascii_lowercase)
            i += 1
        else:
            token_chars = [pat[i]]
            i += 1

        # Check quantifier
        min_rep = 1
        max_rep = 1
        if i < n and pat[i] == "{":
            k = pat.find("}", i + 1)
            if k != -1:
                q_body = pat[i + 1 : k]
                if "," in q_body:
                    lo_s, _, hi_s = q_body.partition(",")
                    min_rep = int(lo_s.strip() or "0")
                    max_rep = int(hi_s.strip()) if hi_s.strip() else min_rep + 6
                else:
                    min_rep = max_rep = int(q_body.strip())
                i = k + 1
        elif i < n and pat[i] == "+":
            min_rep, max_rep = 2, 6
            i += 1
        elif i < n and pat[i] == "*":
            min_rep, max_rep = 0, 4
            i += 1
        elif i < n and pat[i] == "?":
            min_rep, max_rep = 0, 1
            i += 1

        # Keep generated strings compact and unique-friendly
        target_upper = min(max_rep, max(min_rep, min(min_rep + 6, max_rep)))
        rep = int(rng.integers(min_rep, target_upper + 1)) if target_upper > min_rep else min_rep

        for _ in range(rep):
            if token_alts is not None:
                choice = token_alts[int(rng.integers(0, len(token_alts)))]
                # Anchored, so only the whole pattern can receive the unanchored-prefix suffix below.
                out.append(generate_string_from_regex(f"^{choice}$", rng, seq_idx))
            elif token_chars is not None:
                out.append(token_chars[int(rng.integers(0, len(token_chars)))])

    result = "".join(out)
    # If pattern is an unanchored prefix (does not end with '$'), append a deterministic suffix
    if not pattern.strip().endswith("$"):
        if result.endswith("://"):
            result = f"{result}example.org/room-{seq_idx + 1}"
        else:
            result = f"{result}{seq_idx + 1}"
    return result
