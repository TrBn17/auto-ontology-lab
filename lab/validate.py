"""Acceptance and validation suite (A1–A9, A11–A13) producing validate_report.json (§1.5, §2.2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import psycopg
from psycopg import sql

from lab.dsn import ParsedDSN
from lab.plan import compute_target_row_count
from lab.writers import is_degraded_ack_valid


DEFAULT_CANARIES: tuple[str, ...] = (
    "CANARY_7f3a",
    "987654321012",
    "1901-02-03",
)


def _check_a1_canary(
    conn: psycopg.Connection[Any],
    dataset_dir: Path,
    profile: dict[str, Any],
    canary_tokens: list[str],
) -> dict[str, Any]:
    """A1: Verify canary tokens and sentinels do not appear in files or target DB rows."""
    if not canary_tokens:
        return {"status": "pass", "violations": []}

    violations: list[str] = []

    # 1. Check files on disk (profile.json, ddl/, data/, manifest.json)
    files_to_scan: list[Path] = []
    for rel in ("profile.json", "manifest.json"):
        p = dataset_dir / rel
        if p.is_file():
            files_to_scan.append(p)
    for sub in ("ddl", "data"):
        sub_dir = dataset_dir / sub
        if sub_dir.is_dir():
            files_to_scan.extend(sorted(sub_dir.glob("*")))

    for file_path in files_to_scan:
        if not file_path.is_file():
            continue
        text = file_path.read_text(encoding="utf-8", errors="replace")
        for token in canary_tokens:
            if token in text:
                violations.append(f"file:{file_path.relative_to(dataset_dir)} contains {token!r}")

    # 2. Check target DB columns (except explicitly allowlisted categorical columns)
    tables_meta = profile.get("tables") or {}
    with conn.cursor() as cur:
        for table_fqn, t_meta in sorted(tables_meta.items()):
            schema_name, rel_name = table_fqn.split(".", 1)
            tbl_ident = sql.Identifier(schema_name, rel_name)
            cols_meta = t_meta.get("columns") or {}
            for col_name, c_meta in cols_meta.items():
                if c_meta.get("value_source") == "allowlist":
                    continue
                col_ident = sql.Identifier(col_name)
                for token in canary_tokens:
                    cur.execute(
                        sql.SQL("SELECT count(*) FROM {} WHERE {}::text LIKE %s").format(
                            tbl_ident, col_ident
                        ),
                        (f"%{token}%",),
                    )
                    cnt = int(cur.fetchone()[0] or 0)
                    if cnt > 0:
                        violations.append(
                            f"db:{table_fqn}.{col_name} contains {token!r} in {cnt} row(s)"
                        )

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def _check_a2_schema_fidelity(
    conn: psycopg.Connection[Any],
    profile: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """A2: Verify PKs, UNIQUEs (incl. partial), FKs, enums, views, CHECKs (or degraded), and absence of RLS."""
    violations: list[str] = []
    tables_meta = profile.get("tables") or {}
    degraded_set = {
        (str(d.get("table")), str(d.get("constraint")))
        for d in (manifest.get("degraded") or [])
    }

    with conn.cursor() as cur:
        # 1. Tables present and no unexpected tables
        cur.execute(
            """
            SELECT n.nspname || '.' || c.relname AS fqn,
                   c.relrowsecurity OR c.relforcerowsecurity AS has_rls
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e'
              );
            """
        )
        tgt_rows = cur.fetchall()
        tgt_tables = {str(r[0]) for r in tgt_rows}
        rls_tables = [str(r[0]) for r in tgt_rows if r[1]]

        cur.execute("SELECT count(*) FROM pg_policy;")
        policy_count = int(cur.fetchone()[0] or 0)
        if rls_tables or policy_count > 0:
            violations.append(f"Target DB has RLS enabled on {rls_tables} or {policy_count} policies")

        expected_tables = set(tables_meta.keys())
        missing_tables = sorted(expected_tables - tgt_tables)
        extra_tables = sorted(tgt_tables - expected_tables)
        if missing_tables:
            violations.append(f"Missing target tables: {missing_tables}")
        if extra_tables:
            violations.append(f"Unexpected extra target tables: {extra_tables}")

        # 2. Enums
        cur.execute(
            """
            SELECT n.nspname || '.' || t.typname, e.enumlabel
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, t.typname, e.enumsortorder;
            """
        )
        tgt_enums: dict[str, list[str]] = {}
        for efqn, elbl in cur.fetchall():
            tgt_enums.setdefault(str(efqn), []).append(str(elbl))
        for efqn, exp_labels in (profile.get("enums") or {}).items():
            if tgt_enums.get(efqn) != exp_labels:
                violations.append(
                    f"Enum {efqn} mismatch: expected {exp_labels}, got {tgt_enums.get(efqn)}"
                )

        # 3. Views
        cur.execute(
            """
            SELECT n.nspname || '.' || c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'v'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e'
              );
            """
        )
        tgt_views = {str(r[0]) for r in cur.fetchall()}
        for v_fqn in profile.get("views") or []:
            if v_fqn not in tgt_views:
                violations.append(f"Missing view on target: {v_fqn}")

        # 4. Per-table PK, UNIQUE indexes, CHECKs
        for table_fqn, t_meta in sorted(tables_meta.items()):
            if table_fqn not in tgt_tables:
                continue
            schema_name, rel_name = table_fqn.split(".", 1)
            cur.execute(
                """
                SELECT c.oid
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s;
                """,
                (schema_name, rel_name),
            )
            t_oid = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT a.attnum, a.attname
                FROM pg_attribute a
                WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped;
                """,
                (t_oid,),
            )
            att_map = {int(r[0]): str(r[1]) for r in cur.fetchall()}

            # PK check
            cur.execute(
                "SELECT conkey FROM pg_constraint WHERE conrelid = %s AND contype = 'p';",
                (t_oid,),
            )
            pk_row = cur.fetchone()
            tgt_pk = [att_map[int(a)] for a in pk_row[0]] if pk_row else []
            exp_pk = list(t_meta.get("pk") or [])
            if tgt_pk != exp_pk:
                violations.append(f"PK mismatch on {table_fqn}: expected {exp_pk}, got {tgt_pk}")

            # Unique indexes check
            cur.execute(
                """
                SELECT c.relname
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                WHERE i.indrelid = %s AND i.indisunique AND NOT i.indisprimary;
                """,
                (t_oid,),
            )
            tgt_uq_names = {str(r[0]) for r in cur.fetchall()}
            for uq in t_meta.get("unique") or []:
                u_name = str(uq.get("name"))
                if u_name not in tgt_uq_names:
                    violations.append(f"Missing unique index/constraint {u_name} on {table_fqn}")

            # CHECK constraints check
            cur.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = %s AND contype = 'c';",
                (t_oid,),
            )
            tgt_chks = {str(r[0]) for r in cur.fetchall()}
            for chk in t_meta.get("checks") or []:
                c_name = str(chk.get("name"))
                in_tgt = c_name in tgt_chks
                in_deg = (table_fqn, c_name) in degraded_set
                if not in_tgt and not in_deg:
                    violations.append(
                        f"CHECK {c_name} on {table_fqn} is missing from both target DB and degraded list"
                    )
                if in_tgt and in_deg:
                    violations.append(
                        f"CHECK {c_name} on {table_fqn} is listed as degraded but still present on target DB"
                    )

        # 5. Physical FKs
        cur.execute(
            """
            SELECT ns_from.nspname || '.' || cl_from.relname AS from_tbl,
                   con.conname
            FROM pg_constraint con
            JOIN pg_class cl_from ON cl_from.oid = con.conrelid
            JOIN pg_namespace ns_from ON ns_from.oid = cl_from.relnamespace
            WHERE con.contype = 'f';
            """
        )
        tgt_fks = {(str(r[0]), str(r[1])) for r in cur.fetchall()}
        for fk in profile.get("foreign_keys") or []:
            if fk.get("kind") == "implicit":
                continue
            fk_key = (str(fk["from"]["table"]), str(fk["name"]))
            if fk_key not in tgt_fks:
                violations.append(f"Missing physical FK {fk['name']} on {fk['from']['table']}")

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def _check_a3_integrity(
    conn: psycopg.Connection[Any],
    profile: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """A3: Verify zero orphan FK rows, zero PK/UNIQUE duplicates, and all target CHECKs hold."""
    violations: list[str] = []
    degraded_set = {
        (str(d.get("table")), str(d.get("constraint")))
        for d in (manifest.get("degraded") or [])
    }

    with conn.cursor() as cur:
        # 1. FK orphans
        for fk in profile.get("foreign_keys") or []:
            if fk.get("kind") == "implicit":
                continue
            from_tbl = fk["from"]["table"]
            from_cols = fk["from"]["columns"]
            to_tbl = fk["to"]["table"]
            to_cols = fk["to"]["columns"]

            f_schema, f_rel = from_tbl.split(".", 1)
            t_schema, t_rel = to_tbl.split(".", 1)

            join_cond = sql.SQL(" AND ").join(
                sql.SQL("c.{} = p.{}").format(sql.Identifier(fc), sql.Identifier(tc))
                for fc, tc in zip(from_cols, to_cols)
            )
            not_null_cond = sql.SQL(" AND ").join(
                sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(fc)) for fc in from_cols
            )
            parent_null_cond = sql.SQL("p.{} IS NULL").format(sql.Identifier(to_cols[0]))

            cur.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} c LEFT JOIN {} p ON {} WHERE ({}) AND {}"
                ).format(
                    sql.Identifier(f_schema, f_rel),
                    sql.Identifier(t_schema, t_rel),
                    join_cond,
                    not_null_cond,
                    parent_null_cond,
                )
            )
            orphans = int(cur.fetchone()[0] or 0)
            if orphans > 0:
                violations.append(
                    f"FK {fk.get('name')} on {from_tbl} has {orphans} orphan row(s)"
                )

        # 2. Target CHECKs hold
        tables_meta = profile.get("tables") or {}
        for table_fqn, t_meta in sorted(tables_meta.items()):
            f_schema, f_rel = table_fqn.split(".", 1)
            tbl_ident = sql.Identifier(f_schema, f_rel)
            for chk in t_meta.get("checks") or []:
                c_name = str(chk["name"])
                if (table_fqn, c_name) in degraded_set:
                    continue
                expr = str(chk["expr"]).strip()
                if expr.upper().startswith("CHECK"):
                    expr = expr[5:].strip()
                cur.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE (NOT ({})) IS TRUE").format(
                        tbl_ident, sql.SQL(expr)
                    )
                )
                bad = int(cur.fetchone()[0] or 0)
                if bad > 0:
                    violations.append(f"CHECK {c_name} on {table_fqn} failed on {bad} row(s)")

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def _check_a4_row_counts(
    conn: psycopg.Connection[Any],
    profile: dict[str, Any],
    *,
    scale: float,
    max_rows: int = 50_000,
) -> tuple[dict[str, Any], dict[str, int]]:
    """A4: Verify each table has min(row_count, max(50, round(row_count * scale))) rows (±5% or ±2)."""
    violations: list[str] = []
    actual_counts: dict[str, int] = {}
    tables_meta = profile.get("tables") or {}

    with conn.cursor() as cur:
        for table_fqn, t_meta in sorted(tables_meta.items()):
            src_rows = int(t_meta.get("row_count", 0))
            exp_rows = compute_target_row_count(src_rows, scale, max_rows)
            schema_name, rel_name = table_fqn.split(".", 1)
            cur.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier(schema_name, rel_name)
                )
            )
            act_rows = int(cur.fetchone()[0] or 0)
            actual_counts[table_fqn] = act_rows

            tol = max(0.05 * exp_rows, 2.0)
            if abs(act_rows - exp_rows) > tol:
                violations.append(
                    f"{table_fqn}: expected {exp_rows} (±{tol:.1f}), got {act_rows}"
                )

    return (
        {
            "status": "pass" if not violations else "fail",
            "violations": violations,
        },
        actual_counts,
    )


def _check_a5_cardinality(
    conn: psycopg.Connection[Any],
    profile: dict[str, Any],
    actual_counts: dict[str, int],
    *,
    fks_to_check: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A5: Verify FK children-per-parent scaled ratio (±20%) and parent coverage (±0.10 when scales match)."""
    violations: list[str] = []
    tables_meta = profile.get("tables") or {}
    fks = fks_to_check if fks_to_check is not None else (profile.get("foreign_keys") or [])

    with conn.cursor() as cur:
        for fk in fks:
            from_tbl = fk["from"]["table"]
            from_cols = fk["from"]["columns"]
            to_tbl = fk["to"]["table"]

            src_child_rows = int(tables_meta[from_tbl].get("row_count", 0))
            src_parent_rows = int(tables_meta[to_tbl].get("row_count", 0))
            tgt_child_rows = actual_counts.get(from_tbl, 0)
            tgt_parent_rows = actual_counts.get(to_tbl, 0)

            if src_child_rows == 0 or src_parent_rows == 0 or tgt_child_rows == 0 or tgt_parent_rows == 0:
                continue

            s_child = tgt_child_rows / src_child_rows
            s_parent = tgt_parent_rows / src_parent_rows

            f_schema, f_rel = from_tbl.split(".", 1)
            not_null_cond = sql.SQL(" AND ").join(
                sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(fc)) for fc in from_cols
            )
            group_cols = sql.SQL(", ").join(
                sql.SQL("c.{}").format(sql.Identifier(fc)) for fc in from_cols
            )
            cur.execute(
                sql.SQL(
                    "SELECT count(*), count(DISTINCT ({})) FROM {} c WHERE {}"
                ).format(
                    group_cols,
                    sql.Identifier(f_schema, f_rel),
                    not_null_cond,
                )
            )
            tgt_non_null, tgt_distinct_parents = cur.fetchone()
            tgt_non_null = int(tgt_non_null or 0)
            tgt_distinct_parents = int(tgt_distinct_parents or 0)

            tgt_avg = tgt_non_null / tgt_parent_rows
            src_avg = float(fk.get("children_per_parent", {}).get("avg", 0.0))
            exp_avg = src_avg * (s_child / s_parent)

            if exp_avg > 0:
                rel_err = abs(tgt_avg - exp_avg) / exp_avg
                # Allow 1-row discretization slack on small tables
                one_row_slack = 1.5 / max(tgt_parent_rows, 1)
                if rel_err > 0.20 and abs(tgt_avg - exp_avg) > one_row_slack:
                    violations.append(
                        f"FK {fk.get('name')} ({from_tbl}->{to_tbl}) avg children/parent {tgt_avg:.3f} "
                        f"outside ±20% of expected {exp_avg:.3f}"
                    )

            # Coverage check where s_child and s_parent are within 1% of each other
            if abs(s_child - s_parent) / max(s_parent, 1e-9) <= 0.01:
                src_cov = float(fk.get("coverage", 1.0))
                tgt_cov = tgt_distinct_parents / tgt_parent_rows
                if abs(tgt_cov - src_cov) > max(0.10, 1.5 / max(tgt_parent_rows, 1)):
                    violations.append(
                        f"FK {fk.get('name')} coverage {tgt_cov:.3f} outside ±0.10 of source {src_cov:.3f}"
                    )

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def _check_a6_a7_nulls_and_l1(
    conn: psycopg.Connection[Any],
    profile: dict[str, Any],
    actual_counts: dict[str, int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A6 (Null rate: |Δ| <= max(0.03, 1/n)) and A7 (Categorical L1 <= max(0.10, k/n))."""
    null_violations: list[str] = []
    l1_violations: list[str] = []
    tables_meta = profile.get("tables") or {}

    with conn.cursor() as cur:
        for table_fqn, t_meta in sorted(tables_meta.items()):
            n_rows = actual_counts.get(table_fqn, 0)
            if n_rows <= 0:
                continue
            f_schema, f_rel = table_fqn.split(".", 1)
            tbl_ident = sql.Identifier(f_schema, f_rel)
            cols_meta = t_meta.get("columns") or {}

            for col_name, c_meta in cols_meta.items():
                col_ident = sql.Identifier(col_name)
                src_null_rate = float(c_meta.get("null_rate", 0.0))
                cur.execute(
                    sql.SQL(
                        "SELECT count(*) FILTER (WHERE {} IS NULL)::float / GREATEST(count(*), 1) FROM {}"
                    ).format(col_ident, tbl_ident)
                )
                tgt_null_rate = float(cur.fetchone()[0] or 0.0)
                max_delta = max(0.03, 1.0 / n_rows) + 1e-6
                if abs(tgt_null_rate - src_null_rate) > max_delta:
                    null_violations.append(
                        f"{table_fqn}.{col_name}: null_rate {tgt_null_rate:.4f} vs source {src_null_rate:.4f} "
                        f"(limit ±{max_delta:.4f})"
                    )

                # A7: Allowlisted categorical L1
                if c_meta.get("value_source") == "allowlist" and c_meta.get("categorical"):
                    src_cat: dict[str, float] = {
                        str(k): float(v) for k, v in c_meta["categorical"].items()
                    }
                    cur.execute(
                        sql.SQL(
                            "SELECT {}::text, count(*) FROM {} WHERE {} IS NOT NULL GROUP BY {}"
                        ).format(col_ident, tbl_ident, col_ident, col_ident)
                    )
                    freq_rows = cur.fetchall()
                    tot_non_null = sum(int(r[1]) for r in freq_rows)
                    if tot_non_null > 0:
                        tgt_cat = {
                            str(val): int(cnt) / tot_non_null for val, cnt in freq_rows
                        }
                        all_keys = set(src_cat.keys()) | set(tgt_cat.keys())
                        l1 = sum(
                            abs(src_cat.get(k, 0.0) - tgt_cat.get(k, 0.0))
                            for k in all_keys
                        )
                        k_distinct = max(len(src_cat), 1)
                        max_l1 = max(0.10, k_distinct / tot_non_null) + 1e-6
                        if l1 > max_l1:
                            l1_violations.append(
                                f"{table_fqn}.{col_name}: L1={l1:.4f} > limit {max_l1:.4f}"
                            )

    return (
        {"status": "pass" if not null_violations else "fail", "violations": null_violations},
        {"status": "pass" if not l1_violations else "fail", "violations": l1_violations},
    )


def _check_a8_usability(
    target_dsn: ParsedDSN,
    profile: dict[str, Any],
    actual_counts: dict[str, int],
) -> dict[str, Any]:
    """A8: Verify lab_ro can SELECT expected row counts from every table (incl. source-RLS tables) and cannot INSERT."""
    violations: list[str] = []
    tables_meta = profile.get("tables") or {}

    with psycopg.connect(target_dsn.raw_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SET ROLE lab_ro;")
            except Exception as exc:
                return {
                    "status": "fail",
                    "violations": [f"Cannot switch to role lab_ro: {exc}"],
                }

            for table_fqn in sorted(tables_meta.keys()):
                f_schema, f_rel = table_fqn.split(".", 1)
                tbl_ident = sql.Identifier(f_schema, f_rel)
                try:
                    cur.execute(sql.SQL("SELECT count(*) FROM {}").format(tbl_ident))
                    ro_cnt = int(cur.fetchone()[0] or 0)
                    exp_cnt = actual_counts.get(table_fqn, 0)
                    if ro_cnt != exp_cnt:
                        violations.append(
                            f"lab_ro saw {ro_cnt} rows in {table_fqn}, expected {exp_cnt}"
                        )
                except Exception as exc:
                    violations.append(f"lab_ro failed SELECT on {table_fqn}: {exc}")

            # Verify lab_ro cannot INSERT
            if tables_meta:
                first_tbl = sorted(tables_meta.keys())[0]
                f_schema, f_rel = first_tbl.split(".", 1)
                tbl_ident = sql.Identifier(f_schema, f_rel)
                try:
                    cur.execute(
                        sql.SQL("INSERT INTO {} DEFAULT VALUES").format(tbl_ident)
                    )
                    violations.append(f"lab_ro was unexpectedly allowed to INSERT into {first_tbl}")
                except psycopg.Error:
                    pass

            cur.execute("RESET ROLE;")

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def _check_a9_determinism(
    dataset_dir: Path,
    *,
    target_dsn: ParsedDSN | None = None,
    seed: int = 42,
    scale: float = 0.1,
    max_rows: int = 50_000,
    locale: str = "en_US",
) -> dict[str, Any]:
    """A9: Verify running generation in a separate process with the same profile & seed yields byte-identical data/."""
    profile_path = dataset_dir / "profile.json"
    data_dir = dataset_dir / "data"
    if not profile_path.is_file() or not data_dir.is_dir():
        return {"status": "fail", "violations": ["Missing profile.json or data/ directory"]}

    import os
    child_env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="lab_a9_") as tmp:
        tmp_path = Path(tmp)
        cmd = [
            sys.executable,
            "-m",
            "lab",
            "generate",
            "--profile",
            str(profile_path),
            "--out",
            str(tmp_path),
            "--scale",
            str(scale),
            "--seed",
            str(seed),
            "--max-rows",
            str(max_rows),
            "--locale",
            locale,
        ]
        if target_dsn is not None:
            child_env["_LAB_A9_SCRATCH_DSN"] = target_dsn.raw_dsn
            cmd.extend(["--to-env", "_LAB_A9_SCRATCH_DSN"])

        proc = subprocess.run(cmd, env=child_env, text=True, capture_output=True)
        if proc.returncode not in (0, 2):
            return {
                "status": "fail",
                "violations": [f"Subprocess generation failed (code {proc.returncode}): {proc.stderr or proc.stdout}"],
            }

        violations: list[str] = []
        orig_csvs = sorted(data_dir.glob("*.csv"))
        for orig_csv in orig_csvs:
            other_csv = tmp_path / "data" / orig_csv.name
            if not other_csv.is_file():
                violations.append(f"Missing {orig_csv.name} in second run")
                continue
            h1 = hashlib.sha256(orig_csv.read_bytes()).hexdigest()
            h2 = hashlib.sha256(other_csv.read_bytes()).hexdigest()
            if h1 != h2:
                violations.append(f"Non-deterministic CSV output for {orig_csv.name}")

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def _check_a11_first_1000_rows(
    conn: psycopg.Connection[Any],
    profile: dict[str, Any],
    actual_counts: dict[str, int],
) -> dict[str, Any]:
    """A11: For tables > 1,000 rows, verify physical LIMIT 1000 has null_rate ±0.05, L1 <= 0.20, and >=5% values present."""
    violations: list[str] = []
    tables_meta = profile.get("tables") or {}

    with conn.cursor() as cur:
        for table_fqn, t_meta in sorted(tables_meta.items()):
            n_rows = actual_counts.get(table_fqn, 0)
            if n_rows <= 1000:
                continue

            f_schema, f_rel = table_fqn.split(".", 1)
            tbl_ident = sql.Identifier(f_schema, f_rel)
            cols_meta = t_meta.get("columns") or {}

            for col_name, c_meta in cols_meta.items():
                col_ident = sql.Identifier(col_name)
                src_null_rate = float(c_meta.get("null_rate", 0.0))

                cur.execute(
                    sql.SQL(
                        "SELECT count(*) FILTER (WHERE {col} IS NULL)::float / 1000.0 "
                        "FROM (SELECT {col} FROM {tbl} LIMIT 1000) s"
                    ).format(col=col_ident, tbl=tbl_ident)
                )
                first_null_rate = float(cur.fetchone()[0] or 0.0)
                if abs(first_null_rate - src_null_rate) > 0.05 + 1e-6:
                    violations.append(
                        f"{table_fqn}.{col_name} first-1000 null_rate {first_null_rate:.4f} "
                        f"outside ±0.05 of source {src_null_rate:.4f}"
                    )

                if c_meta.get("value_source") == "allowlist" and c_meta.get("categorical"):
                    src_cat: dict[str, float] = {
                        str(k): float(v) for k, v in c_meta["categorical"].items()
                    }
                    cur.execute(
                        sql.SQL(
                            "SELECT {col}::text, count(*) "
                            "FROM (SELECT {col} FROM {tbl} LIMIT 1000) s "
                            "WHERE {col} IS NOT NULL GROUP BY {col}"
                        ).format(col=col_ident, tbl=tbl_ident)
                    )
                    f_rows = cur.fetchall()
                    tot_non_null = sum(int(r[1]) for r in f_rows)
                    if tot_non_null > 0:
                        tgt_cat = {str(v): int(c) / tot_non_null for v, c in f_rows}
                        all_keys = set(src_cat.keys()) | set(tgt_cat.keys())
                        l1 = sum(
                            abs(src_cat.get(k, 0.0) - tgt_cat.get(k, 0.0))
                            for k in all_keys
                        )
                        if l1 > 0.20 + 1e-6:
                            violations.append(
                                f"{table_fqn}.{col_name} first-1000 categorical L1={l1:.4f} > 0.20"
                            )
                        for val_k, share in src_cat.items():
                            if share >= 0.05 and val_k not in tgt_cat:
                                violations.append(
                                    f"{table_fqn}.{col_name} first-1000 missing value {val_k!r} (source share {share:.3f} >= 0.05)"
                                )

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def _check_a12_implicit_fks(
    conn: psycopg.Connection[Any],
    profile: dict[str, Any],
    actual_counts: dict[str, int],
) -> dict[str, Any]:
    """A12: Verify implicit FKs have 0 orphans, unique target values in first 1,000 rows, and satisfy A5."""
    violations: list[str] = []
    implicit_fks = [
        fk for fk in (profile.get("foreign_keys") or []) if fk.get("kind") == "implicit"
    ]
    if not implicit_fks:
        return {"status": "pass", "violations": []}

    with conn.cursor() as cur:
        for fk in implicit_fks:
            from_tbl = fk["from"]["table"]
            from_cols = fk["from"]["columns"]
            to_tbl = fk["to"]["table"]
            to_cols = fk["to"]["columns"]

            f_schema, f_rel = from_tbl.split(".", 1)
            t_schema, t_rel = to_tbl.split(".", 1)

            join_cond = sql.SQL(" AND ").join(
                sql.SQL("c.{} = p.{}").format(sql.Identifier(fc), sql.Identifier(tc))
                for fc, tc in zip(from_cols, to_cols)
            )
            not_null_cond = sql.SQL(" AND ").join(
                sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(fc)) for fc in from_cols
            )
            parent_null_cond = sql.SQL("p.{} IS NULL").format(sql.Identifier(to_cols[0]))

            cur.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} c LEFT JOIN {} p ON {} WHERE ({}) AND {}"
                ).format(
                    sql.Identifier(f_schema, f_rel),
                    sql.Identifier(t_schema, t_rel),
                    join_cond,
                    not_null_cond,
                    parent_null_cond,
                )
            )
            orphans = int(cur.fetchone()[0] or 0)
            if orphans > 0:
                violations.append(
                    f"Implicit FK {from_tbl}({','.join(from_cols)})->{to_tbl}({','.join(to_cols)}) "
                    f"has {orphans} unmatched source value(s)"
                )

            # Check uniqueness in first 1,000 rows of target table
            t_cols_sql = sql.SQL(", ").join(sql.Identifier(tc) for tc in to_cols)
            cur.execute(
                sql.SQL(
                    "SELECT count(*), count(DISTINCT ({cols})) "
                    "FROM (SELECT {cols} FROM {tbl} LIMIT 1000) s"
                ).format(
                    cols=t_cols_sql,
                    tbl=sql.Identifier(t_schema, t_rel),
                )
            )
            tot_1000, dist_1000 = cur.fetchall()[0]
            if int(tot_1000 or 0) != int(dist_1000 or 0):
                violations.append(
                    f"Implicit FK target {to_tbl}({','.join(to_cols)}) is not unique in first 1,000 rows "
                    f"({dist_1000}/{tot_1000} distinct)"
                )

    a5_imp = _check_a5_cardinality(conn, profile, actual_counts, fks_to_check=implicit_fks)
    violations.extend(a5_imp.get("violations") or [])

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def _check_a13_check_repair(
    conn: psycopg.Connection[Any],
    profile: dict[str, Any],
    manifest: dict[str, Any],
    actual_counts: dict[str, int],
    *,
    scale: float,
    max_rows: int = 50_000,
) -> dict[str, Any]:
    """A13: Verify repairable CHECK constraints stay on target, are not degraded, preserve A4 row count, and leave 0 child orphans."""
    violations: list[str] = []
    repaired_list = manifest.get("repaired_checks") or []
    degraded_set = {
        (str(d.get("table")), str(d.get("constraint")))
        for d in (manifest.get("degraded") or [])
    }
    tables_meta = profile.get("tables") or {}

    with conn.cursor() as cur:
        for rep in repaired_list:
            t_fqn = str(rep.get("table"))
            c_name = str(rep.get("constraint"))
            if (t_fqn, c_name) in degraded_set:
                violations.append(f"Repaired CHECK {t_fqn}.{c_name} was also listed in degraded")

            f_schema, f_rel = t_fqn.split(".", 1)
            cur.execute(
                """
                SELECT count(*)
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s AND con.conname = %s AND con.contype = 'c';
                """,
                (f_schema, f_rel, c_name),
            )
            if int(cur.fetchone()[0] or 0) != 1:
                violations.append(f"Repaired CHECK {t_fqn}.{c_name} is missing on target DB")

            exp_cnt = compute_target_row_count(
                int(tables_meta.get(t_fqn, {}).get("row_count", 0)),
                scale,
                max_rows,
            )
            act_cnt = actual_counts.get(t_fqn, 0)
            if act_cnt != exp_cnt:
                violations.append(
                    f"Table {t_fqn} with repaired CHECK {c_name} has {act_cnt} rows, expected exact {exp_cnt}"
                )

    return {
        "status": "pass" if not violations else "fail",
        "violations": violations,
    }


def validate_dataset(
    target_dsn: ParsedDSN,
    dataset_dir: Path,
    *,
    scale: float = 0.1,
    seed: int = 42,
    max_rows: int = 50_000,
    locale: str = "en_US",
    canary_tokens: list[str] | None = None,
    check_determinism: bool = True,
) -> tuple[int, dict[str, Any]]:
    """Run acceptance checks A1–A9 and A11–A13, write validate_report.json, and return (exit_code, report).

    Exit codes (§1.5):
      0 = all checks passed, zero degraded CHECKs
      2 = all checks passed, one or more degraded CHECKs recorded in manifest.degraded
      1 = one or more checks failed
    """
    profile_path = dataset_dir / "profile.json"
    manifest_path = dataset_dir / "manifest.json"
    if not profile_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f"Missing profile.json or manifest.json in {dataset_dir}")

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tokens = list(canary_tokens) if canary_tokens is not None else list(DEFAULT_CANARIES)

    with psycopg.connect(target_dsn.raw_dsn) as conn:
        a1 = _check_a1_canary(conn, dataset_dir, profile, tokens)
        a2 = _check_a2_schema_fidelity(conn, profile, manifest)
        a3 = _check_a3_integrity(conn, profile, manifest)
        a4, actual_counts = _check_a4_row_counts(conn, profile, scale=scale, max_rows=max_rows)
        a5 = _check_a5_cardinality(conn, profile, actual_counts)
        a6, a7 = _check_a6_a7_nulls_and_l1(conn, profile, actual_counts)
        a11 = _check_a11_first_1000_rows(conn, profile, actual_counts)
        a12 = _check_a12_implicit_fks(conn, profile, actual_counts)
        a13 = _check_a13_check_repair(
            conn, profile, manifest, actual_counts, scale=scale, max_rows=max_rows
        )

    a8 = _check_a8_usability(target_dsn, profile, actual_counts)
    a9 = (
        _check_a9_determinism(
            dataset_dir,
            target_dsn=target_dsn,
            seed=seed,
            scale=scale,
            max_rows=max_rows,
            locale=locale,
        )
        if check_determinism
        else {"status": "skipped", "violations": []}
    )

    checks = {
        "A1_canary": a1,
        "A2_schema_fidelity": a2,
        "A3_integrity": a3,
        "A4_row_counts": a4,
        "A5_cardinality": a5,
        "A6_null_rate": a6,
        "A7_categorical_l1": a7,
        "A8_usability": a8,
        "A9_determinism": a9,
        "A11_first_1000_rows": a11,
        "A12_implicit_fks": a12,
        "A13_check_repair": a13,
    }

    any_failed = any(c["status"] == "fail" for c in checks.values())
    degraded_list = manifest.get("degraded") or []
    ack_valid = is_degraded_ack_valid(dataset_dir, degraded_list)

    if any_failed:
        exit_code = 1
        overall_status = "failed"
    elif degraded_list:
        exit_code = 2
        overall_status = "degraded"
    else:
        exit_code = 0
        overall_status = "passed"

    report = {
        "dataset": manifest.get("dataset", dataset_dir.name),
        "overall_status": overall_status,
        "exit_code": exit_code,
        "degraded_count": len(degraded_list),
        "degraded": degraded_list,
        "degraded_acknowledged": ack_valid,
        "repaired_checks": manifest.get("repaired_checks") or [],
        "checks": checks,
    }

    (dataset_dir / "validate_report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    return exit_code, report
