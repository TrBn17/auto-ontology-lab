#!/usr/bin/env python3
"""Import a synthetic dataset from datasets/<project-name>/ into Auto-Ontology.

Usage examples:
    python scripts/import_dataset.py --list
    python scripts/import_dataset.py --dataset language-learning-sandbox
    python scripts/import_dataset.py --dataset language-learning-sandbox --db-name language_sandbox --drop --compile
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASETS_DIR = REPO_ROOT / "datasets"
AUTO_ONTOLOGY_ENV = REPO_ROOT / "auto-ontology" / ".env"


def parse_env_file(env_path: Path) -> dict[str, str]:
    """Read key=value pairs from a .env file without external dependencies."""
    env: dict[str, str] = {}
    if not env_path.is_file():
        return env
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def ensure_clean_env(env_path: Path) -> bool:
    """Fix common .env.example placeholders that block UI connections or embeddings.

    Returns True if auto-ontology/.env was modified and containers were refreshed.
    """
    if not env_path.is_file():
        return False

    original = env_path.read_text(encoding="utf-8")
    updated = original

    # 1. Clear default wideworldimporters placeholder so catalog connections are used
    updated = re.sub(
        r"^CONNECTION_STRINGS=postgresql://user:password@localhost:5432/wideworldimporters\s*$",
        "CONNECTION_STRINGS=",
        updated,
        flags=re.M,
    )

    # 2. Propagate DEFAULT_MODELS_API_KEY to any role key still holding placeholder
    m = re.search(r"^DEFAULT_MODELS_API_KEY=(.+)$", updated, flags=re.M)
    api_key = m.group(1).strip() if m else ""
    if api_key and api_key != "nvapi-your-api-key-here":
        for role_key in ("REASONING_API_KEY", "NON_REASONING_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY"):
            updated = re.sub(
                rf"^{role_key}=nvapi-your-api-key-here\s*$",
                f"{role_key}={api_key}",
                updated,
                flags=re.M,
            )

    # 3. Use stable nvidia/nemotron-3-embed-1b instead of 3.5 which 500s on batch embed
    updated = updated.replace(
        "EMBED_MODEL=nvidia/nemotron-3.5-embed-1b",
        "EMBED_MODEL=nvidia/nemotron-3-embed-1b",
    )

    if updated != original:
        print("[0/4] Detected default placeholders in auto-ontology/.env; auto-fixing and refreshing services...")
        env_path.write_text(updated, encoding="utf-8")
        compose_file = REPO_ROOT / "auto-ontology" / "docker-compose.yml"
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "up",
                "-d",
                "auto-ontology",
                "ingestion-service",
            ],
            check=True,
        )
        return True
    return False


def resolve_sql_load_order(dataset_dir: Path, auto_generate: bool = False) -> list[Path]:
    """Resolve the ordered list of SQL files to load for a dataset directory.

    Supports both:
    1. Standard flat contract:
       - schema.sql
       - seed.sql
    2. Multi-file / generator layout (e.g. hahlex-edu):
       - generate.py (auto-executed if seed.sql is missing and auto_generate=True)
       - manifest.json["load_order"] or schema/{roles,schema,reference_data,schema_migrations}.sql + seed.sql
    """
    seed_file = dataset_dir / "seed.sql"
    generator_file = dataset_dir / "generate.py"
    if not seed_file.is_file() and generator_file.is_file() and auto_generate:
        print(f"      'seed.sql' not found in '{dataset_dir.name}'; running generate.py...")
        subprocess.run([sys.executable, str(generator_file)], cwd=str(dataset_dir), check=True)

    manifest_file = dataset_dir / "manifest.json"
    if manifest_file.is_file():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            load_order = manifest.get("load_order")
            if isinstance(load_order, list) and load_order:
                paths = [dataset_dir / rel for rel in load_order]
                if all(p.is_file() for p in paths):
                    return paths
        except Exception:
            pass

    flat_schema = dataset_dir / "schema.sql"
    if flat_schema.is_file() and seed_file.is_file():
        return [flat_schema, seed_file]

    schema_subdir = dataset_dir / "schema"
    if schema_subdir.is_dir():
        ordered_candidates = [
            schema_subdir / "roles.sql",
            schema_subdir / "schema.sql",
            schema_subdir / "reference_data.sql",
            schema_subdir / "schema_migrations.sql",
        ]
        sql_files = [p for p in ordered_candidates if p.is_file()]
        if seed_file.is_file():
            sql_files.append(seed_file)
        if (schema_subdir / "schema.sql").is_file() and (seed_file.is_file() or generator_file.is_file()):
            return sql_files

    return []


def discover_datasets() -> list[Path]:
    """Return valid dataset directories under datasets/."""
    if not DATASETS_DIR.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(DATASETS_DIR.iterdir()):
        if not child.is_dir():
            continue
        has_flat = (child / "schema.sql").is_file() and (
            (child / "seed.sql").is_file() or (child / "generate.py").is_file()
        )
        has_subdir = (child / "schema" / "schema.sql").is_file() and (
            (child / "seed.sql").is_file() or (child / "generate.py").is_file()
        )
        has_manifest = bool(resolve_sql_load_order(child, auto_generate=False))
        if has_flat or has_subdir or has_manifest:
            found.append(child)
    return found


def sanitize_sql_for_psql(sql_text: str) -> str:
    """Strip pg_dump \\restrict / \\unrestrict meta-commands that can fail across psql versions."""
    lines = [
        line
        for line in sql_text.splitlines()
        if not line.startswith("\\restrict ") and not line.startswith("\\unrestrict ")
    ]
    return "\n".join(lines) + "\n"


def detect_custom_schemas(schema_sql: str) -> list[str]:
    """Extract schema names created by CREATE SCHEMA statements in schema.sql."""
    pattern = re.compile(
        r"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][a-zA-Z0-9_]*)",
        re.IGNORECASE,
    )
    schemas: list[str] = []
    for match in pattern.finditer(schema_sql):
        name = match.group(1)
        if name.lower() != "public" and name not in schemas:
            schemas.append(name)
    return schemas


def normalize_db_name(raw: str) -> str:
    """Convert a folder/project name into a safe PostgreSQL database identifier."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw.strip().lower()).strip("_")
    if not cleaned or not re.match(r"^[a-z_][a-z0-9_]*$", cleaned):
        raise ValueError(f"Invalid database name: {raw!r}")
    return cleaned


def run_psql(
    postgres_container: str,
    postgres_user: str,
    database: str,
    *,
    command: str | None = None,
    stdin_sql: str | None = None,
    single_transaction: bool = False,
) -> None:
    """Run psql inside the target Postgres Docker container."""
    cmd = [
        "docker",
        "exec",
        "-i",
        postgres_container,
        "psql",
        "-U",
        postgres_user,
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-d",
        database,
    ]
    if single_transaction:
        cmd.append("-1")
    if command is not None:
        cmd.extend(["-c", command])
    proc = subprocess.run(
        cmd,
        input=stdin_sql,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        raise RuntimeError(f"psql failed with exit code {proc.returncode}")


def register_connection_in_auto_ontology(
    backend_container: str,
    db_name: str,
    postgres_user: str,
    postgres_password: str,
    *,
    compile_semantics: bool = False,
) -> dict:
    """Register or re-trigger ingestion for the database in Auto-Ontology."""
    py_code = """
import json, os, sys, urllib.request, urllib.error

db_name = sys.argv[1]
pg_user = sys.argv[2]
pg_pass = sys.argv[3]
do_compile = sys.argv[4] == "1"

base_url = "http://127.0.0.1:3001/api"
ingest_url = os.environ.get("INGESTION_SERVICE_URL", "http://ingestion-service:3002")
conn_obj = {
    "type": "postgresql",
    "host": "postgres",
    "port": "5432",
    "user": pg_user,
    "password": pg_pass,
    "database": db_name,
}

def request_json(url, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "x-auto-ontology-user-id": "system-import"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} on {method} {url}: {body}") from exc

_, existing = request_json(f"{base_url}/connections")
existing_names = {
    str(item.get("database_name") or "").lower()
    for item in (existing.get("data") or [])
}

if db_name.lower() not in existing_names:
    request_json(
        f"{base_url}/connections/test",
        method="POST",
        payload={"connection": conn_obj, "replacing": None},
    )
    request_json(
        f"{base_url}/connections",
        method="POST",
        payload={"connection": conn_obj},
    )
    action = "created"
else:
    request_json(
        f"{base_url}/connections/{db_name}",
        method="PUT",
        payload={"connection": conn_obj},
    )
    request_json(f"{ingest_url}/ingest", method="POST", payload=conn_obj)
    action = "updated & re-ingested"

if do_compile:
    import time, psycopg
    from auto_ontology.infra.postgres import get_postgres_connection_string
    with psycopg.connect(get_postgres_connection_string(), autocommit=True) as conn:
        for _ in range(60):
            try:
                row = conn.execute(
                    "SELECT count(*) FROM vdb.data_objects_layer WHERE database_name = %s",
                    (db_name,),
                ).fetchone()
                if row and row[0] > 0:
                    break
            except Exception:
                pass
            time.sleep(1)
        conn.execute(
            \"\"\"
            INSERT INTO frontend.configurations (key, value, updated_at)
            VALUES ('semantic_compilation_enabled', 'true', now())
            ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = now()
            \"\"\"
        )
    request_json(f"{ingest_url}/semantic/compile", method="POST", payload={})

print(json.dumps({"status": "ok", "action": action, "database": db_name}))
"""
    cmd = [
        "docker",
        "exec",
        backend_container,
        "python",
        "-c",
        py_code,
        db_name,
        postgres_user,
        postgres_password,
        "1" if compile_semantics else "0",
    ]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        raise RuntimeError("Failed to register connection in Auto-Ontology")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a dataset from datasets/<project-name> into Auto-Ontology."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available datasets under datasets/ and exit.",
    )
    parser.add_argument(
        "--dataset",
        help="Dataset folder name under datasets/ (or path to dataset directory).",
    )
    parser.add_argument(
        "--db-name",
        help="Target PostgreSQL database name (default: normalized dataset folder name).",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Drop and recreate the target database if it already exists.",
    )
    parser.add_argument(
        "--compile",
        dest="compile",
        action="store_true",
        default=True,
        help="Enable and trigger Semantic Compilation after registering the connection (default: enabled).",
    )
    parser.add_argument(
        "--no-compile",
        dest="compile",
        action="store_false",
        help="Only ingest the catalog and embeddings without triggering Semantic Compilation.",
    )
    parser.add_argument(
        "--postgres-container",
        default="postgres",
        help="Docker container name for PostgreSQL (default: postgres).",
    )
    parser.add_argument(
        "--backend-container",
        default="auto-ontology",
        help="Docker container name for Auto-Ontology backend (default: auto-ontology).",
    )
    args = parser.parse_args()

    datasets = discover_datasets()
    if args.list:
        if not datasets:
            print("No datasets found in datasets/.")
            return 0
        print("Available datasets:")
        for d in datasets:
            print(f"  - {d.name} ({d})")
        return 0

    if args.dataset:
        candidate = Path(args.dataset)
        if not candidate.is_dir():
            candidate = DATASETS_DIR / args.dataset
        if not candidate.is_dir():
            print(f"Error: dataset directory not found: {args.dataset}", file=sys.stderr)
            return 1
        dataset_dir = candidate.resolve()
    else:
        if len(datasets) == 1:
            dataset_dir = datasets[0]
        elif not datasets:
            print("Error: no datasets found under datasets/.", file=sys.stderr)
            return 1
        else:
            names = ", ".join(d.name for d in datasets)
            print(
                f"Error: multiple datasets found ({names}). Specify --dataset <name>.",
                file=sys.stderr,
            )
            return 1

    sql_load_files = resolve_sql_load_order(dataset_dir, auto_generate=True)
    validate_file = dataset_dir / "validate.sql"

    if not sql_load_files:
        print(
            f"Error: {dataset_dir} does not contain a valid SQL layout (expected schema.sql + seed.sql, or schema/ + generate.py).",
            file=sys.stderr,
        )
        return 1

    db_name = normalize_db_name(args.db_name or dataset_dir.name)
    ensure_clean_env(AUTO_ONTOLOGY_ENV)
    env_vars = parse_env_file(AUTO_ONTOLOGY_ENV)
    pg_user = env_vars.get("POSTGRES_USER", "postgres")
    pg_password = env_vars.get("POSTGRES_PASSWORD", "change-me")

    print(f"[1/4] Preparing database '{db_name}' in container '{args.postgres_container}'...")
    already_seeded = False
    if args.drop:
        run_psql(
            args.postgres_container,
            pg_user,
            "postgres",
            command=f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE);",
        )
        run_psql(
            args.postgres_container,
            pg_user,
            "postgres",
            command=f"CREATE DATABASE {db_name};",
        )
    else:
        check_cmd = [
            "docker",
            "exec",
            args.postgres_container,
            "psql",
            "-U",
            pg_user,
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'",
        ]
        exists = subprocess.run(check_cmd, text=True, capture_output=True).stdout.strip() == "1"
        if not exists:
            run_psql(
                args.postgres_container,
                pg_user,
                "postgres",
                command=f"CREATE DATABASE {db_name};",
            )
        else:
            tbl_cmd = [
                "docker",
                "exec",
                args.postgres_container,
                "psql",
                "-U",
                pg_user,
                "-d",
                db_name,
                "-tAc",
                "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema')",
            ]
            tbl_count = int(subprocess.run(tbl_cmd, text=True, capture_output=True).stdout.strip() or "0")
            if tbl_count > 0:
                already_seeded = True
                print(f"      Database '{db_name}' already exists with {tbl_count} relations (use --drop to recreate).")

    if not already_seeded:
        rel_names = ", ".join(str(p.relative_to(dataset_dir)).replace("\\", "/") for p in sql_load_files)
        print(f"[2/4] Importing SQL files ({rel_names}) from '{dataset_dir.name}'...")
        custom_schemas: list[str] = []
        for sql_path in sql_load_files:
            raw_sql = sql_path.read_text(encoding="utf-8")
            for s in detect_custom_schemas(raw_sql):
                if s not in custom_schemas:
                    custom_schemas.append(s)
            run_psql(
                args.postgres_container,
                pg_user,
                db_name,
                stdin_sql=sanitize_sql_for_psql(raw_sql),
                single_transaction=True,
            )

        if custom_schemas:
            search_path = ", ".join([*custom_schemas, "public"])
            print(f"      Setting database search_path to: {search_path}")
            run_psql(
                args.postgres_container,
                pg_user,
                db_name,
                command=f"ALTER DATABASE {db_name} SET search_path TO {search_path};",
            )

        if validate_file.is_file():
            print("[3/4] Running validate.sql checks...")
            run_psql(
                args.postgres_container,
                pg_user,
                db_name,
                stdin_sql=sanitize_sql_for_psql(validate_file.read_text(encoding="utf-8")),
            )
        else:
            print("[3/4] No validate.sql found, skipping validation.")
    else:
        print("[2/4] Skipping SQL import (already seeded).")
        print("[3/4] Skipping validate.sql (already seeded).")

    print(f"[4/4] Registering connection '{db_name}' in Auto-Ontology & triggering ingestion...")
    result = register_connection_in_auto_ontology(
        args.backend_container,
        db_name,
        pg_user,
        pg_password,
        compile_semantics=args.compile,
    )
    print(f"Done! Connection {result['action']} for database '{db_name}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
