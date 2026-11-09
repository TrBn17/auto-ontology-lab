"""Target database provisioning, safe --drop guard (§1.4), COPY loader, and lab_ro setup."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from typing import Any
import psycopg
from psycopg import sql

from lab.ddl import _quote_fqn, _quote_ident, _split_sql_statements
from lab.dsn import ParsedDSN


LAB_DB_COMMENT_PREFIX = "lab_synth:"


def _maintenance_dsn(target_dsn: ParsedDSN, maint_db: str = "postgres") -> str:
    """Build a connection string to the maintenance 'postgres' database on the same server."""
    conn_params = [
        f"host={target_dsn.host}",
        f"port={target_dsn.port}",
        f"user={target_dsn.user}",
        f"dbname={maint_db}",
    ]
    if target_dsn.password is not None:
        conn_params.append(f"password={target_dsn.password}")
    return " ".join(conn_params)


def ensure_target_database(
    target_dsn: ParsedDSN,
    dataset_id: str,
    *,
    drop: bool = False,
) -> None:
    """Create or safely recreate the target database with the 'lab_synth:<dataset_id>' comment (§1.4)."""
    dbname = target_dsn.dbname
    maint_conn_str = _maintenance_dsn(target_dsn, "postgres")

    with psycopg.connect(maint_conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.oid, pg_catalog.shobj_description(d.oid, 'pg_database')
                FROM pg_database d
                WHERE d.datname = %s;
                """,
                (dbname,),
            )
            row = cur.fetchone()

            if row is not None:
                _, db_comment = row
                comment_str = str(db_comment or "")
                if drop:
                    if not comment_str.startswith(LAB_DB_COMMENT_PREFIX):
                        raise PermissionError(
                            f"Refusing to --drop database {dbname!r} (§1.4): database comment is {comment_str!r}, "
                            f"expected prefix {LAB_DB_COMMENT_PREFIX!r}."
                        )
                    cur.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE);").format(
                            sql.Identifier(dbname)
                        )
                    )
                    cur.execute(
                        sql.SQL("CREATE DATABASE {};").format(sql.Identifier(dbname))
                    )
                else:
                    # If database already exists without --drop, verify it is empty or carries the lab comment
                    if not comment_str.startswith(LAB_DB_COMMENT_PREFIX):
                        with psycopg.connect(target_dsn.raw_dsn) as tgt_conn:
                            with tgt_conn.cursor() as tgt_cur:
                                tgt_cur.execute(
                                    """
                                    SELECT count(*)
                                    FROM information_schema.tables
                                    WHERE table_schema NOT IN ('pg_catalog', 'information_schema');
                                    """
                                )
                                existing_tables = int(tgt_cur.fetchone()[0] or 0)
                                if existing_tables > 0:
                                    raise PermissionError(
                                        f"Target database {dbname!r} already exists with {existing_tables} tables "
                                        f"and lacks the {LAB_DB_COMMENT_PREFIX!r} marker (§1.4)."
                                    )
            else:
                cur.execute(
                    sql.SQL("CREATE DATABASE {};").format(sql.Identifier(dbname))
                )

            marker_comment = f"{LAB_DB_COMMENT_PREFIX}{dataset_id}"
            cur.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {};").format(
                    sql.Identifier(dbname),
                    sql.Literal(marker_comment),
                )
            )


def _execute_sql_file(cur: psycopg.Cursor[Any], sql_path: Path) -> None:
    if not sql_path.is_file():
        return
    raw_sql = sql_path.read_text(encoding="utf-8")
    for stmt in _split_sql_statements(raw_sql):
        cur.execute(stmt)


def load_dataset_to_target(
    target_dsn: ParsedDSN,
    dataset_dir: Path,
    *,
    drop: bool = False,
) -> None:
    """Create/prepare the target database, load DDL + CSVs + FKs + views, reset sequences, and configure lab_ro."""
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest.json not found in {dataset_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset_id = str(manifest.get("dataset") or dataset_dir.name)
    profile_path = dataset_dir / "profile.json"
    profile = (
        json.loads(profile_path.read_text(encoding="utf-8"))
        if profile_path.is_file()
        else {}
    )

    ensure_target_database(target_dsn, dataset_id, drop=drop)

    ddl_dir = dataset_dir / "ddl"
    data_dir = dataset_dir / "data"

    with psycopg.connect(target_dsn.raw_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # 1. Types & Tables
            _execute_sql_file(cur, ddl_dir / "01_types.sql")
            _execute_sql_file(cur, ddl_dir / "02_tables.sql")

            # 2. Load CSVs via COPY in dependency order
            entities = manifest.get("entities") or []
            tables_meta = profile.get("tables") or {}
            table_list = [e["table"] for e in entities]

            for table_fqn in table_list:
                csv_path = data_dir / f"{table_fqn}.csv"
                if not csv_path.is_file():
                    continue
                csv_bytes = csv_path.read_bytes()
                lines = csv_bytes.splitlines()
                if len(lines) <= 1:
                    continue
                header_cols = lines[0].decode("utf-8").split(",")
                cols_sql = ", ".join(_quote_ident(c) for c in header_cols)
                copy_stmt = (
                    f"COPY {_quote_fqn(table_fqn)} ({cols_sql}) "
                    "FROM STDIN WITH (FORMAT csv, HEADER true, NULL '\\N')"
                )
                with cur.copy(copy_stmt) as copy:
                    copy.write(csv_bytes)

            # 3. Foreign keys & Views
            _execute_sql_file(cur, ddl_dir / "03_fkeys.sql")
            _execute_sql_file(cur, ddl_dir / "04_views.sql")

            # 4. Reset sequences for identity/serial columns
            for table_fqn, t_meta in tables_meta.items():
                cols_meta = t_meta.get("columns") or {}
                for col_name, c_meta in cols_meta.items():
                    if c_meta.get("is_identity"):
                        cur.execute(
                            f"SELECT setval(pg_get_serial_sequence('{table_fqn}', '{col_name}'), "
                            f"COALESCE((SELECT MAX({_quote_ident(col_name)}) FROM {_quote_fqn(table_fqn)}), 1), true);"
                        )

            # 5. Provision read-only role `lab_ro` (§1.4, A8) using LAB_RO_PASSWORD from environment
            ro_password = os.environ.get("LAB_RO_PASSWORD") or "lab_ro"
            try:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'lab_ro';")
                if cur.fetchone() is None:
                    cur.execute(
                        sql.SQL(
                            "CREATE ROLE lab_ro LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;"
                        ).format(sql.Literal(ro_password))
                    )
                else:
                    cur.execute(
                        sql.SQL(
                            "ALTER ROLE lab_ro WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;"
                        ).format(sql.Literal(ro_password))
                    )
            except Exception as exc:
                masked = str(exc).replace(ro_password, "***") if ro_password else str(exc)
                raise RuntimeError(f"Failed to configure role lab_ro: {masked}") from None
            cur.execute("ALTER ROLE lab_ro SET default_transaction_read_only = on;")
            cur.execute("ALTER ROLE lab_ro SET statement_timeout = '30s';")
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO lab_ro;").format(
                    sql.Identifier(target_dsn.dbname)
                )
            )

            schemas = {"public"}
            for table_fqn in table_list:
                if "." in table_fqn:
                    schemas.add(table_fqn.split(".", 1)[0])
            for v_fqn in profile.get("views") or []:
                if "." in v_fqn:
                    schemas.add(v_fqn.split(".", 1)[0])

            for s_name in sorted(schemas):
                s_ident = sql.Identifier(s_name)
                cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM lab_ro;").format(s_ident))
                cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO lab_ro;").format(s_ident))
                cur.execute(
                    sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM lab_ro;").format(s_ident)
                )
                cur.execute(
                    sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO lab_ro;").format(s_ident)
                )
                cur.execute(
                    sql.SQL("GRANT SELECT ON ALL SEQUENCES IN SCHEMA {} TO lab_ro;").format(s_ident)
                )

        conn.commit()

    # 6. Run ANALYZE in autocommit mode
    with psycopg.connect(target_dsn.raw_dsn, autocommit=True) as conn:
        conn.execute("ANALYZE;")
