"""Read and validate a dataset produced by `lab clone` (STEP1_CLONE_PLAN.md §1.2, §1.3)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab.common.config import REPO_ROOT
from lab.common.errors import LabError
from lab.writers import is_degraded_ack_valid

DATASETS_DIR = REPO_ROOT / "datasets"
_MARKER = ".lab-dataset"
_VALID_DB_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class Dataset:
    root: Path
    dataset_id: str
    db_name: str
    load_order: list[Path]
    schemas: list[str]
    tables: list[str]
    row_counts: dict[str, int] | None


def load_dataset(ref: str, datasets_dir: Path = DATASETS_DIR) -> Dataset:
    root = datasets_dir / ref
    if not root.is_dir():
        raise LabError(f"dataset directory {root} not found")
    if not (root / _MARKER).is_file():
        raise LabError(f"{root} has no {_MARKER} marker, so it is not a lab dataset; generate it with `lab clone`")
    manifest = _read_manifest(root)

    degraded = manifest.get("degraded") or []
    if degraded and not is_degraded_ack_valid(root, degraded):
        raise LabError(
            f"{root} has {len(degraded)} dropped CHECK constraint(s) with no valid acknowledgement; "
            "review them, then run `lab ack-degraded`"
        )

    tables = [str(entity["table"]) for entity in manifest.get("entities") or []]
    return Dataset(
        root=root,
        dataset_id=str(manifest.get("dataset") or ref),
        db_name=_normalize_db_name(ref),
        load_order=_resolve_load_order(root, manifest.get("load_order")),
        schemas=_custom_schemas(tables),
        tables=tables,
        row_counts=manifest.get("row_counts"),
    )


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest.json"
    if not path.is_file():
        raise LabError(f"{path} not found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LabError(f"{path} is not valid JSON: {exc}") from exc


def _resolve_load_order(root: Path, load_order: Any) -> list[Path]:
    if not load_order:
        raise LabError(f"{root / 'manifest.json'} has an empty load_order")
    resolved_root = root.resolve()
    files = []
    for rel in load_order:
        path = root / rel
        if not path.resolve().is_relative_to(resolved_root):
            raise LabError(f"load_order entry {rel!r} points outside the dataset")
        if not path.is_file():
            raise LabError(f"load_order entry {path} not found")
        files.append(path)
    return files


def _custom_schemas(tables: list[str]) -> list[str]:
    schemas: list[str] = []
    for table in tables:
        schema = table.split(".", 1)[0] if "." in table else "public"
        if schema != "public" and schema not in schemas:
            schemas.append(schema)
    return schemas


def _normalize_db_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw.strip().lower()).strip("_")
    if not _VALID_DB_NAME.match(cleaned):
        raise LabError(f"cannot derive a database name from {raw!r}")
    return cleaned
