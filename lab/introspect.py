"""Read-only source database introspection and privacy-preserving profiler (§1.1, §1.4)."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import sys
from typing import Any
import psycopg
from psycopg import sql

from lab.check_parser import parse_table_checks
from lab.dsn import ParsedDSN
from lab.pii import PIIMatcher


DEFAULT_EXCLUDE_TABLE_REGEX = (
    r"^(.*_migrations|alembic_version|flyway_.*|audit_.*|.*_log|"
    r"sessions?|outbox|celery_.*|pg_.*)$"
)

_IMPLICIT_FK_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\(\s*([^)]+)\s*\)\s*=\s*"
    r"([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\(\s*([^)]+)\s*\)\s*$"
)


def parse_implicit_fks(raw_specs: list[str] | None) -> list[dict[str, Any]]:
    """Parse --implicit-fk 'schema.child(c1,c2)=schema.parent(p1,p2)' specifications."""
    results: list[dict[str, Any]] = []
    for spec in raw_specs or []:
        m = _IMPLICIT_FK_RE.match(spec)
        if not m:
            raise ValueError(
                f"Invalid --implicit-fk specification {spec!r}. "
                "Expected format: 'schema.child_table(col1,col2)=schema.parent_table(pk1,pk2)'."
            )
        from_tbl = m.group(1).strip()
        from_cols = [c.strip() for c in m.group(2).split(",") if c.strip()]
        to_tbl = m.group(3).strip()
        to_cols = [c.strip() for c in m.group(4).split(",") if c.strip()]
        if not from_cols or len(from_cols) != len(to_cols):
            raise ValueError(
                f"Invalid --implicit-fk {spec!r}: source and target column lists must have the same non-zero length."
            )
        results.append(
            {
                "name": f"implicit_{from_tbl.replace('.', '_')}_{'_'.join(from_cols)}",
                "kind": "implicit",
                "from": {"table": from_tbl, "columns": from_cols},
                "to": {"table": to_tbl, "columns": to_cols},
            }
        )
    return results


def _is_text_type(pg_type: str, udt_name: str) -> bool:
    t = pg_type.lower()
    return (
        t in ("text", "character varying", "character", "bpchar", "citext", "name", "varchar", "char")
        or t.startswith("character varying(")
        or t.startswith("character(")
        or t.startswith("varchar(")
        or t.startswith("char(")
        or udt_name.lower() in ("text", "varchar", "bpchar", "citext", "name")
    )


def _is_numeric_or_temporal_type(pg_type: str) -> bool:
    t = pg_type.lower()
    if any(
        t.startswith(prefix)
        for prefix in (
            "smallint",
            "integer",
            "bigint",
            "numeric",
            "decimal",
            "real",
            "double precision",
            "date",
            "timestamp",
            "time ",
            "time without",
            "time with",
        )
    ):
        return True
    return t in ("int2", "int4", "int8", "float4", "float8", "date", "time", "timetz", "timestamp", "timestamptz")


def _is_json_type(pg_type: str, udt_name: str) -> bool:
    return pg_type.lower() in ("json", "jsonb") or udt_name.lower() in ("json", "jsonb")


def introspect_source(
    source_dsn: ParsedDSN,
    *,
    allow_categorical: list[str] | None = None,
    pii_extra: list[str] | None = None,
    override_pii_veto: list[str] | None = None,
    implicit_fks: list[str] | None = None,
    exclude_regex: str | None = None,
) -> dict[str, Any]:
    """Profile a PostgreSQL database through a read-only session and return the profile dict."""
    pii_matcher = PIIMatcher(extra_patterns=pii_extra, raw_overrides=override_pii_veto)
    parsed_implicit_fks = parse_implicit_fks(implicit_fks)
    allow_set = {c.strip() for c in (allow_categorical or []) if c.strip()}
    for c_fqn in allow_set:
        if c_fqn.count(".") != 2:
            raise ValueError(
                f"Invalid --allow-categorical entry {c_fqn!r}: expected 'schema.table.column'."
            )

    combined_exclude = DEFAULT_EXCLUDE_TABLE_REGEX
    if exclude_regex:
        combined_exclude = f"({DEFAULT_EXCLUDE_TABLE_REGEX})|({exclude_regex})"
    exclude_re = re.compile(combined_exclude, re.IGNORECASE)

    with psycopg.connect(source_dsn.raw_dsn) as conn:
        with conn.cursor() as cur:
            # Enforce §1.4 read-only session and timeout immediately
            cur.execute("SET default_transaction_read_only = on;")
            cur.execute("SET statement_timeout = '60s';")

            cur.execute("SHOW server_version;")
            server_version = cur.fetchone()[0]

            # 1. Enum types
            cur.execute(
                """
                SELECT n.nspname AS schema_name,
                       t.typname AS enum_name,
                       e.enumlabel AS label
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                JOIN pg_enum e ON e.enumtypid = t.oid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                ORDER BY n.nspname, t.typname, e.enumsortorder;
                """
            )
            enums: dict[str, list[str]] = {}
            for schema_name, enum_name, label in cur.fetchall():
                enums.setdefault(f"{schema_name}.{enum_name}", []).append(label)

            # 2. Views
            cur.execute(
                """
                SELECT n.nspname, c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'v'
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_depend d
                      WHERE d.objid = c.oid AND d.deptype = 'e'
                  )
                ORDER BY n.nspname, c.relname;
                """
            )
            views = [f"{r[0]}.{r[1]}" for r in cur.fetchall()]

            # 3. Base tables
            cur.execute(
                """
                SELECT n.nspname AS schema_name,
                       c.relname AS table_name,
                       c.oid AS table_oid,
                       c.relrowsecurity OR c.relforcerowsecurity AS has_rls
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p')
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_depend d
                      WHERE d.objid = c.oid AND d.deptype = 'e'
                  )
                ORDER BY n.nspname, c.relname;
                """
            )
            raw_tables = cur.fetchall()

            excluded_tables: list[str] = []
            included_tables: list[tuple[str, str, int, bool]] = []
            for schema_name, table_name, table_oid, has_rls in raw_tables:
                fqn = f"{schema_name}.{table_name}"
                if exclude_re.search(table_name) or exclude_re.search(fqn):
                    excluded_tables.append(fqn)
                else:
                    included_tables.append((schema_name, table_name, table_oid, bool(has_rls)))

            included_fqns = {f"{s}.{t}" for s, t, _, _ in included_tables}

            # 4. Physical Foreign Keys across included tables
            cur.execute(
                """
                SELECT con.conname,
                       ns_from.nspname AS from_schema,
                       cl_from.relname AS from_table,
                       con.conkey AS from_attnums,
                       ns_to.nspname AS to_schema,
                       cl_to.relname AS to_table,
                       con.confkey AS to_attnums,
                       con.conrelid,
                       con.confrelid
                FROM pg_constraint con
                JOIN pg_class cl_from ON cl_from.oid = con.conrelid
                JOIN pg_namespace ns_from ON ns_from.oid = cl_from.relnamespace
                JOIN pg_class cl_to ON cl_to.oid = con.confrelid
                JOIN pg_namespace ns_to ON ns_to.oid = cl_to.relnamespace
                WHERE con.contype = 'f'
                ORDER BY ns_from.nspname, cl_from.relname, con.conname;
                """
            )
            raw_fks = cur.fetchall()

            # Helper map: (table_oid, attnum) -> col_name
            cur.execute(
                """
                SELECT a.attrelid, a.attnum, a.attname
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p')
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema');
                """
            )
            attnum_map: dict[tuple[int, int], str] = {
                (int(relid), int(attnum)): str(attname)
                for relid, attnum, attname in cur.fetchall()
            }

            fk_Specs: list[dict[str, Any]] = []
            fk_columns_by_table: dict[str, set[str]] = {}
            for (
                conname,
                from_schema,
                from_table,
                from_attnums,
                to_schema,
                to_table,
                to_attnums,
                conrelid,
                confrelid,
            ) in raw_fks:
                from_fqn = f"{from_schema}.{from_table}"
                to_fqn = f"{to_schema}.{to_table}"
                if from_fqn not in included_fqns or to_fqn not in included_fqns:
                    continue
                from_cols = [attnum_map[(int(conrelid), int(a))] for a in from_attnums]
                to_cols = [attnum_map[(int(confrelid), int(a))] for a in to_attnums]
                fk_Specs.append(
                    {
                        "name": str(conname),
                        "kind": "fk",
                        "from": {"table": from_fqn, "columns": from_cols},
                        "to": {"table": to_fqn, "columns": to_cols},
                    }
                )
                fk_columns_by_table.setdefault(from_fqn, set()).update(from_cols)

            for imp in parsed_implicit_fks:
                from_fqn = imp["from"]["table"]
                to_fqn = imp["to"]["table"]
                if from_fqn not in included_fqns:
                    raise ValueError(f"--implicit-fk source table {from_fqn!r} not found in schema.")
                if to_fqn not in included_fqns:
                    raise ValueError(f"--implicit-fk target table {to_fqn!r} not found in schema.")
                fk_Specs.append(dict(imp))
                fk_columns_by_table.setdefault(from_fqn, set()).update(imp["from"]["columns"])

            tables_dict: dict[str, Any] = {}
            all_columns_fqn: dict[str, str] = {}
            allowlist_candidates: list[dict[str, Any]] = []
            vetoed_candidates: list[dict[str, Any]] = []

            for schema_name, table_name, table_oid, has_rls in included_tables:
                fqn = f"{schema_name}.{table_name}"
                tbl_ident = sql.Identifier(schema_name, table_name)

                # Row count
                cur.execute(sql.SQL("SELECT count(*) FROM {}").format(tbl_ident))
                row_count = int(cur.fetchone()[0])
                use_sample = row_count > 1_000_000
                sample_clause = sql.SQL(" TABLESAMPLE SYSTEM (1)") if use_sample else sql.SQL("")

                # Primary Key
                cur.execute(
                    """
                    SELECT con.conkey
                    FROM pg_constraint con
                    WHERE con.conrelid = %s AND con.contype = 'p';
                    """,
                    (table_oid,),
                )
                pk_row = cur.fetchone()
                pk_cols = [attnum_map[(int(table_oid), int(a))] for a in pk_row[0]] if pk_row else []

                # Unique constraints & unique indexes (including partial indexes)
                cur.execute(
                    """
                    SELECT c.relname AS index_name,
                           i.indkey AS indkey,
                           pg_get_expr(i.indpred, i.indrelid) AS pred,
                           pg_get_indexdef(i.indexrelid) AS indexdef
                    FROM pg_index i
                    JOIN pg_class c ON c.oid = i.indexrelid
                    WHERE i.indrelid = %s
                      AND i.indisunique
                      AND NOT i.indisprimary
                    ORDER BY c.relname;
                    """,
                    (table_oid,),
                )
                table_col_names = {
                    col_n for (rel_id, _), col_n in attnum_map.items() if rel_id == int(table_oid)
                }
                unique_list: list[dict[str, Any]] = []
                for idx_name, indkey, pred, indexdef in cur.fetchall():
                    # indkey is an int2vector; 0 indicates an expression column (e.g., lower(branch_name))
                    att_nums = [int(x) for x in str(indkey).split() if x.lstrip("-").isdigit()]
                    cols: list[str] = []
                    for a in att_nums:
                        if a > 0 and (int(table_oid), a) in attnum_map:
                            cols.append(attnum_map[(int(table_oid), a)])
                    if 0 in att_nums and indexdef:
                        # Extract column identifiers from the USING btree (...) clause before WHERE
                        idx_str = str(indexdef)
                        where_pos = idx_str.upper().find(" WHERE ")
                        key_part = idx_str[:where_pos] if where_pos != -1 else idx_str
                        m_using = re.search(r"\bUSING\s+\w+\s*\((.*)\)", key_part, re.IGNORECASE)
                        if m_using:
                            for ident in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", m_using.group(1)):
                                if ident in table_col_names and ident not in cols:
                                    cols.append(ident)
                    unique_list.append(
                        {
                            "name": str(idx_name),
                            "columns": cols,
                            "predicate": str(pred) if pred else None,
                            "indexdef": str(indexdef),
                        }
                    )

                # CHECK constraints
                cur.execute(
                    """
                    SELECT con.conname,
                           pg_get_constraintdef(con.oid, true) AS expr
                    FROM pg_constraint con
                    WHERE con.conrelid = %s AND con.contype = 'c'
                    ORDER BY con.conname;
                    """,
                    (table_oid,),
                )
                checks_list = [
                    {"name": str(cname), "expr": str(cexpr)}
                    for cname, cexpr in cur.fetchall()
                ]

                # Columns metadata
                cur.execute(
                    """
                    SELECT a.attnum,
                           a.attname,
                           format_type(a.atttypid, a.atttypmod) AS formatted_type,
                           tn.nspname AS type_schema,
                           t.typname AS udt_name,
                           t.typtype AS typtype,
                           NOT a.attnotnull AS is_nullable,
                           a.attidentity AS attidentity,
                           a.atthasdef AS has_default,
                           pg_get_expr(ad.adbin, ad.adrelid) AS default_expr,
                           a.attndims AS dimensions,
                           bt.typname AS elem_udt_name
                    FROM pg_attribute a
                    JOIN pg_type t ON t.oid = a.atttypid
                    JOIN pg_namespace tn ON tn.oid = t.typnamespace
                    LEFT JOIN pg_type bt ON bt.oid = t.typelem
                    LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                    WHERE a.attrelid = %s
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY a.attnum;
                    """,
                    (table_oid,),
                )
                col_rows = cur.fetchall()
                col_names_set = {str(r[1]) for r in col_rows}
                check_hints = parse_table_checks(checks_list, col_names_set)

                columns_dict: dict[str, Any] = {}
                for (
                    attnum,
                    col_name,
                    formatted_type,
                    type_schema,
                    udt_name,
                    typtype,
                    is_nullable,
                    attidentity,
                    has_default,
                    default_expr,
                    dimensions,
                    elem_udt_name,
                ) in col_rows:
                    col_name = str(col_name)
                    col_fqn = f"{fqn}.{col_name}"
                    all_columns_fqn[col_fqn] = col_name
                    col_ident = sql.Identifier(col_name)

                    default_str = str(default_expr) if default_expr else None
                    is_ident = bool(attidentity) or (
                        default_str is not None and "nextval(" in default_str.lower()
                    )
                    is_array = int(dimensions or 0) > 0 or str(formatted_type).endswith("[]") or str(udt_name).startswith("_")
                    udt_fqn = f"{type_schema}.{udt_name}"

                    pii_pattern = pii_matcher.match_column(col_name)
                    pii_category = pii_matcher.classify_pii_category(col_name)
                    is_overridden = pii_matcher.is_veto_overridden(col_fqn)

                    # Validate if column is in --allow-categorical
                    if col_fqn in allow_set and pii_pattern is not None and not is_overridden:
                        raise ValueError(
                            f"Privacy violation (§1.1): Column {col_fqn!r} matches PII pattern {pii_pattern!r} "
                            f"and cannot be allowlisted without --override-pii-veto '{col_fqn}=<reason>'."
                        )

                    col_meta: dict[str, Any] = {
                        "type": str(formatted_type),
                        "udt_type": udt_fqn,
                        "nullable": bool(is_nullable),
                        "null_rate": 0.0,
                        "is_identity": is_ident,
                    }
                    if pii_category is not None:
                        col_meta["pii"] = pii_category
                        col_meta["pii_matched_pattern"] = pii_pattern

                    # Safe structural default markers (never copy literal strings from defaults)
                    if default_str:
                        low_def = default_str.lower()
                        if "gen_random_uuid()" in low_def or "uuid_generate_v4()" in low_def:
                            col_meta["default_kind"] = "uuid"
                        elif "now()" in low_def or "current_timestamp" in low_def or "clock_timestamp()" in low_def:
                            col_meta["default_kind"] = "now"
                        elif low_def in ("true", "false"):
                            col_meta["default_kind"] = f"bool:{low_def}"
                        elif low_def.startswith("'{}'::json"):
                            col_meta["default_kind"] = "empty_json"

                    if row_count == 0:
                        columns_dict[col_name] = col_meta
                        continue

                    # Null rate
                    cur.execute(
                        sql.SQL(
                            "SELECT count(*) FILTER (WHERE {col} IS NULL)::float / GREATEST(count(*), 1) "
                            "FROM {tbl}{sample}"
                        ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                    )
                    null_rate = float(cur.fetchone()[0] or 0.0)
                    col_meta["null_rate"] = round(null_rate, 6)

                    # Branch by column type according to §1.1 Privacy Boundary
                    if is_array:
                        col_meta["is_array"] = True
                        col_meta["base_type"] = str(elem_udt_name or formatted_type.rstrip("[]"))
                        cur.execute(
                            sql.SQL(
                                "SELECT min(cardinality({col})), max(cardinality({col})), avg(cardinality({col}))::float "
                                "FROM {tbl}{sample} WHERE {col} IS NOT NULL"
                            ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                        )
                        c_min, c_max, c_avg = cur.fetchone()
                        col_meta["cardinality"] = {
                            "min": int(c_min if c_min is not None else 0),
                            "max": int(c_max if c_max is not None else 2),
                            "avg": round(float(c_avg if c_avg is not None else 1.0), 2),
                        }
                        if col_name in check_hints.array_elements:
                            col_meta["allowed_elements"] = check_hints.array_elements[col_name]
                        if col_name in check_hints.array_card_bounds:
                            lo, hi = check_hints.array_card_bounds[col_name]
                            col_meta["cardinality"]["min"] = max(col_meta["cardinality"]["min"], lo)
                            col_meta["cardinality"]["max"] = min(max(col_meta["cardinality"]["max"], lo), hi)

                    elif _is_json_type(str(formatted_type), str(udt_name)):
                        col_meta["is_json"] = True
                        cur.execute(
                            sql.SQL(
                                "SELECT min(char_length({col}::text)), max(char_length({col}::text)), avg(char_length({col}::text))::float "
                                "FROM {tbl}{sample} WHERE {col} IS NOT NULL"
                            ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                        )
                        l_min, l_max, l_avg = cur.fetchone()
                        col_meta["len"] = {
                            "min": int(l_min if l_min is not None else 2),
                            "max": int(l_max if l_max is not None else 2),
                            "avg": round(float(l_avg if l_avg is not None else 2.0), 2),
                        }

                    elif str(formatted_type).lower() == "boolean" or str(udt_name).lower() == "bool":
                        cur.execute(
                            sql.SQL(
                                "SELECT count(*) FILTER (WHERE {col} IS TRUE), "
                                "       count(*) FILTER (WHERE {col} IS FALSE) "
                                "FROM {tbl}{sample} WHERE {col} IS NOT NULL"
                            ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                        )
                        n_true, n_false = cur.fetchone()
                        tot_bool = int(n_true or 0) + int(n_false or 0)
                        if tot_bool > 0:
                            col_meta["categorical"] = {
                                "true": round(int(n_true) / tot_bool, 6),
                                "false": round(int(n_false) / tot_bool, 6),
                            }
                        else:
                            col_meta["categorical"] = {"true": 0.5, "false": 0.5}

                    elif udt_fqn in enums:
                        col_meta["enum_type"] = udt_fqn
                        col_meta["allowed_values"] = list(enums[udt_fqn])
                        col_meta["value_source"] = "enum"

                    elif _is_text_type(str(formatted_type), str(udt_name)):
                        # §1.1: Text columns NEVER read min(col) or max(col) values!
                        cur.execute(
                            sql.SQL(
                                "SELECT count(DISTINCT {col}), "
                                "       min(char_length({col})), "
                                "       max(char_length({col})), "
                                "       avg(char_length({col}))::float "
                                "FROM {tbl}{sample} WHERE {col} IS NOT NULL"
                            ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                        )
                        n_dist, l_min, l_max, l_avg = cur.fetchone()
                        n_dist_int = int(n_dist or 0)
                        max_len_int = int(l_max if l_max is not None else 12)
                        col_meta["distinct"] = n_dist_int
                        col_meta["len"] = {
                            "min": int(l_min if l_min is not None else 1),
                            "max": max_len_int,
                            "avg": round(float(l_avg if l_avg is not None else 10.0), 2),
                        }

                        if col_name in check_hints.in_values:
                            col_meta["allowed_values"] = list(check_hints.in_values[col_name])
                            col_meta["value_source"] = "check_in"

                        # Candidate evaluation for --profile-only (§1.1)
                        is_key_col = col_name in pk_cols or col_name in fk_columns_by_table.get(fqn, set())
                        if (
                            1 <= n_dist_int <= 25
                            and max_len_int <= 30
                            and not is_key_col
                            and col_name not in check_hints.in_values
                        ):
                            if pii_pattern is not None and not is_overridden:
                                vetoed_candidates.append(
                                    {
                                        "column": col_fqn,
                                        "distinct": n_dist_int,
                                        "max_len": max_len_int,
                                        "matched_pattern": pii_pattern,
                                    }
                                )
                            else:
                                allowlist_candidates.append(
                                    {
                                        "column": col_fqn,
                                        "distinct": n_dist_int,
                                        "max_len": max_len_int,
                                    }
                                )

                    elif _is_numeric_or_temporal_type(str(formatted_type)):
                        # §1.1: Min and max are read ONLY for numeric, date and timestamp columns,
                        # and NEVER when the column name matches the PII pattern (even if veto overridden).
                        if pii_pattern is None:
                            cur.execute(
                                sql.SQL(
                                    "SELECT count(DISTINCT {col}), min({col})::text, max({col})::text "
                                    "FROM {tbl}{sample} WHERE {col} IS NOT NULL"
                                ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                            )
                            n_dist, v_min, v_max = cur.fetchone()
                            col_meta["distinct"] = int(n_dist or 0)
                            if v_min is not None and v_max is not None:
                                if any(
                                    str(formatted_type).lower().startswith(p)
                                    for p in ("smallint", "integer", "bigint", "int")
                                ):
                                    col_meta["min"] = int(float(v_min))
                                    col_meta["max"] = int(float(v_max))
                                elif any(
                                    str(formatted_type).lower().startswith(p)
                                    for p in ("numeric", "decimal", "real", "double", "float")
                                ):
                                    col_meta["min"] = float(v_min)
                                    col_meta["max"] = float(v_max)
                                else:
                                    col_meta["min"] = str(v_min)
                                    col_meta["max"] = str(v_max)
                        else:
                            cur.execute(
                                sql.SQL(
                                    "SELECT count(DISTINCT {col}) FROM {tbl}{sample} WHERE {col} IS NOT NULL"
                                ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                            )
                            n_dist = cur.fetchone()[0]
                            col_meta["distinct"] = int(n_dist or 0)

                        if col_name in check_hints.in_values:
                            col_meta["allowed_values"] = list(check_hints.in_values[col_name])
                            col_meta["value_source"] = "check_in"
                    else:
                        # UUID or other types
                        cur.execute(
                            sql.SQL(
                                "SELECT count(DISTINCT {col}) FROM {tbl}{sample} WHERE {col} IS NOT NULL"
                            ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                        )
                        col_meta["distinct"] = int(cur.fetchone()[0] or 0)

                    # §1.1 Source (c): Explicit human-reviewed --allow-categorical frequencies
                    if col_fqn in allow_set:
                        cur.execute(
                            sql.SQL(
                                "SELECT {col}::text, count(*) "
                                "FROM {tbl}{sample} "
                                "WHERE {col} IS NOT NULL "
                                "GROUP BY {col} "
                                "ORDER BY {col}::text"
                            ).format(col=col_ident, tbl=tbl_ident, sample=sample_clause)
                        )
                        freq_rows = cur.fetchall()
                        tot_non_null = sum(int(r[1]) for r in freq_rows)
                        if tot_non_null > 0:
                            col_meta["categorical"] = {
                                str(val): round(int(cnt) / tot_non_null, 6)
                                for val, cnt in freq_rows
                            }
                            col_meta["value_source"] = "allowlist"

                    columns_dict[col_name] = col_meta

                tables_dict[fqn] = {
                    "row_count": row_count,
                    "pk": pk_cols,
                    "unique": unique_list,
                    "checks": checks_list,
                    "has_rls": has_rls,
                    "columns": columns_dict,
                }

            # Validate that every --allow-categorical column exists
            for c_fqn in sorted(allow_set):
                if c_fqn not in all_columns_fqn:
                    raise ValueError(f"--allow-categorical references unknown column {c_fqn!r}.")

            pii_overrides_audit = pii_matcher.build_audit_overrides(all_columns_fqn)

            # 5. Profile Foreign Keys & Implicit FKs (null_rate, children_per_parent, coverage)
            profiled_fks: list[dict[str, Any]] = []
            for fk in fk_Specs:
                from_tbl = fk["from"]["table"]
                from_cols = fk["from"]["columns"]
                to_tbl = fk["to"]["table"]
                to_cols = fk["to"]["columns"]

                for c in from_cols:
                    if c not in tables_dict[from_tbl]["columns"]:
                        raise ValueError(f"FK column {from_tbl}.{c} does not exist.")
                for c in to_cols:
                    if c not in tables_dict[to_tbl]["columns"]:
                        raise ValueError(f"FK column {to_tbl}.{c} does not exist.")

                from_schema, from_rel = from_tbl.split(".", 1)
                to_schema, to_rel = to_tbl.split(".", 1)
                from_ident = sql.Identifier(from_schema, from_rel)
                to_ident = sql.Identifier(to_schema, to_rel)

                is_nullable = any(
                    tables_dict[from_tbl]["columns"][c]["nullable"] for c in from_cols
                )
                child_row_count = tables_dict[from_tbl]["row_count"]
                parent_row_count = tables_dict[to_tbl]["row_count"]

                if child_row_count == 0 or parent_row_count == 0:
                    profiled_fks.append(
                        {
                            **fk,
                            "nullable": is_nullable,
                            "null_rate": 0.0,
                            "children_per_parent": {"avg": 0.0, "max": 0},
                            "coverage": 0.0,
                        }
                    )
                    continue

                null_cond = sql.SQL(" OR ").join(
                    sql.SQL("c.{} IS NULL").format(sql.Identifier(col)) for col in from_cols
                )
                not_null_cond = sql.SQL(" AND ").join(
                    sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(col)) for col in from_cols
                )
                group_cols = sql.SQL(", ").join(
                    sql.SQL("c.{}").format(sql.Identifier(col)) for col in from_cols
                )

                cur.execute(
                    sql.SQL(
                        "SELECT count(*) FILTER (WHERE {null_cond})::float / GREATEST(count(*), 1), "
                        "       count(*) FILTER (WHERE {not_null_cond}) "
                        "FROM {from_tbl} c"
                    ).format(
                        null_cond=null_cond,
                        not_null_cond=not_null_cond,
                        from_tbl=from_ident,
                    )
                )
                fk_null_rate, non_null_children = cur.fetchone()
                fk_null_rate = float(fk_null_rate or 0.0)
                non_null_children = int(non_null_children or 0)

                cur.execute(
                    sql.SQL(
                        "SELECT count(*), COALESCE(max(cnt), 0) FROM ("
                        "  SELECT count(*) AS cnt FROM {from_tbl} c "
                        "  WHERE {not_null_cond} "
                        "  GROUP BY {group_cols}"
                        ") sub"
                    ).format(
                        from_tbl=from_ident,
                        not_null_cond=not_null_cond,
                        group_cols=group_cols,
                    )
                )
                distinct_parents_referenced, max_children = cur.fetchone()
                distinct_parents_referenced = int(distinct_parents_referenced or 0)
                max_children = int(max_children or 0)

                avg_per_parent = round(non_null_children / max(parent_row_count, 1), 6)
                coverage = round(min(1.0, distinct_parents_referenced / max(parent_row_count, 1)), 6)

                profiled_fks.append(
                    {
                        **fk,
                        "nullable": is_nullable,
                        "null_rate": round(fk_null_rate, 6),
                        "children_per_parent": {
                            "avg": avg_per_parent,
                            "max": max_children,
                        },
                        "coverage": coverage,
                    }
                )

    return {
        "source": {
            "dialect": "postgresql",
            "server_version": str(server_version),
            "profiled_at": datetime.now(timezone.utc).isoformat(),
            "scale_note": "TABLESAMPLE 1% on tables > 1M rows",
        },
        "excluded_tables": excluded_tables,
        "enums": enums,
        "tables": tables_dict,
        "foreign_keys": profiled_fks,
        "views": views,
        "pii_veto_overrides": pii_overrides_audit,
        "allowlist_candidates": allowlist_candidates,
        "vetoed_candidates": vetoed_candidates,
    }


def print_candidates_report(profile: dict[str, Any], out_stream: Any = sys.stdout) -> None:
    """Print allowlist candidates and PII-vetoed columns without ever printing data values (§1.1)."""
    candidates = profile.get("allowlist_candidates") or []
    vetoed = profile.get("vetoed_candidates") or []

    out_stream.write("\n=== Categorical Allowlist Candidates (≤ 25 distinct, max_len ≤ 30, no PII match) ===\n")
    if not candidates:
        out_stream.write("  (none)\n")
    else:
        for item in candidates:
            out_stream.write(
                f"  --allow-categorical {item['column']}   (distinct={item['distinct']}, max_len={item['max_len']})\n"
            )

    out_stream.write("\n=== Vetoed Low-Cardinality Columns (matched PII pattern; require --override-pii-veto) ===\n")
    if not vetoed:
        out_stream.write("  (none)\n")
    else:
        for item in vetoed:
            out_stream.write(
                f"  {item['column']}   (distinct={item['distinct']}, max_len={item['max_len']}, "
                f"matched_pii={item['matched_pattern']!r})\n"
            )
    out_stream.write("\n")
