"""`lab import`: one command from a `lab clone` dataset to a compiled stack and a T0 snapshot."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from lab.common.config import REPO_ROOT, LabConfig, load_config, require_token
from lab.common.errors import LabError
from lab.common.gateway import Gateway
from lab.common.pg import Psql
from lab.common.runlog import DEFAULT_ROOT, RunDir
from lab.common.snapshot import snapshot_catalog, snapshot_target
from lab.importer import register
from lab.importer.dataset import Dataset, load_dataset
from lab.importer.target import count_foreign_key_columns, ensure_droppable, ensure_ro_role, prepare_target

SNAPSHOTS_DIR = REPO_ROOT / "snapshots"
RUNS_DIR = DEFAULT_ROOT
_RO_PASSWORD_ENV = "LAB_RO_PASSWORD"


def execute(
    admin: Gateway,
    psql: Psql,
    cfg: LabConfig,
    dataset: Dataset,
    ro_password: str,
    *,
    drop: bool,
    resnapshot: bool,
    snapshots_dir: Path,
) -> dict[str, Any]:
    """Run every stage in the order upstream requires and return the import.json report."""
    db = dataset.db_name
    _refuse_to_overwrite_t0(dataset, snapshots_dir, allowed=drop or resnapshot)

    print("Waiting for any running compilation pass...", flush=True)
    register.wait_until_idle(admin)

    others = register.connections_with_other_ro_password(psql, cfg.catalog_db, ro_password)
    if others:
        raise LabError(
            f"{_RO_PASSWORD_ENV} differs from the password stored for {', '.join(others)}. The role is shared "
            "across the cluster, so changing it would silently drop those databases from compilation."
        )

    if drop:
        # Check first: refusing after the delete would cost an unrelated database its connection.
        ensure_droppable(psql, dataset)
        register.ensure_no_ingest_running(cfg, db)
        register.remove_connection(admin, psql, cfg.catalog_db, db)
    has_connection = register.connection_registered(admin, db)

    print(f"Preparing target database {db!r}...", flush=True)
    load = prepare_target(psql, dataset, drop=drop)
    if has_connection and load.action == "loaded":
        # The catalog describes the database that disappeared; its Terms would be snapshotted as T0.
        raise LabError(
            f"connection {db!r} exists but its database had to be recreated, so the catalog is stale. "
            "Re-run with --drop to remove the connection and start clean."
        )
    ensure_ro_role(psql, dataset, ro_password)

    result = register.register_and_compile(admin, psql, cfg, db, ro_password)

    print("Saving the T0 snapshots...", flush=True)
    register.wait_until_idle(admin)  # A pass writing during the dump would mix its rows into T0.
    snapshots = _write_t0(psql, cfg, dataset, snapshots_dir)

    return {
        "dataset": dataset.dataset_id,
        "database": db,
        "action": load.action,
        "load_seconds": load.seconds,
        "row_count_check": load.row_count_check,
        "row_counts": load.row_counts,
        "resumed_from": result.resumed_from,
        "ingest": asdict(result.ingest) if result.ingest else None,
        "compile_run_id": result.compile_run_id,
        "compile_seconds": result.compile_seconds,
        "terms": result.terms,
        "tables_total": len(result.catalog_tables),
        "tables_without_terms": result.tables_without_terms,
        "tables_missing_from_catalog": sorted(set(dataset.tables) - set(result.catalog_tables)),
        "foreign_key_columns": {
            "target": count_foreign_key_columns(psql, db),
            "catalog": register.catalog_foreign_key_columns(psql, cfg.catalog_db, db),
        },
        "snapshots": snapshots,
        "upstream_commit": cfg.upstream_commit,
    }


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", required=True, help="Dataset folder name under datasets/.")
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Remove the connection and rebuild the database. Only databases marked for this dataset can be dropped.",
    )
    parser.add_argument(
        "--resnapshot", action="store_true", help="Replace an existing T0 with the catalog's current state."
    )
    parser.add_argument("--freeze", type=Path, default=None, help="Freeze file (default: config/freeze.yaml).")


def run(args: argparse.Namespace) -> int:
    ro_password = ""
    try:
        dataset = load_dataset(args.dataset)
        cfg = load_config(args.freeze)
        token = require_token("admin")
        ro_password = os.environ.get(_RO_PASSWORD_ENV, "")
        if not ro_password:
            raise LabError(f"{_RO_PASSWORD_ENV} is not set; use the same value `lab clone` used")
        report = execute(
            Gateway(cfg.gateway_url, token),
            Psql(cfg.containers.postgres, cfg.pg_superuser),
            cfg,
            dataset,
            ro_password,
            drop=args.drop,
            resnapshot=args.resnapshot,
            snapshots_dir=SNAPSHOTS_DIR,
        )
    except (LabError, httpx.HTTPError) as exc:
        message = str(exc).replace(ro_password, "***") if ro_password else str(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 1

    path = RunDir.create(root=RUNS_DIR).write_json("import.json", report)
    _print_coverage(report)
    print(f"Report: {path}")
    return 0


def _t0_paths(dataset: Dataset, snapshots_dir: Path) -> tuple[Path, Path]:
    name = dataset.root.name
    return snapshots_dir / f"T0_{name}.dump", snapshots_dir / f"T0_{name}.target.dump"


def _refuse_to_overwrite_t0(dataset: Dataset, snapshots_dir: Path, *, allowed: bool) -> None:
    existing = [path.name for path in _t0_paths(dataset, snapshots_dir) if path.exists()]
    if existing and not allowed:
        # After Step 5's mutations the catalog is no longer T0, and that baseline cannot be rebuilt cheaply.
        raise LabError(
            f"{', '.join(existing)} already exists in {snapshots_dir}. Use `lab import --resnapshot` to replace it "
            "with the catalog's current state, or --drop to rebuild everything."
        )


def _write_t0(psql: Psql, cfg: LabConfig, dataset: Dataset, snapshots_dir: Path) -> dict[str, dict[str, str]]:
    """Write both dumps aside, then swap them in, so a failure never leaves a mismatched pair."""
    catalog_dest, target_dest = _t0_paths(dataset, snapshots_dir)
    incoming = snapshots_dir / f".incoming_{dataset.root.name}"
    shutil.rmtree(incoming, ignore_errors=True)
    incoming.mkdir(parents=True)
    try:
        catalog_meta = snapshot_catalog(psql, cfg.catalog_db, incoming / catalog_dest.name)
        snapshot_target(psql, dataset.db_name, incoming / target_dest.name)
        target_sidecar = (incoming / target_dest.name).with_suffix(".json")
        target_sha = json.loads(target_sidecar.read_text(encoding="utf-8"))["sha256"]
        for path in incoming.iterdir():
            path.replace(snapshots_dir / path.name)
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
    return {
        "catalog": {"path": catalog_dest.name, "sha256": catalog_meta.sha256},
        "target": {"path": target_dest.name, "sha256": target_sha},
    }


def _print_coverage(report: dict[str, Any]) -> None:
    missing = report["tables_without_terms"]
    total = report["tables_total"]
    print(f"{report['terms']} Term(s) across {total - len(missing)}/{total} tables.")
    if missing:
        print(
            f"Tables without a Term: {', '.join(missing)}\n"
            "Bridge tables are expected here. For any other table, look for per-table errors in the ingestion "
            "log, because upstream logs them and still marks the pass as succeeded."
        )
    fks = report["foreign_key_columns"]
    print(f"Foreign key columns: {fks['catalog']} in the catalog, {fks['target']} in the database.")
    if fks["catalog"] != fks["target"]:
        print("WARNING: the catalog does not match the database's foreign keys; see docs/FINDINGS.md F6.")
    if report["tables_missing_from_catalog"]:
        print(f"WARNING: in the manifest but not the catalog: {', '.join(report['tables_missing_from_catalog'])}")
