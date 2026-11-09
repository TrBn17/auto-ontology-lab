"""Deterministic synthetic data generator implementing Appendix A and A1–A13."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import math
import re
import string
from typing import Any
import uuid
from faker import Faker
import numpy as np
import psycopg
from psycopg import sql

from lab.check_parser import (
    TableCheckHints,
    extract_referenced_columns,
    generate_string_from_regex,
    parse_table_checks,
)
from lab.dsn import ParsedDSN
from lab.plan import GenerationPlan, build_generation_plan


@dataclass
class GenerationResult:
    """Output of synthetic row generation."""

    rows_by_table: dict[str, list[dict[str, Any]]]
    column_order_by_table: dict[str, list[str]]
    degraded_checks: list[dict[str, Any]] = field(default_factory=list)
    repaired_checks: list[dict[str, Any]] = field(default_factory=list)


def _make_rng(seed: int, label: str) -> np.random.Generator:
    """Create a process-independent deterministic numpy RNG from sha256(seed:label)."""
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).digest()
    seed_ints = np.frombuffer(digest, dtype=np.uint32)
    return np.random.default_rng(seed_ints)


def _make_faker(seed: int, label: str, locale: str = "en_US") -> Faker:
    """Create a process-independent seeded Faker instance."""
    digest = hashlib.sha256(f"faker:{seed}:{label}".encode("utf-8")).digest()
    seed_int = int.from_bytes(digest[:8], "big")
    fk = Faker(locale)
    fk.seed_instance(seed_int)
    return fk


def _largest_remainder_counts(total: int, weights: dict[str, float]) -> dict[str, int]:
    """Allocate `total` integer slots across keys of `weights` using Largest Remainder Method."""
    if total <= 0 or not weights:
        return {k: 0 for k in weights}
    keys = list(weights.keys())
    w_sum = sum(max(0.0, float(weights[k])) for k in keys)
    if w_sum <= 0:
        norm = {k: 1.0 / len(keys) for k in keys}
    else:
        norm = {k: max(0.0, float(weights[k])) / w_sum for k in keys}

    exact = {k: norm[k] * total for k in keys}
    floors = {k: int(math.floor(exact[k])) for k in keys}
    rem = total - sum(floors.values())

    # Sort by remainder descending, tie-breaking deterministically by key
    order = sorted(keys, key=lambda k: (-(exact[k] - floors[k]), str(k)))
    for k in order[:rem]:
        floors[k] += 1
    return floors


def allocate_null_mask(n_rows: int, null_rate: float, nullable: bool, rng: np.random.Generator) -> list[bool]:
    """Allocate an exact quota of NULL booleans, stratified across the first 1,000 rows (A6, A11)."""
    if n_rows <= 0:
        return []
    if not nullable or null_rate <= 0.0:
        return [False] * n_rows
    if null_rate >= 1.0:
        return [True] * n_rows

    total_nulls = int(round(null_rate * n_rows))
    total_nulls = max(0, min(n_rows, total_nulls))

    if n_rows <= 1000:
        mask = [True] * total_nulls + [False] * (n_rows - total_nulls)
        perm = rng.permutation(n_rows)
        return [mask[int(i)] for i in perm]

    # Stratify: first 1,000 rows and remaining rows
    nulls_first = max(0, min(1000, int(round(null_rate * 1000))))
    nulls_first = min(nulls_first, total_nulls)
    if total_nulls - nulls_first > n_rows - 1000:
        nulls_first = total_nulls - (n_rows - 1000)
    nulls_rest = total_nulls - nulls_first

    mask_first = [True] * nulls_first + [False] * (1000 - nulls_first)
    perm_first = rng.permutation(1000)
    part1 = [mask_first[int(i)] for i in perm_first]

    n_rest = n_rows - 1000
    mask_rest = [True] * nulls_rest + [False] * (n_rest - nulls_rest)
    perm_rest = rng.permutation(n_rest)
    part2 = [mask_rest[int(i)] for i in perm_rest]

    return part1 + part2


def allocate_categorical_column(
    n_rows: int,
    null_rate: float,
    nullable: bool,
    weights: dict[str, float],
    rng: np.random.Generator,
) -> list[str | None]:
    """Allocate exact categorical and null quotas, stratified for the first 1,000 rows (A6, A7, A11)."""
    if n_rows <= 0:
        return []
    null_mask = allocate_null_mask(n_rows, null_rate, nullable, rng)
    if not weights:
        return [None] * n_rows

    if n_rows <= 1000:
        n_non_null = sum(1 for is_null in null_mask if not is_null)
        counts = _largest_remainder_counts(n_non_null, weights)
        pool: list[str] = []
        for k, cnt in counts.items():
            pool.extend([k] * cnt)
        perm = rng.permutation(len(pool))
        shuffled_pool = [pool[int(i)] for i in perm]
        it = iter(shuffled_pool)
        return [None if is_null else next(it) for is_null in null_mask]

    # Stratified allocation for first 1,000 rows and remainder
    mask_first = null_mask[:1000]
    mask_rest = null_mask[1000:]
    n_non_null_total = sum(1 for is_null in null_mask if not is_null)
    n_non_null_first = sum(1 for is_null in mask_first if not is_null)

    total_counts = _largest_remainder_counts(n_non_null_total, weights)
    first_counts = _largest_remainder_counts(n_non_null_first, weights)

    # Clamp first_counts so it never exceeds total_counts
    for k in first_counts:
        if first_counts[k] > total_counts[k]:
            first_counts[k] = total_counts[k]
    diff = n_non_null_first - sum(first_counts.values())
    if diff > 0:
        for k in sorted(first_counts.keys()):
            avail = total_counts[k] - first_counts[k]
            add = min(diff, avail)
            first_counts[k] += add
            diff -= add
            if diff == 0:
                break

    rest_counts = {k: total_counts[k] - first_counts[k] for k in total_counts}

    pool_first: list[str] = []
    for k, cnt in first_counts.items():
        pool_first.extend([k] * cnt)
    perm1 = rng.permutation(len(pool_first))
    shuffled1 = [pool_first[int(i)] for i in perm1]
    it1 = iter(shuffled1)
    out1 = [None if is_null else next(it1) for is_null in mask_first]

    pool_rest: list[str] = []
    for k, cnt in rest_counts.items():
        pool_rest.extend([k] * cnt)
    perm2 = rng.permutation(len(pool_rest))
    shuffled2 = [pool_rest[int(i)] for i in perm2]
    it2 = iter(shuffled2)
    out2 = [None if is_null else next(it2) for is_null in mask_rest]

    return out1 + out2


def _coerce_categorical_value(val: str | None, pg_type: str) -> Any:
    if val is None:
        return None
    t = pg_type.lower()
    if t == "boolean" or t == "bool":
        return val.lower() == "true"
    if any(t.startswith(p) for p in ("smallint", "integer", "bigint", "int")):
        return int(float(val))
    if any(t.startswith(p) for p in ("numeric", "decimal", "real", "double", "float")):
        return float(val)
    return val


def _generate_uuid_from_rng(rng: np.random.Generator) -> str:
    raw = bytearray(rng.bytes(16))
    raw[6] = (raw[6] & 0x0F) | 0x40  # version 4
    raw[8] = (raw[8] & 0x3F) | 0x80  # variant RFC 4122
    return str(uuid.UUID(bytes=bytes(raw)))


def _parse_iso_date(val: Any, default_date: date) -> date:
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val[:10])
        except ValueError:
            pass
    return default_date


def _parse_iso_timestamp(val: Any, default_dt: datetime) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        cleaned = val.strip().replace(" ", "T")
        if cleaned.endswith("+00"):
            cleaned += ":00"
        elif cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return default_dt


def _varchar_limit(pg_type: str) -> int | None:
    m = re.search(r"\((\d+)\)", pg_type)
    if m and ("char" in pg_type.lower() or "varying" in pg_type.lower()):
        return int(m.group(1))
    return None


def _draw_single_non_null_value(
    col_name: str,
    col_meta: dict[str, Any],
    check_hints: TableCheckHints,
    rng: np.random.Generator,
    faker_inst: Faker,
    row_idx: int,
    *,
    force_unique: bool = False,
) -> Any:
    """Draw a single non-null value for a column according to its profile metadata and CHECK hints."""
    pg_type = str(col_meta.get("type", "text"))
    low_type = pg_type.lower()

    # 1. Regex CHECK hint
    if col_name in check_hints.regex_patterns:
        pat = check_hints.regex_patterns[col_name]
        val = generate_string_from_regex(pat, rng, row_idx)
        return val

    # 2. Categorical or allowed_values
    if "categorical" in col_meta and col_meta["categorical"]:
        cats = list(col_meta["categorical"].keys())
        probs = np.array([float(col_meta["categorical"][k]) for k in cats], dtype=float)
        probs = probs / probs.sum() if probs.sum() > 0 else np.ones(len(cats)) / len(cats)
        chosen = cats[int(rng.choice(len(cats), p=probs))]
        return _coerce_categorical_value(chosen, pg_type)

    allowed_vals = col_meta.get("allowed_values") or check_hints.in_values.get(col_name)
    if allowed_vals:
        vals = list(allowed_vals)
        chosen = vals[int(rng.integers(0, len(vals)))]
        return _coerce_categorical_value(chosen, pg_type)

    # 3. Array column
    if col_meta.get("is_array"):
        card_info = col_meta.get("cardinality") or {"min": 1, "max": 2}
        c_min = int(card_info.get("min", 1))
        c_max = max(c_min, int(card_info.get("max", 2)))
        k = int(rng.integers(c_min, c_max + 1)) if c_max > c_min else c_min
        allowed_elems = col_meta.get("allowed_elements") or check_hints.array_elements.get(col_name)
        if allowed_elems:
            k = min(k, len(allowed_elems))
            if k <= 0:
                return []
            idxs = rng.choice(len(allowed_elems), size=k, replace=False)
            return [allowed_elems[int(i)] for i in sorted(idxs)]
        base_t = str(col_meta.get("base_type", "text")).lower()
        if "int" in base_t:
            return [int(rng.integers(1, 50)) for _ in range(k)]
        return [f"tag_{int(rng.integers(1, 20))}" for _ in range(k)]

    # 4. JSON / JSONB
    if col_meta.get("is_json"):
        return "{}"

    # 5. UUID
    if "uuid" in low_type:
        return _generate_uuid_from_rng(rng)

    # 6. Boolean
    if low_type in ("boolean", "bool"):
        return bool(rng.integers(0, 2))

    # 7. Integer types
    if any(low_type.startswith(p) for p in ("smallint", "integer", "bigint", "int")):
        if col_meta.get("is_identity"):
            return row_idx + 1
        pii_cat = col_meta.get("pii")
        if pii_cat == "phone" and "bigint" in low_type:
            lo, hi = 2_000_000_000, 9_999_999_999
        else:
            lo = int(col_meta.get("min", 1))
            hi = int(col_meta.get("max", max(lo + 100, 100)))
        chk_lo, chk_hi = check_hints.numeric_bounds.get(col_name, (None, None))
        if chk_lo is not None:
            lo = max(lo, int(math.ceil(chk_lo)))
        if chk_hi is not None:
            hi = min(hi, int(math.floor(chk_hi)))
        if hi < lo:
            hi = lo + 50
        if force_unique:
            return lo + row_idx
        return int(rng.integers(lo, hi + 1))

    # 8. Floating / Numeric types
    if any(low_type.startswith(p) for p in ("numeric", "decimal", "real", "double", "float")):
        lo_f = float(col_meta.get("min", 1.0))
        hi_f = float(col_meta.get("max", max(lo_f + 100.0, 100.0)))
        chk_lo, chk_hi = check_hints.numeric_bounds.get(col_name, (None, None))
        if chk_lo is not None:
            lo_f = max(lo_f, float(chk_lo))
        if chk_hi is not None:
            hi_f = min(hi_f, float(chk_hi))
        if hi_f < lo_f:
            hi_f = lo_f + 10.0
        scale_digits = 2
        m_num = re.search(r"numeric\(\d+\s*,\s*(\d+)\)", low_type)
        if m_num:
            scale_digits = int(m_num.group(1))
        val_f = float(rng.uniform(lo_f, hi_f))
        return round(val_f, scale_digits)

    # 9. Date
    if low_type == "date":
        pii_cat = col_meta.get("pii")
        if pii_cat == "dob":
            d_min, d_max = date(1965, 1, 1), date(2004, 12, 31)
        else:
            d_min = _parse_iso_date(col_meta.get("min"), date(2021, 1, 1))
            d_max = _parse_iso_date(col_meta.get("max"), date(2025, 12, 31))
        if d_max < d_min:
            d_max = d_min + timedelta(days=365)
        span = (d_max - d_min).days
        offset = int(rng.integers(0, span + 1)) if span > 0 else 0
        return (d_min + timedelta(days=offset)).isoformat()

    # 10. Timestamp
    if low_type.startswith("timestamp"):
        dt_min = _parse_iso_timestamp(
            col_meta.get("min"),
            datetime(2021, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
        dt_max = _parse_iso_timestamp(
            col_meta.get("max"),
            datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        )
        if dt_max < dt_min:
            dt_max = dt_min + timedelta(days=365)
        span_sec = int((dt_max - dt_min).total_seconds())
        offset_sec = int(rng.integers(0, span_sec + 1)) if span_sec > 0 else 0
        res_dt = dt_min + timedelta(seconds=offset_sec)
        return res_dt.isoformat()

    # 11. Time
    if low_type.startswith("time"):
        hour = int(rng.integers(8, 20))
        minute = int(rng.choice([0, 15, 30, 45]))
        return time(hour, minute, 0).isoformat()

    # 12. Text / Varchar / Char
    len_info = col_meta.get("len") or {"min": 5, "max": 24, "avg": 12.0}
    min_l = max(1, int(len_info.get("min", 4)))
    max_l = max(min_l, int(len_info.get("max", 32)))
    if col_name in check_hints.char_len_bounds:
        c_lo, c_hi = check_hints.char_len_bounds[col_name]
        min_l = max(min_l, c_lo)
        max_l = min(max(max_l, c_lo), c_hi)
    vc_lim = _varchar_limit(pg_type)
    if vc_lim is not None:
        max_l = min(max_l, vc_lim)
        min_l = min(min_l, max_l)

    pii_cat = col_meta.get("pii")
    if force_unique and not pii_cat:
        prefix = re.sub(r"[^a-z0-9]", "", col_name.lower())[:8] or "key"
        candidate = f"{prefix}_{row_idx + 1:05d}"
    elif pii_cat == "email":
        candidate = f"user{row_idx + 1}_{int(rng.integers(100, 999))}@example.org"
    elif pii_cat == "phone":
        candidate = f"+1555{int(rng.integers(1000000, 9999999))}"
    elif pii_cat == "name":
        candidate = faker_inst.name()
    elif pii_cat == "company":
        candidate = f"{faker_inst.company()} {row_idx + 1}" if force_unique else faker_inst.company()
    elif pii_cat == "slug":
        slug_base = faker_inst.slug()
        candidate = f"{slug_base}-{row_idx + 1}"
    elif pii_cat == "address":
        candidate = faker_inst.street_address()
    elif pii_cat == "ssn":
        candidate = f"{int(rng.integers(100, 899))}-{int(rng.integers(10, 99))}-{int(rng.integers(1000, 9999))}"
    elif pii_cat == "passport":
        candidate = f"P{int(rng.integers(10000000, 99999999))}"
    elif pii_cat == "iban":
        candidate = f"GB{int(rng.integers(10, 99))}MOCK{int(rng.integers(1000000000, 9999999999))}"
    elif pii_cat == "card":
        candidate = f"card_{int(rng.integers(1000, 9999))}"
    else:
        target_len = int(rng.integers(min_l, max(min_l, min(max_l, 80)) + 1))
        if target_len <= 12:
            word = faker_inst.word()
            candidate = f"{word}_{int(rng.integers(10, 99))}"
        else:
            candidate = faker_inst.text(max_nb_chars=max(20, target_len + 15)).replace("\n", " ").strip()

    candidate = candidate.strip()
    if len(candidate) < min_l:
        pad = "x" * (min_l - len(candidate))
        candidate = f"{candidate}{pad}"
    if len(candidate) > max_l:
        candidate = candidate[:max_l].rstrip()
        if len(candidate) < min_l:
            candidate = (candidate + ("a" * min_l))[:max_l]
    return candidate


def _group_overlapping_fks(fks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group FKs on a single table into connected components that share source columns."""
    if not fks:
        return []
    n = len(fks)
    visited = [False] * n
    groups: list[list[dict[str, Any]]] = []

    for i in range(n):
        if visited[i]:
            continue
        comp_indices = [i]
        visited[i] = True
        cols_in_comp = set(fks[i]["from"]["columns"])
        changed = True
        while changed:
            changed = False
            for j in range(n):
                if not visited[j]:
                    j_cols = set(fks[j]["from"]["columns"])
                    if cols_in_comp & j_cols:
                        visited[j] = True
                        comp_indices.append(j)
                        cols_in_comp.update(j_cols)
                        changed = True
        comp = [fks[idx] for idx in comp_indices]
        comp.sort(
            key=lambda fk: (
                bool(fk.get("nullable", False)),
                -len(fk["from"]["columns"]),
                -float(fk.get("coverage", 1.0)),
                fk.get("name", ""),
            )
        )
        groups.append(comp)
    return groups


def _assign_fk_component(
    comp: list[dict[str, Any]],
    n_rows: int,
    rows: list[dict[str, Any]],
    rows_by_table: dict[str, list[dict[str, Any]]],
    rng: np.random.Generator,
) -> None:
    """Assign parent key tuples for a connected component of FKs on a child table (A3, A5, A12)."""
    if n_rows <= 0 or not comp:
        return

    non_nullable_comp_cols: set[str] = {
        c for fk in comp if not fk.get("nullable", False) for c in fk["from"]["columns"]
    }

    primary_fk = comp[0]
    p_table = primary_fk["to"]["table"]
    p_to_cols = primary_fk["to"]["columns"]
    p_from_cols = primary_fk["from"]["columns"]
    parent_rows = rows_by_table.get(p_table, [])

    # Extract distinct parent key tuples in order
    seen_p: set[tuple[Any, ...]] = set()
    parent_tuples: list[tuple[Any, ...]] = []
    for pr in parent_rows:
        tup = tuple(pr[c] for c in p_to_cols)
        if any(v is None for v in tup):
            continue
        if tup not in seen_p:
            seen_p.add(tup)
            parent_tuples.append(tup)

    # Filter parent_tuples so shared column values exist in all non-nullable secondary parent tables
    for sec_fk in comp[1:]:
        if sec_fk.get("nullable", False):
            continue
        s_table = sec_fk["to"]["table"]
        s_to_cols = sec_fk["to"]["columns"]
        s_from_cols = sec_fk["from"]["columns"]
        s_parent_rows = rows_by_table.get(s_table, [])

        shared_positions: list[int] = []
        primary_positions: list[int] = []
        for idx_s, c_s in enumerate(s_from_cols):
            if c_s in p_from_cols:
                shared_positions.append(idx_s)
                primary_positions.append(p_from_cols.index(c_s))
        if not primary_positions:
            continue

        valid_shared: set[tuple[Any, ...]] = set()
        for spr in s_parent_rows:
            full_tup = tuple(spr[c] for c in s_to_cols)
            if any(v is None for v in full_tup):
                continue
            valid_shared.add(tuple(full_tup[pos] for pos in shared_positions))

        filtered = [
            pt for pt in parent_tuples if tuple(pt[pos] for pos in primary_positions) in valid_shared
        ]
        if filtered:
            parent_tuples = filtered

    if not parent_tuples:
        for fk in comp:
            for c in fk["from"]["columns"]:
                if c not in non_nullable_comp_cols:
                    for r in rows:
                        r[c] = None
        return

    # Allocate null mask for primary FK
    null_mask = allocate_null_mask(
        n_rows,
        float(primary_fk.get("null_rate", 0.0)),
        bool(primary_fk.get("nullable", False)),
        rng,
    )
    n_non_null = sum(1 for is_null in null_mask if not is_null)

    # Check if any column in p_from_cols was already populated earlier (e.g. deferred cycle FK sharing org_id)
    pre_populated_cols = [
        c for c in p_from_cols if rows[0].get(c) is not None and any(r.get(c) is not None for r in rows)
    ] if len(comp) == 1 and primary_fk.get("nullable", False) and len(p_from_cols) > 1 else []

    if pre_populated_cols and len(pre_populated_cols) < len(p_from_cols):
        pre_positions = [p_from_cols.index(c) for c in pre_populated_cols]
        lookup_p: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        for pt in parent_tuples:
            sh_key = tuple(pt[pos] for pos in pre_positions)
            lookup_p.setdefault(sh_key, []).append(pt)
        for row_i, is_null in enumerate(null_mask):
            if is_null:
                for c in p_from_cols:
                    if c not in pre_populated_cols:
                        rows[row_i][c] = None
            else:
                sh_key = tuple(rows[row_i].get(c) for c in pre_populated_cols)
                cands = lookup_p.get(sh_key, [])
                if cands:
                    picked = cands[int(rng.integers(0, len(cands)))]
                    for c, val in zip(p_from_cols, picked):
                        rows[row_i][c] = val
                else:
                    for c in p_from_cols:
                        if c not in pre_populated_cols:
                            rows[row_i][c] = None
        return

    # Target active parents to match source coverage (A5)
    src_coverage = float(primary_fk.get("coverage", 1.0))
    n_parents = len(parent_tuples)
    target_active = min(
        n_parents,
        min(n_non_null, max(1, int(round(src_coverage * n_parents)))),
    )

    if target_active > 0 and n_non_null > 0:
        active_idxs = rng.choice(n_parents, size=target_active, replace=False)
        active_parents = [parent_tuples[int(i)] for i in sorted(active_idxs)]
        n_act = len(active_parents)
        cpp = primary_fk.get("children_per_parent") or {}
        src_max = int(cpp.get("max") or 0)
        src_avg = float(cpp.get("avg") or 0.0)
        even_ceil = int(math.ceil(n_non_null / n_act))
        if src_max > 0 and abs(src_max - src_avg) < 1e-6:
            per_parent_cap = even_ceil
        elif src_max > 0:
            per_parent_cap = max(even_ceil, src_max)
        else:
            per_parent_cap = max(even_ceil, n_non_null)

        # Give each active parent 1 child first, then distribute remaining slots respecting per_parent_cap
        counts = [1] * n_act
        chosen_tuples: list[tuple[Any, ...]] = list(active_parents)
        rem = n_non_null - len(chosen_tuples)
        eligible = [idx for idx in range(n_act) if counts[idx] < per_parent_cap]
        for _ in range(rem):
            if not eligible:
                eligible = list(range(n_act))
            pick_pos = int(rng.integers(0, len(eligible)))
            p_idx = eligible[pick_pos]
            chosen_tuples.append(active_parents[p_idx])
            counts[p_idx] += 1
            if counts[p_idx] >= per_parent_cap and len(eligible) > 1:
                eligible.pop(pick_pos)
            elif counts[p_idx] >= per_parent_cap:
                eligible = []
        perm = rng.permutation(len(chosen_tuples))
        shuffled_tuples = [chosen_tuples[int(i)] for i in perm]
    else:
        shuffled_tuples = []

    it_primary = iter(shuffled_tuples)
    for row_i, is_null in enumerate(null_mask):
        if is_null:
            for c in p_from_cols:
                if c not in non_nullable_comp_cols:
                    rows[row_i][c] = None
        else:
            tup = next(it_primary)
            for c, val in zip(p_from_cols, tup):
                rows[row_i][c] = val

    already_set_cols: set[str] = set(p_from_cols)

    # Populate secondary FKs in the component
    for sec_fk in comp[1:]:
        s_table = sec_fk["to"]["table"]
        s_to_cols = sec_fk["to"]["columns"]
        s_from_cols = sec_fk["from"]["columns"]
        s_parent_rows = rows_by_table.get(s_table, [])

        shared_positions = [idx_s for idx_s, c_s in enumerate(s_from_cols) if c_s in already_set_cols]
        shared_col_names = [s_from_cols[pos] for pos in shared_positions]
        new_cols = [c_s for c_s in s_from_cols if c_s not in already_set_cols]

        if not new_cols and not sec_fk.get("nullable", False):
            # All columns of sec_fk were already set by a wider non-nullable FK
            continue

        lookup: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        seen_sec_p: set[tuple[Any, ...]] = set()
        for spr in s_parent_rows:
            full_tup = tuple(spr[c] for c in s_to_cols)
            if any(v is None for v in full_tup) or full_tup in seen_sec_p:
                continue
            seen_sec_p.add(full_tup)
            shared_key = tuple(full_tup[pos] for pos in shared_positions)
            lookup.setdefault(shared_key, []).append(full_tup)

        sec_null_mask = allocate_null_mask(
            n_rows,
            float(sec_fk.get("null_rate", 0.0)),
            bool(sec_fk.get("nullable", False)),
            rng,
        )
        n_sec_non_null = sum(1 for is_null in sec_null_mask if not is_null)

        # If sec_fk has both nulls and non-nulls, stratify non-null slots across shared_keys
        # proportionally to len(lookup[key]) so every shared_key gets enough non-null rows to cover its parents
        if 0 < n_sec_non_null < n_rows and shared_col_names:
            rows_by_key: dict[tuple[Any, ...], list[int]] = {}
            for r_i, r_d in enumerate(rows):
                if any(r_d.get(c_n) is None for c_n in shared_col_names):
                    continue
                k_sh = tuple(r_d.get(c_n) for c_n in shared_col_names)
                if k_sh in lookup:
                    rows_by_key.setdefault(k_sh, []).append(r_i)
            if rows_by_key:
                total_avail_cands = sum(len(lookup[k]) for k in rows_by_key) or 1
                key_weights = {
                    k: len(lookup[k]) / total_avail_cands for k in rows_by_key
                }
                raw_quotas = _largest_remainder_counts(n_sec_non_null, key_weights)
                # Clamp quotas to len(rows_by_key[k]) and redistribute any surplus
                clamped: dict[tuple[Any, ...], int] = {
                    k: min(raw_quotas.get(k, 0), len(rows_by_key[k])) for k in rows_by_key
                }
                deficit = n_sec_non_null - sum(clamped.values())
                for k in sorted(rows_by_key.keys(), key=lambda x: str(x)):
                    while deficit > 0 and clamped[k] < len(rows_by_key[k]):
                        clamped[k] += 1
                        deficit -= 1
                new_mask = [True] * n_rows
                for k, r_indices in rows_by_key.items():
                    q_k = clamped.get(k, 0)
                    if q_k > 0:
                        chosen_r = rng.choice(len(r_indices), size=q_k, replace=False)
                        for c_pos in chosen_r:
                            new_mask[r_indices[int(c_pos)]] = False
                sec_null_mask = new_mask

        cand_usage: dict[tuple[Any, ...], int] = {}
        used_with_primary: dict[tuple[Any, ...], set[tuple[Any, ...]]] = {}
        shuffled_full_lookup: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        for k_sh, c_list in lookup.items():
            if len(c_list) > 1:
                p_ord = rng.permutation(len(c_list))
                shuffled_full_lookup[k_sh] = [c_list[int(i)] for i in p_ord]
            else:
                shuffled_full_lookup[k_sh] = list(c_list)

        # Size active candidate pool in shuffled_lookup to match sec_fk's source coverage (A5)
        n_sec_parents = len(seen_sec_p)
        sec_cov = float(sec_fk.get("coverage", 1.0))
        target_sec_active = min(
            n_sec_parents,
            min(n_sec_non_null, max(1, int(round(sec_cov * n_sec_parents)))),
        )
        non_null_rows_per_key: dict[tuple[Any, ...], int] = {}
        for r_i, r_d in enumerate(rows):
            if sec_null_mask[r_i] or any(r_d.get(c_n) is None for c_n in shared_col_names):
                continue
            k_sh = tuple(r_d.get(c_n) for c_n in shared_col_names)
            if k_sh in shuffled_full_lookup:
                non_null_rows_per_key[k_sh] = non_null_rows_per_key.get(k_sh, 0) + 1

        shuffled_lookup: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        if non_null_rows_per_key and n_sec_parents > 0:
            active_keys = sorted(non_null_rows_per_key.keys(), key=lambda x: str(x))
            max_usable = {
                k: min(len(shuffled_full_lookup[k]), non_null_rows_per_key[k]) for k in active_keys
            }
            # Each active key with max_usable >= 1 needs at least 1 candidate
            active_quotas = {k: (1 if max_usable[k] >= 1 else 0) for k in active_keys}
            rem_active = min(target_sec_active, sum(max_usable.values())) - sum(active_quotas.values())
            if rem_active > 0:
                elig_keys = [k for k in active_keys if active_quotas[k] < max_usable[k]]
                while rem_active > 0 and elig_keys:
                    # Pick key with largest remaining capacity ratio
                    best_k = max(
                        elig_keys,
                        key=lambda k: (len(shuffled_full_lookup[k]) - active_quotas[k]) / max(len(shuffled_full_lookup[k]), 1),
                    )
                    active_quotas[best_k] += 1
                    rem_active -= 1
                    if active_quotas[best_k] >= max_usable[best_k]:
                        elig_keys.remove(best_k)
            for k_sh, full_list in shuffled_full_lookup.items():
                q_c = active_quotas.get(k_sh, len(full_list))
                shuffled_lookup[k_sh] = full_list[: max(1, q_c)]
        else:
            shuffled_lookup = dict(shuffled_full_lookup)

        nullable_cols_for_sec = [
            c_s for c_s in s_from_cols if c_s not in already_set_cols and c_s not in non_nullable_comp_cols
        ]

        for row_i in range(n_rows):
            row = rows[row_i]
            if sec_null_mask[row_i] and nullable_cols_for_sec:
                for c_s in nullable_cols_for_sec:
                    row[c_s] = None
                continue

            if any(row.get(c_name) is None for c_name in shared_col_names):
                for c_s in nullable_cols_for_sec:
                    row[c_s] = None
                continue

            # Match against lookup, preferring candidates not yet paired with this primary tuple
            # and balancing global candidate usage
            key = tuple(row.get(c_name) for c_name in shared_col_names)
            candidates = shuffled_lookup.get(key, [])
            if candidates:
                prim_tup = tuple(row.get(c) for c in p_from_cols)
                default_paired = {prim_tup} if (s_table == p_table and s_to_cols == p_to_cols) else set()
                already_paired = used_with_primary.setdefault(prim_tup, set(default_paired))
                unpaired = [c_tup for c_tup in candidates if c_tup not in already_paired]
                if not unpaired and default_paired:
                    full_cands = shuffled_full_lookup.get(key, candidates)
                    unpaired = [c_tup for c_tup in full_cands if c_tup not in already_paired]
                pool = unpaired if unpaired else [c_tup for c_tup in candidates if c_tup not in default_paired] or candidates
                picked = min(pool, key=lambda c_tup: cand_usage.get(c_tup, 0))
                already_paired.add(picked)
                cand_usage[picked] = cand_usage.get(picked, 0) + 1
                for c_s, val in zip(s_from_cols, picked):
                    row[c_s] = val
            else:
                for c_s in nullable_cols_for_sec:
                    row[c_s] = None

        already_set_cols.update(s_from_cols)


def _row_matches_partial_predicate(row: dict[str, Any], predicate: str | None) -> bool:
    """Evaluate a PostgreSQL partial index WHERE predicate against an in-memory row dict."""
    if not predicate:
        return True
    clauses = re.split(r"\bAND\b", predicate, flags=re.IGNORECASE)
    for raw_c in clauses:
        c = raw_c.strip()
        m_any = re.search(
            r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*ANY\s*\(\s*ARRAY\[(.*?)\]",
            c,
            re.IGNORECASE | re.DOTALL,
        )
        if m_any:
            col = m_any.group(1)
            allowed = set(re.findall(r"'([^']*)'", m_any.group(2)))
            if str(row.get(col)) not in allowed:
                return False
            continue
        c = c.lstrip("(").rstrip(")").strip()
        m_null = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s+IS\s+NULL$", c, re.IGNORECASE)
        if m_null:
            if row.get(m_null.group(1)) is not None:
                return False
            continue
        m_notnull = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s+IS\s+NOT\s+NULL$", c, re.IGNORECASE)
        if m_notnull:
            if row.get(m_notnull.group(1)) is None:
                return False
            continue
        m_bool_eq = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(true|false)$", c, re.IGNORECASE)
        if m_bool_eq:
            col, b_str = m_bool_eq.group(1), m_bool_eq.group(2).lower()
            if bool(row.get(col)) is not (b_str == "true"):
                return False
            continue
        m_not_bool = re.match(r"^NOT\s+([a-zA-Z_][a-zA-Z0-9_]*)$", c, re.IGNORECASE)
        if m_not_bool:
            if bool(row.get(m_not_bool.group(1))) is not False:
                return False
            continue
        m_bare_bool = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)$", c, re.IGNORECASE)
        if m_bare_bool:
            if bool(row.get(m_bare_bool.group(1))) is not True:
                return False
            continue
    return True


def _target_drives_prefix(
    target_col: str,
    prefix_col: str,
    check_hints: TableCheckHints,
    fk_cols: set[str],
    pk_cols: set[str],
) -> bool:
    """Decide which side of `split_part(target, sep, 1) = prefix` is derived from the other."""
    if prefix_col in fk_cols:
        return False
    if target_col in fk_cols:
        return True  # An FK value must stay present in its parent, so the prefix has to follow it.
    return (target_col in check_hints.regex_patterns or target_col in pk_cols) and prefix_col not in pk_cols


def _apply_split_part_prefix(
    rows: list[dict[str, Any]],
    check_hints: TableCheckHints,
    *,
    fk_cols: set[str],
    pk_cols: set[str],
) -> None:
    """Make rows satisfy each split_part CHECK, without ever rewriting a foreign key value."""
    for target_col, sep, prefix_col in check_hints.split_part_prefix:
        target_drives = _target_drives_prefix(target_col, prefix_col, check_hints, fk_cols, pk_cols)
        if not target_drives and target_col in fk_cols:
            continue  # Both sides are FKs: leave the CHECK to repair or degrade, and keep the FKs valid.
        for i, row in enumerate(rows):
            if row.get(target_col) is None or row.get(prefix_col) is None:
                continue
            if target_drives:
                row[prefix_col] = str(row[target_col]).split(sep, 1)[0]
            else:
                parts = str(row[target_col]).split(sep, 1)
                rest = parts[1] if len(parts) > 1 else f"item_{i + 1}"
                row[target_col] = f"{row[prefix_col]}{sep}{rest}"


def _enforce_uniqueness_on_table(
    table_fqn: str,
    table_meta: dict[str, Any],
    implicit_unique_col_sets: list[list[str]],
    rows: list[dict[str, Any]],
    populated_by_fk: set[str],
    table_fks: list[dict[str, Any]],
    rows_by_table: dict[str, list[dict[str, Any]]],
    check_hints: TableCheckHints,
    rng: np.random.Generator,
    faker_inst: Faker,
) -> None:
    """Ensure PK, UNIQUE constraints, partial unique indexes, and implicit FK targets have zero duplicates."""
    n_rows = len(rows)
    if n_rows <= 1:
        return

    unique_specs: list[tuple[str, list[str], str | None]] = []
    pk_cols = list(table_meta.get("pk") or [])
    if pk_cols:
        unique_specs.append(("pk", pk_cols, None))
    for uq in table_meta.get("unique") or []:
        u_cols = list(uq.get("columns") or [])
        if u_cols:
            unique_specs.append((str(uq.get("name", "uq")), u_cols, uq.get("predicate")))
    for imp_cols in implicit_unique_col_sets:
        if imp_cols:
            unique_specs.append(("implicit_fk_target", imp_cols, None))

    columns_meta = table_meta.get("columns") or {}
    col_names_set = set(columns_meta.keys())
    fk_groups = _group_overlapping_fks(table_fks)

    neq_pairs: list[tuple[str, str]] = [
        (ca, cb) for ca, op, cb in check_hints.pair_comparisons if op in ("<>", "!=")
    ]

    def _violates_neq(r: dict[str, Any]) -> bool:
        for ca, cb in neq_pairs:
            if r.get(ca) is not None and r.get(cb) is not None and r.get(ca) == r.get(cb):
                return True
        return False

    for uq_name, uq_cols, uq_pred in unique_specs:
        seen: set[tuple[Any, ...]] = set()
        # A prefix derived from an FK is reset after this pass, so redrawing it could not remove a duplicate.
        fk_derived = {
            prefix for target, _, prefix in check_hints.split_part_prefix if target in populated_by_fk
        }
        mutable_cols = [c for c in uq_cols if c not in populated_by_fk and c not in fk_derived]
        fk_uq_cols =[c for c in uq_cols if c in populated_by_fk]
        pred_cols = (
            [c for c in extract_referenced_columns(uq_pred, col_names_set) if c not in populated_by_fk]
            if uq_pred
            else []
        )

        for i, row in enumerate(rows):
            if not _row_matches_partial_predicate(row, uq_pred):
                continue
            tup = tuple(
                row[c].lower() if isinstance(row[c], str) else row[c]
                for c in uq_cols
            )
            if any(v is None for v in tup):
                continue
            if tup not in seen and not _violates_neq(row):
                seen.add(tup)
                continue

            resolved = False

            # Case 0: Partial unique index collision -> try swapping predicate columns with a non-matching row
            if uq_pred and pred_cols:
                for j in range(n_rows):
                    if j == i:
                        continue
                    if not _row_matches_partial_predicate(rows[j], uq_pred):
                        j_tup = tuple(
                            rows[j][c].lower() if isinstance(rows[j][c], str) else rows[j][c]
                            for c in uq_cols
                        )
                        if not any(v is None for v in j_tup) and j_tup not in seen:
                            for pc in pred_cols:
                                row[pc], rows[j][pc] = rows[j][pc], row[pc]
                            seen.add(j_tup)
                            resolved = True
                            break
                if resolved:
                    continue

            # Case A: We have non-FK mutable columns in the unique constraint
            if mutable_cols:
                for attempt in range(30):
                    for mc in mutable_cols:
                        row[mc] = _draw_single_non_null_value(
                            mc,
                            columns_meta[mc],
                            check_hints,
                            rng,
                            faker_inst,
                            i + (attempt + 1) * n_rows,
                            force_unique=(attempt >= 5),
                        )
                    for target_col, sep, prefix_col in check_hints.split_part_prefix:
                        if target_col in mutable_cols and row.get(target_col) is not None and row.get(prefix_col) is not None:
                            if target_col in check_hints.regex_patterns:
                                row[prefix_col] = str(row[target_col]).split(sep, 1)[0]
                            else:
                                parts = str(row[target_col]).split(sep, 1)
                                rest = parts[1] if len(parts) > 1 else f"item_{i + 1}_{attempt}"
                                row[target_col] = f"{row[prefix_col]}{sep}{rest}"
                    cand_tup = tuple(
                        row[c].lower() if isinstance(row[c], str) else row[c]
                        for c in uq_cols
                    )
                    if cand_tup not in seen and not _violates_neq(row):
                        seen.add(cand_tup)
                        resolved = True
                        break

                if not resolved:
                    for mc in mutable_cols:
                        c_type = str(columns_meta[mc].get("type", "")).lower()
                        if isinstance(row[mc], str):
                            suffix = f"_{i + 1}"
                            max_l = columns_meta[mc].get("len", {}).get("max", 64)
                            if mc in check_hints.char_len_bounds:
                                _, max_l = check_hints.char_len_bounds[mc]
                            vc_l = _varchar_limit(c_type)
                            if vc_l:
                                max_l = min(max_l, vc_l)
                            base = row[mc][: max(1, max_l - len(suffix))]
                            row[mc] = f"{base}{suffix}"
                            break
                        elif isinstance(row[mc], int):
                            row[mc] = int(row[mc]) + i + 100_000
                            break

                    cand_tup = tuple(
                        row[c].lower() if isinstance(row[c], str) else row[c]
                        for c in uq_cols
                    )
                    if cand_tup not in seen and not _violates_neq(row):
                        seen.add(cand_tup)
                        resolved = True

            # Case B: Junction / bridge table where unique columns are FK columns
            if not resolved and fk_uq_cols:
                relevant_groups = [
                    comp
                    for comp in fk_groups
                    if any(set(fk["from"]["columns"]) & set(fk_uq_cols) for fk in comp)
                ]
                for attempt in range(120):
                    single_row_list = [dict(row)]
                    for comp in relevant_groups:
                        _assign_fk_component(comp, 1, single_row_list, rows_by_table, rng)
                    cand_row = single_row_list[0]
                    if not _row_matches_partial_predicate(cand_row, uq_pred):
                        row.update(cand_row)
                        resolved = True
                        break
                    cand_tup = tuple(
                        cand_row[c].lower() if isinstance(cand_row[c], str) else cand_row[c]
                        for c in uq_cols
                    )
                    if not any(v is None for v in cand_tup) and cand_tup not in seen and not _violates_neq(cand_row):
                        row.update(cand_row)
                        seen.add(cand_tup)
                        resolved = True
                        break

            # Case C: If still colliding on a partial unique index, adjust a predicate column
            # so the duplicate row exits the partial index subset
            if not resolved and uq_pred and pred_cols:
                for pc in pred_cols:
                    if isinstance(row.get(pc), bool):
                        row[pc] = not row[pc]
                        if not _row_matches_partial_predicate(row, uq_pred):
                            resolved = True
                            break
                    elif pc in check_hints.allowed_values:
                        orig_val = row.get(pc)
                        for alt_val in check_hints.allowed_values[pc]:
                            row[pc] = alt_val
                            if not _row_matches_partial_predicate(row, uq_pred):
                                resolved = True
                                break
                        if resolved:
                            break
                        row[pc] = orig_val

            if not resolved and (mutable_cols or fk_uq_cols):
                raise RuntimeError(
                    f"Unable to satisfy UNIQUE constraint {uq_name!r} on {table_fqn}({', '.join(uq_cols)})."
                )


def _evaluate_and_repair_checks_in_postgres(
    conn: psycopg.Connection[Any],
    table_fqn: str,
    table_meta: dict[str, Any],
    rows: list[dict[str, Any]],
    locked_cols: set[str],
    check_hints: TableCheckHints,
    rng: np.random.Generator,
    faker_inst: Faker,
    *,
    check_max_attempts: int = 20,
    check_min_repair_rate: float = 0.20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate table CHECK constraints in a PostgreSQL temp table and repair failing rows (Appendix A, A13).

    Returns (degraded_checks, repaired_checks).
    """
    checks = list(table_meta.get("checks") or [])
    if not checks or not rows:
        return [], []

    columns_meta = table_meta.get("columns") or {}
    col_names = list(columns_meta.keys())
    col_names_set = set(col_names)

    degraded: list[dict[str, Any]] = []
    repaired: list[dict[str, Any]] = []

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pg_temp._lab_chk_eval;")
        col_defs = [sql.SQL("_row_idx integer PRIMARY KEY")]
        enum_types_to_strip: list[str] = []
        for c in col_names:
            c_meta = columns_meta[c]
            c_type = str(c_meta["type"])
            if c_meta.get("enum_type"):
                enum_types_to_strip.append(str(c_meta["enum_type"]))
                enum_types_to_strip.append(str(c_meta["enum_type"]).split(".", 1)[-1])
                c_type = "text"
            col_defs.append(sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(c_type)))

        cur.execute(
            sql.SQL("CREATE TEMP TABLE _lab_chk_eval ({});").format(
                sql.SQL(", ").join(col_defs)
            )
        )

        # Insert all candidate rows
        placeholders = sql.SQL(", ").join([sql.Placeholder()] * (len(col_names) + 1))
        col_idents = sql.SQL(", ").join(
            [sql.Identifier("_row_idx"), *[sql.Identifier(c) for c in col_names]]
        )
        insert_sql = sql.SQL("INSERT INTO pg_temp._lab_chk_eval ({}) VALUES ({})").format(
            col_idents, placeholders
        )
        batch_params = [
            (idx, *[row[c] for c in col_names]) for idx, row in enumerate(rows)
        ]
        cur.executemany(insert_sql, batch_params)

        active_checks = list(checks)

        def _unwrap_check_expr(raw_def: str) -> str:
            s = raw_def.strip()
            if s.upper().startswith("CHECK"):
                s = s[5:].strip()
            for et in enum_types_to_strip:
                s = re.sub(rf"::{re.escape(et)}(\[\])?", r"::text\1", s)
            return s

        def _find_failing_rows_for_checks(chk_defs: list[str]) -> list[int]:
            clauses = [f"(NOT ({_unwrap_check_expr(d)})) IS TRUE" for d in chk_defs]
            where_sql = " OR ".join(clauses)
            cur.execute(
                f"SELECT _row_idx FROM pg_temp._lab_chk_eval WHERE {where_sql} ORDER BY _row_idx;"
            )
            return [int(r[0]) for r in cur.fetchall()]

        def _update_rows_in_temp(row_indices: list[int]) -> None:
            if not row_indices:
                return
            set_clauses = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(c)) for c in col_names
            )
            upd_sql = sql.SQL("UPDATE pg_temp._lab_chk_eval SET {} WHERE _row_idx = %s").format(
                set_clauses
            )
            params = [(*[rows[idx][c] for c in col_names], idx) for idx in row_indices]
            cur.executemany(upd_sql, params)

        i_chk = 0
        while i_chk < len(active_checks):
            chk = active_checks[i_chk]
            cname = str(chk["name"])
            cexpr = str(chk["expr"])
            prefix_exprs = [str(c["expr"]) for c in active_checks[: i_chk + 1]]

            try:
                failing = _find_failing_rows_for_checks(prefix_exprs)
            except Exception as exc:
                conn.rollback()
                degraded.append(
                    {
                        "table": table_fqn,
                        "constraint": cname,
                        "rows_failed": len(rows),
                        "rows_repaired": 0,
                        "repair_rate": 0.0,
                        "reason": f"evaluation error ({type(exc).__name__}); dropped on target",
                    }
                )
                active_checks.pop(i_chk)
                continue

            initial_failed = len(failing)
            if initial_failed == 0:
                i_chk += 1
                continue

            ref_cols_set: set[str] = set()
            for pe in prefix_exprs:
                for c in extract_referenced_columns(pe, col_names_set):
                    if c not in locked_cols:
                        ref_cols_set.add(c)
            ref_cols = [c for c in col_names if c in ref_cols_set]
            if not ref_cols:
                ref_cols = [c for c in col_names if c not in locked_cols]

            involves_null = any("NULL" in pe.upper() for pe in prefix_exprs)

            # Backup initial values of all rows in case the CHECK gets dropped
            backup_rows = {idx: dict(rows[idx]) for idx in range(len(rows))}
            still_failing = list(failing)

            def _run_repair_attempts(max_iters: int, cols_to_mutate: list[str], offset_base: int) -> None:
                nonlocal still_failing
                for attempt in range(max_iters):
                    if not still_failing:
                        break
                    failing_set = set(still_failing)
                    prev_snapshot = {idx: dict(rows[idx]) for idx in range(len(rows))}
                    dirty_indices: set[int] = set(still_failing)
                    swapped_partners: dict[int, int] = {}

                    for r_idx in still_failing:
                        for rc in cols_to_mutate:
                            c_meta = columns_meta[rc]
                            is_cat = bool("categorical" in c_meta and c_meta["categorical"])
                            is_nullable_col = bool(c_meta.get("nullable", False)) and float(c_meta.get("null_rate", 0.0)) > 0.0

                            # If column is under an exact allowlist categorical quota, or is a nullable column
                            # in a NULL-correlation CHECK when rows[r_idx][rc] is None (or on alternating attempts),
                            # swap with another row so exact quotas and NULL counts stay invariant!
                            if is_cat or (involves_null and is_nullable_col and (rows[r_idx][rc] is None or attempt % 2 == 1)):
                                if len(still_failing) > 1 and attempt % 2 == 0:
                                    other_failing = [idx for idx in still_failing if idx != r_idx]
                                    swap_idx = other_failing[int(rng.integers(0, len(other_failing)))]
                                else:
                                    swap_idx = int(rng.integers(0, len(rows)))
                                if swap_idx != r_idx and rows[swap_idx][rc] != rows[r_idx][rc]:
                                    rows[r_idx][rc], rows[swap_idx][rc] = (
                                        rows[swap_idx][rc],
                                        rows[r_idx][rc],
                                    )
                                    dirty_indices.add(swap_idx)
                                    swapped_partners[swap_idx] = r_idx
                                continue

                            if rows[r_idx][rc] is None:
                                continue
                            rows[r_idx][rc] = _draw_single_non_null_value(
                                rc,
                                c_meta,
                                check_hints,
                                rng,
                                faker_inst,
                                r_idx + (attempt + offset_base) * len(rows),
                            )

                    _update_rows_in_temp(sorted(dirty_indices))
                    new_failing = _find_failing_rows_for_checks(prefix_exprs)
                    new_failing_set = set(new_failing)

                    # Revert any swap that broke a previously passing row, or any failing row that did not get fixed
                    reverted_indices: set[int] = set()
                    for s_idx, partner_idx in swapped_partners.items():
                        if s_idx not in failing_set and s_idx in new_failing_set:
                            rows[s_idx].update(prev_snapshot[s_idx])
                            rows[partner_idx].update(prev_snapshot[partner_idx])
                            reverted_indices.add(s_idx)
                            reverted_indices.add(partner_idx)

                    if reverted_indices:
                        _update_rows_in_temp(sorted(reverted_indices))
                        new_failing = _find_failing_rows_for_checks(prefix_exprs)

                    still_failing = new_failing

            _run_repair_attempts(check_max_attempts, ref_cols, 1)

            rows_repaired = initial_failed - len(still_failing)
            repair_rate = rows_repaired / max(initial_failed, 1)

            if repair_rate < check_min_repair_rate:
                # Restore backed-up rows so quota/distribution is untouched, and drop CHECK
                for r_idx, orig_dict in backup_rows.items():
                    rows[r_idx].update(orig_dict)
                _update_rows_in_temp(list(backup_rows.keys()))
                degraded.append(
                    {
                        "table": table_fqn,
                        "constraint": cname,
                        "rows_failed": initial_failed,
                        "rows_repaired": rows_repaired,
                        "repair_rate": round(repair_rate, 4),
                        "reason": "repair rate below threshold; dropped on target",
                    }
                )
                active_checks.pop(i_chk)
                continue

            if still_failing:
                # Extended pass: 10x attempts redrawing non-key columns (Appendix A step 3)
                non_key_cols = [c for c in col_names if c not in locked_cols]
                _run_repair_attempts(check_max_attempts * 10, non_key_cols, 50)

            if still_failing:
                final_repaired = initial_failed - len(still_failing)
                for r_idx, orig_dict in backup_rows.items():
                    rows[r_idx].update(orig_dict)
                _update_rows_in_temp(list(backup_rows.keys()))
                degraded.append(
                    {
                        "table": table_fqn,
                        "constraint": cname,
                        "rows_failed": initial_failed,
                        "rows_repaired": final_repaired,
                        "repair_rate": round(final_repaired / max(initial_failed, 1), 4),
                        "reason": "rows still failing after extended repair pass; dropped on target",
                    }
                )
                active_checks.pop(i_chk)
                continue

            # Fully repaired! Verify we didn't break any earlier active CHECKs on the table
            repaired.append(
                {
                    "table": table_fqn,
                    "constraint": cname,
                    "rows_failed": initial_failed,
                    "rows_repaired": initial_failed,
                    "repair_rate": 1.0,
                }
            )
            i_chk += 1

        cur.execute("DROP TABLE IF EXISTS pg_temp._lab_chk_eval;")

    return degraded, repaired


def generate_dataset(
    profile: dict[str, Any],
    *,
    scale: float = 0.1,
    seed: int = 42,
    max_rows: int = 50_000,
    locale: str = "en_US",
    check_max_attempts: int = 20,
    check_min_repair_rate: float = 0.20,
    scratch_dsn: ParsedDSN | None = None,
) -> GenerationResult:
    """Generate deterministic synthetic rows strictly from profile.json (never connects to source)."""
    plan: GenerationPlan = build_generation_plan(profile, scale=scale, max_rows=max_rows)
    tables_meta = profile.get("tables") or {}

    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    column_order_by_table: dict[str, list[str]] = {}
    all_degraded: list[dict[str, Any]] = []
    all_repaired: list[dict[str, Any]] = []

    scratch_conn: psycopg.Connection[Any] | None = None
    if scratch_dsn is not None:
        try:
            scratch_conn = psycopg.connect(scratch_dsn.raw_dsn, autocommit=True)
        except Exception:
            try:
                maint_params = [
                    f"host={scratch_dsn.host}",
                    f"port={scratch_dsn.port}",
                    f"user={scratch_dsn.user}",
                    "dbname=postgres",
                ]
                if scratch_dsn.password is not None:
                    maint_params.append(f"password={scratch_dsn.password}")
                scratch_conn = psycopg.connect(" ".join(maint_params), autocommit=True)
            except Exception:
                scratch_conn = None

    try:
        for table_fqn in plan.table_order:
            t_meta = tables_meta[table_fqn]
            n_rows = plan.target_row_counts[table_fqn]
            columns_meta = t_meta.get("columns") or {}
            col_names = list(columns_meta.keys())
            column_order_by_table[table_fqn] = col_names

            if n_rows <= 0:
                rows_by_table[table_fqn] = []
                continue

            table_rng = _make_rng(seed, table_fqn)
            table_faker = _make_faker(seed, table_fqn, locale=locale)
            check_hints = parse_table_checks(t_meta.get("checks") or [], set(col_names))

            rows: list[dict[str, Any]] = [{} for _ in range(n_rows)]
            populated_cols: set[str] = set()

            # 1. Deferred cycle FKs are initially NULL and populated after all tables exist
            for dfk in plan.deferred_cycle_fks:
                if dfk["from"]["table"] == table_fqn:
                    for c in dfk["from"]["columns"]:
                        for r in rows:
                            r[c] = None
                        populated_cols.add(c)

            # 2. Populate inter-table FKs (grouped by shared columns)
            fk_groups = _group_overlapping_fks(plan.table_fks.get(table_fqn, []))
            for comp in fk_groups:
                _assign_fk_component(comp, n_rows, rows, rows_by_table, table_rng)
                for fk in comp:
                    populated_cols.update(fk["from"]["columns"])

            # 3. Identify PK, unique, and implicit FK target columns
            pk_cols = set(t_meta.get("pk") or [])
            imp_unique_sets = plan.implicit_target_unique_cols.get(table_fqn, [])
            single_unique_cols: set[str] = set()
            if len(pk_cols) == 1:
                single_unique_cols.update(pk_cols)
            for uq in t_meta.get("unique") or []:
                u_cols = uq.get("columns") or []
                if len(u_cols) == 1:
                    single_unique_cols.add(u_cols[0])
                else:
                    non_fk_u = [c for c in u_cols if c not in populated_cols]
                    if len(non_fk_u) == 1:
                        single_unique_cols.add(non_fk_u[0])
            for imp_cols in imp_unique_sets:
                if len(imp_cols) == 1:
                    single_unique_cols.add(imp_cols[0])

            # 4. Generate remaining non-self-FK columns
            self_fk_cols: set[str] = set()
            for sfk in plan.self_fks.get(table_fqn, []):
                self_fk_cols.update(sfk["from"]["columns"])

            for col_name in col_names:
                if col_name in populated_cols or col_name in self_fk_cols:
                    continue

                col_rng = _make_rng(seed, f"{table_fqn}.{col_name}")
                col_faker = _make_faker(seed, f"{table_fqn}.{col_name}", locale=locale)
                c_meta = columns_meta[col_name]
                pg_type = str(c_meta.get("type", "text"))
                nullable = bool(c_meta.get("nullable", False)) and col_name not in pk_cols
                null_rate = float(c_meta.get("null_rate", 0.0)) if nullable else 0.0
                force_uq = col_name in single_unique_cols

                # Categorical or allowed_values (exact stratified quota)
                col_allowed = c_meta.get("allowed_values") or check_hints.in_values.get(col_name)
                if ("categorical" in c_meta and c_meta["categorical"]) or col_allowed:
                    if "categorical" in c_meta and c_meta["categorical"]:
                        weights = {str(k): float(v) for k, v in c_meta["categorical"].items()}
                    else:
                        allowed = [str(v) for v in col_allowed]
                        weights = {k: 1.0 / len(allowed) for k in allowed}

                    raw_vals = allocate_categorical_column(
                        n_rows, null_rate, nullable, weights, col_rng
                    )
                    for i, rv in enumerate(raw_vals):
                        rows[i][col_name] = _coerce_categorical_value(rv, pg_type)
                    populated_cols.add(col_name)
                    continue

                # Non-categorical column: allocate stratified null mask, then draw values
                null_mask = allocate_null_mask(n_rows, null_rate, nullable, col_rng)
                if c_meta.get("is_identity") or (
                    col_name in pk_cols
                    and any(
                        pg_type.lower().startswith(p)
                        for p in ("smallint", "integer", "bigint", "int")
                    )
                ):
                    # Shuffled sequential integers 1..n_rows (Appendix A)
                    seq = list(range(1, n_rows + 1))
                    perm = col_rng.permutation(n_rows)
                    for i, p_idx in enumerate(perm):
                        rows[i][col_name] = None if null_mask[i] else seq[int(p_idx)]
                else:
                    for i in range(n_rows):
                        if null_mask[i]:
                            rows[i][col_name] = None
                        else:
                            rows[i][col_name] = _draw_single_non_null_value(
                                col_name,
                                c_meta,
                                check_hints,
                                col_rng,
                                col_faker,
                                i,
                                force_unique=force_uq,
                            )
                populated_cols.add(col_name)

            # 5. Populate self-referencing FKs after PKs exist, respecting source coverage (A5)
            for sfk in plan.self_fks.get(table_fqn, []):
                s_from = sfk["from"]["columns"]
                s_to = sfk["to"]["columns"]
                s_null_rate = float(sfk.get("null_rate", 0.2))
                s_nullable = bool(sfk.get("nullable", True))
                self_only_cols = [c for c in s_from if c not in populated_cols]
                shared_pairs = [(c_f, c_t) for c_f, c_t in zip(s_from, s_to) if c_f in populated_cols]

                # At least row 0 must have NULL self-FK to anchor the hierarchy
                s_mask = allocate_null_mask(n_rows, s_null_rate, s_nullable, table_rng)
                if s_nullable and not any(s_mask):
                    s_mask[0] = True

                root_indices = [idx for idx, is_null in enumerate(s_mask) if is_null]
                child_indices = [idx for idx, is_null in enumerate(s_mask) if not is_null]
                for r_idx in root_indices:
                    for c in (self_only_cols or s_from):
                        rows[r_idx][c] = None

                if child_indices:
                    src_cov = float(sfk.get("coverage", 0.25))
                    target_active = min(
                        n_rows,
                        min(len(child_indices), max(1, int(round(src_cov * n_rows)))),
                    )
                    # Prefer picking active parents from root_indices first so hierarchy is acyclic
                    candidate_parents = list(root_indices) + [
                        idx for idx in child_indices if idx not in root_indices
                    ]
                    active_parents = candidate_parents[:target_active]
                    assigned_parents: list[int] = list(active_parents)
                    rem_c = len(child_indices) - len(assigned_parents)
                    if rem_c > 0:
                        extra = table_rng.integers(0, len(active_parents), size=rem_c)
                        for e_i in extra:
                            assigned_parents.append(active_parents[int(e_i)])
                    perm_p = table_rng.permutation(len(assigned_parents))
                    for c_idx, p_pos in zip(child_indices, perm_p):
                        p_idx = assigned_parents[int(p_pos)]
                        if shared_pairs:
                            if p_idx == c_idx or any(
                                rows[p_idx].get(c_t) != rows[c_idx].get(c_f) for c_f, c_t in shared_pairs
                            ):
                                matching = [
                                    cand
                                    for cand in candidate_parents
                                    if cand != c_idx
                                    and all(
                                        rows[cand].get(c_t) == rows[c_idx].get(c_f)
                                        for c_f, c_t in shared_pairs
                                    )
                                ]
                                if matching:
                                    p_idx = matching[int(table_rng.integers(0, len(matching)))]
                                else:
                                    for c in (self_only_cols or s_from):
                                        rows[c_idx][c] = None
                                    continue
                        elif p_idx == c_idx and len(active_parents) > 1:
                            p_idx = active_parents[0] if active_parents[0] != c_idx else active_parents[1]
                        for c_f, c_t in zip(s_from, s_to):
                            rows[c_idx][c_f] = rows[p_idx][c_t]
                populated_cols.update(s_from)

            fk_cols_on_table = {
                c for fk in plan.table_fks.get(table_fqn, []) for c in fk["from"]["columns"]
            }

            # 6. Apply constructive multi-column hints (split_part prefix, simple pairwise comparisons)
            _apply_split_part_prefix(rows, check_hints, fk_cols=fk_cols_on_table, pk_cols=pk_cols)

            for col_a, op, col_b in check_hints.pair_comparisons:
                if col_a in populated_cols and col_b in populated_cols:
                    for i, r in enumerate(rows):
                        va, vb = r.get(col_a), r.get(col_b)
                        if va is None or vb is None:
                            continue
                        if op in (">=", ">") and va < vb:
                            r[col_a], r[col_b] = vb, va
                        elif op in ("<=", "<") and va > vb:
                            r[col_a], r[col_b] = vb, va
                        elif op in ("<>", "!=") and va == vb:
                            # Swap with another row that has a different value and matching composite FK siblings
                            sibling_fk_cols = {
                                c
                                for fk in plan.table_fks.get(table_fqn, [])
                                if col_a in fk["from"]["columns"]
                                for c in fk["from"]["columns"]
                                if c != col_a
                            }
                            for j in range(n_rows):
                                if (
                                    rows[j].get(col_a) is not None
                                    and rows[j][col_a] != vb
                                    and all(rows[j].get(sc) == r.get(sc) for sc in sibling_fk_cols)
                                ):
                                    r[col_a], rows[j][col_a] = rows[j][col_a], r[col_a]
                                    break

            # 7. Enforce uniqueness on PK, UNIQUE, partial unique indexes, and implicit FK targets
            _enforce_uniqueness_on_table(
                table_fqn,
                t_meta,
                imp_unique_sets,
                rows,
                fk_cols_on_table,
                plan.table_fks.get(table_fqn, []),
                rows_by_table,
                check_hints,
                table_rng,
                table_faker,
            )

            # Re-sync target-driven split_part_prefix after uniqueness redraws
            for target_col, sep, prefix_col in check_hints.split_part_prefix:
                if _target_drives_prefix(target_col, prefix_col, check_hints, fk_cols_on_table, pk_cols):
                    for r in rows:
                        if r.get(target_col) is not None:
                            r[prefix_col] = str(r[target_col]).split(sep, 1)[0]

            # 8. Evaluate and repair CHECK constraints in scratch temp table (Appendix A, A13)
            locked_key_cols = set(pk_cols)
            for fk in plan.table_fks.get(table_fqn, []):
                locked_key_cols.update(fk["from"]["columns"])
            for imp_cols in imp_unique_sets:
                locked_key_cols.update(imp_cols)

            if scratch_conn is not None and t_meta.get("checks"):
                deg, rep = _evaluate_and_repair_checks_in_postgres(
                    scratch_conn,
                    table_fqn,
                    t_meta,
                    rows,
                    locked_key_cols,
                    check_hints,
                    table_rng,
                    table_faker,
                    check_max_attempts=check_max_attempts,
                    check_min_repair_rate=check_min_repair_rate,
                )
                all_degraded.extend(deg)
                all_repaired.extend(rep)

            rows_by_table[table_fqn] = rows

        # 9. Resolve deferred cycle FKs in memory before writing anything (Appendix A)
        for dfk in plan.deferred_cycle_fks:
            child_tbl = dfk["from"]["table"]
            child_rows = rows_by_table.get(child_tbl, [])
            if not child_rows:
                continue
            cyc_rng = _make_rng(seed, f"cycle:{child_tbl}:{dfk.get('name', '')}")
            _assign_fk_component([dfk], len(child_rows), child_rows, rows_by_table, cyc_rng)

    finally:
        if scratch_conn is not None:
            scratch_conn.close()

    return GenerationResult(
        rows_by_table=rows_by_table,
        column_order_by_table=column_order_by_table,
        degraded_checks=all_degraded,
        repaired_checks=all_repaired,
    )
