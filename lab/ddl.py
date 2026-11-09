"""DDL extraction, pg_dump password-safe wrapper, and DDL sanitizer (§1.2, §1.4)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any
import psycopg
from psycopg import sql

from lab.dsn import ParsedDSN, dsn_contains_password


# Statements stripped by sanitize_ddl (§1.4 + trigger & COMMENT ON filtering)
_STRIP_STATEMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*CREATE\s+POLICY\b", re.IGNORECASE),
    re.compile(r"^\s*ALTER\s+POLICY\b", re.IGNORECASE),
    re.compile(r"^\s*DROP\s+POLICY\b", re.IGNORECASE),
    re.compile(
        r"^\s*ALTER\s+TABLE\b[^;]*\b(?:ENABLE|DISABLE|FORCE|NO\s+FORCE)\s+ROW\s+LEVEL\s+SECURITY\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*ALTER\s+TABLE\b[^;]*\b(?:ENABLE|DISABLE)\s+(?:REPLICA\s+|ALWAYS\s+)?TRIGGER\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"^\s*COMMENT\s+ON\b", re.IGNORECASE),
    re.compile(r"^\s*ALTER\b[^;]*\bOWNER\s+TO\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"^\s*GRANT\b", re.IGNORECASE),
    re.compile(r"^\s*REVOKE\b", re.IGNORECASE),
    re.compile(r"^\s*SET\s+default_tablespace\b", re.IGNORECASE),
    re.compile(r"^\s*SET\s+default_table_access_method\b", re.IGNORECASE),
    re.compile(r"^\s*ALTER\b[^;]*\bSET\s+TABLESPACE\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"^\s*CREATE\s+ROLE\b", re.IGNORECASE),
    re.compile(r"^\s*ALTER\s+ROLE\b", re.IGNORECASE),
    re.compile(r"^\s*SECURITY\s+LABEL\b", re.IGNORECASE),
)


def _split_sql_statements(raw_sql: str) -> list[str]:
    """Split a SQL script into top-level statements while respecting quotes and $$ blocks."""
    cleaned_lines: list[str] = []
    for line in raw_sql.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("\\restrict")
            or stripped.startswith("\\unrestrict")
            or stripped.startswith("\\connect")
            or stripped.startswith("\\c ")
        ):
            continue
        if stripped.startswith("--"):
            continue
        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    statements: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    in_line_comment = False
    dollar_tag: str | None = None
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                buf.append(ch)
            i += 1
            continue

        if dollar_tag is not None:
            if text.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(ch)
                i += 1
            continue

        if in_single:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append(text[i + 1])
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    buf.append(text[i + 1])
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            in_line_comment = True
            i += 2
            continue

        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue

        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue

        if ch == "$":
            m = re.match(r"^\$([a-zA-Z0-9_]*)\$", text[i:])
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue

        if ch == ";":
            buf.append(";")
            stmt = "".join(buf).strip()
            if stmt and stmt != ";":
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail if tail.endswith(";") else f"{tail};")
    return statements


def sanitize_ddl(raw_sql: str) -> str:
    """Sanitize raw DDL by removing RLS, triggers, COMMENT ON, OWNER, GRANT/REVOKE, tablespaces, and psql meta-commands."""
    kept: list[str] = []
    for stmt in _split_sql_statements(raw_sql):
        if any(pat.search(stmt) for pat in _STRIP_STATEMENT_PATTERNS):
            continue
        # Strip inline 'WITH (..., tablespace=...)' if present
        kept.append(stmt)
    return "\n\n".join(kept) + ("\n" if kept else "")


def build_pg_dump_command(
    dsn: ParsedDSN,
    *,
    docker_container: str | None = "postgres",
) -> tuple[list[str], dict[str, str]]:
    """Construct a passwordless-argv pg_dump command and its environment dict (§1.4)."""
    env = dsn.to_pg_env(os.environ.copy())
    if docker_container:
        # Use `docker exec -e PGPASSWORD` (without =value) so Docker reads PGPASSWORD from env,
        # ensuring the password never appears in the host or container process argv!
        host_Inside = "127.0.0.1" if dsn.host in ("localhost", "127.0.0.1") else dsn.host
        cmd = [
            "docker",
            "exec",
            "-e",
            "PGPASSWORD",
            docker_container,
            "pg_dump",
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--no-tablespaces",
            "--no-comments",
            "-h",
            host_Inside,
            "-p",
            str(dsn.port),
            "-U",
            dsn.user,
            "-d",
            dsn.dbname,
        ]
    else:
        cmd = [
            "pg_dump",
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--no-tablespaces",
            "--no-comments",
            "-h",
            dsn.host,
            "-p",
            str(dsn.port),
            "-U",
            dsn.user,
            "-d",
            dsn.dbname,
        ]

    # Verify invariant: no argument in cmd contains a password
    for arg in cmd:
        if dsn_contains_password(arg) or (dsn.password and dsn.password in arg and dsn.password not in ("postgres", dsn.user, dsn.dbname)):
            raise AssertionError("Invariant violation: password leaked into pg_dump argv.")

    return cmd, env


def _quote_ident(name: str) -> str:
    escaped = name.replace('"', '""')
    if re.match(r"^[a-z_][a-z0-9_]*$", name) and name not in {
        "user",
        "order",
        "group",
        "table",
        "select",
        "where",
        "from",
        "check",
        "unique",
        "primary",
        "references",
        "default",
        "constraint",
        "end",
        "start",
        "type",
    }:
        return name
    return f'"{escaped}"'


def _quote_fqn(fqn: str) -> str:
    schema, _, rel = fqn.partition(".")
    if not rel:
        return _quote_ident(schema)
    return f"{_quote_ident(schema)}.{_quote_ident(rel)}"


def extract_ddl_artifacts(
    source_dsn: ParsedDSN,
    excluded_tables: list[str] | None = None,
) -> dict[str, Any]:
    """Extract structured schema DDL from source PostgreSQL in read-only mode.

    Returns a dictionary of structured DDL components that `write_ddl_files` can render
    into `01_types.sql`, `02_tables.sql`, `03_fkeys.sql`, and `04_views.sql`, optionally
    filtering out degraded CHECK constraints.
    """
    excluded_set = set(excluded_tables or [])

    with psycopg.connect(source_dsn.raw_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on;")
            cur.execute("SET statement_timeout = '60s';")

            # 1. Custom schemas
            cur.execute(
                """
                SELECT nspname
                FROM pg_namespace
                WHERE nspname NOT IN ('public', 'pg_catalog', 'information_schema')
                  AND nspname NOT LIKE 'pg_toast%'
                  AND nspname NOT LIKE 'pg_temp%'
                ORDER BY nspname;
                """
            )
            schemas = [str(r[0]) for r in cur.fetchall()]

            # 2. Extensions (non-system)
            cur.execute(
                """
                SELECT e.extname, n.nspname
                FROM pg_extension e
                JOIN pg_namespace n ON n.oid = e.extnamespace
                WHERE e.extname NOT IN ('plpgsql')
                ORDER BY e.extname;
                """
            )
            extensions = [(str(r[0]), str(r[1])) for r in cur.fetchall()]

            # 3. Enums
            cur.execute(
                """
                SELECT n.nspname, t.typname, e.enumlabel
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                JOIN pg_enum e ON e.enumtypid = t.oid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                ORDER BY n.nspname, t.typname, e.enumsortorder;
                """
            )
            enums: dict[str, list[str]] = {}
            for ns, typ, lbl in cur.fetchall():
                enums.setdefault(f"{ns}.{typ}", []).append(str(lbl))

            # 4. Non-trigger user functions (in case CHECK or VIEW uses them)
            cur.execute(
                """
                SELECT pg_get_functiondef(p.oid)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                JOIN pg_type rt ON rt.oid = p.prorettype
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND rt.typname <> 'trigger'
                  AND p.prokind = 'f'
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_depend d
                      WHERE d.objid = p.oid AND d.deptype = 'e'
                  )
                ORDER BY n.nspname, p.proname;
                """
            )
            functions = [sanitize_ddl(str(r[0])).strip() for r in cur.fetchall() if r[0]]

            # 5. Base tables
            cur.execute(
                """
                SELECT n.nspname, c.relname, c.oid
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
            tables_raw = cur.fetchall()

            tables_ddl: list[dict[str, Any]] = []
            fkeys_ddl: list[str] = []

            for ns, rel, oid in tables_raw:
                fqn = f"{ns}.{rel}"
                if fqn in excluded_set:
                    continue

                # Columns
                cur.execute(
                    """
                    SELECT a.attname,
                           format_type(a.atttypid, a.atttypmod) AS col_type,
                           a.attnotnull,
                           a.attidentity,
                           pg_get_expr(ad.adbin, ad.adrelid) AS default_expr
                    FROM pg_attribute a
                    LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                    WHERE a.attrelid = %s
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY a.attnum;
                    """,
                    (oid,),
                )
                col_defs: list[str] = []
                sequences_pre: list[str] = []
                for attname, col_type, attnotnull, attidentity, default_expr in cur.fetchall():
                    col_str = f"    {_quote_ident(str(attname))} {col_type}"
                    if attidentity == "a":
                        col_str += " GENERATED ALWAYS AS IDENTITY"
                    elif attidentity == "d":
                        col_str += " GENERATED BY DEFAULT AS IDENTITY"
                    elif default_expr is not None:
                        def_s = str(default_expr)
                        if "nextval(" in def_s.lower():
                            m_seq = re.search(r"nextval\('([^']+)'", def_s, re.IGNORECASE)
                            if m_seq:
                                seq_name = m_seq.group(1)
                                sequences_pre.append(f"CREATE SEQUENCE IF NOT EXISTS {seq_name};")
                            col_str += f" DEFAULT {def_s}"
                        elif any(
                            safe_fn in def_s.lower()
                            for safe_fn in ("gen_random_uuid()", "uuid_generate_v4()", "now()", "current_timestamp", "current_date", "'{}'::jsonb", "'{}'::json", "array[]")
                        ) or def_s.lower() in ("true", "false") or re.match(r"^-?\d+(\.\d+)?$", def_s):
                            col_str += f" DEFAULT {def_s}"
                    if attnotnull:
                        col_str += " NOT NULL"
                    col_defs.append(col_str)

                # Table constraints (PK, UNIQUE, CHECK, FK)
                cur.execute(
                    """
                    SELECT con.conname,
                           con.contype,
                           pg_get_constraintdef(con.oid, true) AS condef,
                           ns_to.nspname AS to_schema,
                           cl_to.relname AS to_table
                    FROM pg_constraint con
                    LEFT JOIN pg_class cl_to ON cl_to.oid = con.confrelid
                    LEFT JOIN pg_namespace ns_to ON ns_to.oid = cl_to.relnamespace
                    WHERE con.conrelid = %s
                    ORDER BY con.contype, con.conname;
                    """,
                    (oid,),
                )
                pk_constraints: list[str] = []
                uq_constraints: list[str] = []
                chk_constraints: list[dict[str, str]] = []
                uq_constraint_names: set[str] = set()
                pk_constraint_names: set[str] = set()

                for conname, contype, condef, to_schema, to_table in cur.fetchall():
                    cname = str(conname)
                    cdef = str(condef)
                    ctype = str(contype)
                    if ctype == "p":
                        pk_constraint_names.add(cname)
                        pk_constraints.append(f"    CONSTRAINT {_quote_ident(cname)} {cdef}")
                    elif ctype == "u":
                        uq_constraint_names.add(cname)
                        uq_constraints.append(f"    CONSTRAINT {_quote_ident(cname)} {cdef}")
                    elif ctype == "c":
                        chk_constraints.append({"name": cname, "def": cdef})
                    elif ctype == "f":
                        to_fqn = f"{to_schema}.{to_table}" if to_schema and to_table else ""
                        if to_fqn not in excluded_set:
                            fkeys_ddl.append(
                                f"ALTER TABLE ONLY {_quote_fqn(fqn)} "
                                f"ADD CONSTRAINT {_quote_ident(cname)} {cdef};"
                            )

                # Indexes (including partial unique indexes not backing a constraint)
                cur.execute(
                    """
                    SELECT c.relname,
                           pg_get_indexdef(i.indexrelid) AS indexdef
                    FROM pg_index i
                    JOIN pg_class c ON c.oid = i.indexrelid
                    WHERE i.indrelid = %s
                      AND NOT i.indisprimary
                    ORDER BY c.relname;
                    """,
                    (oid,),
                )
                indexes: list[str] = []
                for idx_name, idx_def in cur.fetchall():
                    if str(idx_name) in uq_constraint_names or str(idx_name) in pk_constraint_names:
                        continue
                    indexes.append(f"{idx_def};")

                all_inline = col_defs + pk_constraints + uq_constraints
                create_tbl_sql = (
                    f"CREATE TABLE {_quote_fqn(fqn)} (\n"
                    + ",\n".join(all_inline)
                    + "\n);"
                )
                tables_ddl.append(
                    {
                        "table": fqn,
                        "sequences": sequences_pre,
                        "create_sql": create_tbl_sql,
                        "checks": chk_constraints,
                        "indexes": indexes,
                    }
                )

            # 6. Views (topologically sorted by view-to-view dependency)
            cur.execute(
                """
                SELECT c.oid,
                       n.nspname,
                       c.relname,
                       pg_get_viewdef(c.oid, true) AS viewdef
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
            view_rows = cur.fetchall()
            view_by_oid: dict[int, tuple[str, str]] = {}
            for v_oid, v_ns, v_rel, v_def in view_rows:
                v_fqn = f"{v_ns}.{v_rel}"
                v_sql = f"CREATE OR REPLACE VIEW {_quote_fqn(v_fqn)} AS\n{str(v_def).rstrip().rstrip(';')};"
                view_by_oid[int(v_oid)] = (v_fqn, v_sql)

            # View dependencies
            cur.execute(
                """
                SELECT DISTINCT rw.ev_class AS view_oid,
                                d.refobjid AS dep_view_oid
                FROM pg_rewrite rw
                JOIN pg_depend d ON d.objid = rw.oid
                WHERE d.refobjid <> rw.ev_class;
                """
            )
            view_deps: dict[int, set[int]] = {oid: set() for oid in view_by_oid}
            for v_oid, dep_oid in cur.fetchall():
                v_i, d_i = int(v_oid), int(dep_oid)
                if v_i in view_by_oid and d_i in view_by_oid:
                    view_deps[v_i].add(d_i)

            ordered_views: list[str] = []
            visited_views: set[int] = set()

            def _visit_view(v_oid: int) -> None:
                if v_oid in visited_views:
                    return
                visited_views.add(v_oid)
                for dep in sorted(view_deps.get(v_oid, ())):
                    _visit_view(dep)
                ordered_views.append(view_by_oid[v_oid][1])

            for v_oid in sorted(view_by_oid):
                _visit_view(v_oid)

    return {
        "schemas": schemas,
        "extensions": extensions,
        "enums": enums,
        "functions": functions,
        "tables": tables_ddl,
        "fkeys": fkeys_ddl,
        "views": ordered_views,
    }


def write_ddl_files(
    ddl_dir: Path,
    ddl_artifacts: dict[str, Any],
    *,
    degraded_checks: set[tuple[str, str]] | None = None,
) -> list[str]:
    """Write ddl/01_types.sql, 02_tables.sql, 03_fkeys.sql, 04_views.sql without degraded CHECKs."""
    ddl_dir.mkdir(parents=True, exist_ok=True)
    degraded_set = degraded_checks or set()

    # 01_types.sql
    types_lines: list[str] = [
        "-- Auto-generated by lab clone (01_types.sql)",
        "SET client_encoding = 'UTF8';",
        "SET standard_conforming_strings = on;",
        "SET check_function_bodies = false;",
        "SET client_min_messages = warning;",
        "",
    ]
    for schema_name in ddl_artifacts.get("schemas", []):
        types_lines.append(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema_name)};")
    for ext_name, ext_schema in ddl_artifacts.get("extensions", []):
        types_lines.append(
            f"CREATE EXTENSION IF NOT EXISTS {_quote_ident(ext_name)} WITH SCHEMA {_quote_ident(ext_schema)};"
        )
    for enum_fqn, labels in ddl_artifacts.get("enums", {}).items():
        escaped_labels = ", ".join("'" + lbl.replace("'", "''") + "'" for lbl in labels)
        types_lines.append(f"CREATE TYPE {_quote_fqn(enum_fqn)} AS ENUM ({escaped_labels});")
    for fn_sql in ddl_artifacts.get("functions", []):
        if fn_sql:
            types_lines.append(fn_sql)

    (ddl_dir / "01_types.sql").write_text(
        sanitize_ddl("\n\n".join(types_lines)),
        encoding="utf-8",
    )

    # 02_tables.sql
    tables_lines: list[str] = ["-- Auto-generated by lab clone (02_tables.sql)"]
    for tbl in ddl_artifacts.get("tables", []):
        fqn = tbl["table"]
        for seq_sql in tbl.get("sequences", []):
            tables_lines.append(seq_sql)
        tables_lines.append(tbl["create_sql"])
        for chk in tbl.get("checks", []):
            cname = chk["name"]
            if (fqn, cname) in degraded_set:
                continue
            tables_lines.append(
                f"ALTER TABLE {_quote_fqn(fqn)} ADD CONSTRAINT {_quote_ident(cname)} {chk['def']};"
            )
        for idx_sql in tbl.get("indexes", []):
            tables_lines.append(idx_sql)

    (ddl_dir / "02_tables.sql").write_text(
        sanitize_ddl("\n\n".join(tables_lines)),
        encoding="utf-8",
    )

    # 03_fkeys.sql
    fkeys_lines: list[str] = ["-- Auto-generated by lab clone (03_fkeys.sql)"]
    fkeys_lines.extend(ddl_artifacts.get("fkeys", []))
    (ddl_dir / "03_fkeys.sql").write_text(
        sanitize_ddl("\n\n".join(fkeys_lines)),
        encoding="utf-8",
    )

    # 04_views.sql
    views_lines: list[str] = ["-- Auto-generated by lab clone (04_views.sql)"]
    views_lines.extend(ddl_artifacts.get("views", []))
    (ddl_dir / "04_views.sql").write_text(
        sanitize_ddl("\n\n".join(views_lines)),
        encoding="utf-8",
    )

    return ["ddl/01_types.sql", "ddl/02_tables.sql", "ddl/03_fkeys.sql", "ddl/04_views.sql"]
