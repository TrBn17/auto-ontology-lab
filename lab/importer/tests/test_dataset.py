from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lab.common.errors import LabError
from lab.importer.dataset import load_dataset
from lab.writers import acknowledge_degraded_checks

LOAD_ORDER = ["ddl/02_tables.sql", "data/_load.sql"]


def make_dataset(root: Path, name: str = "edu", **manifest_overrides: Any) -> Path:
    folder = root / name
    (folder / "ddl").mkdir(parents=True)
    (folder / "data").mkdir()
    for rel in LOAD_ORDER:
        (folder / rel).write_text("SELECT 1;\n", encoding="utf-8")
    (folder / ".lab-dataset").write_text(json.dumps({"dataset_id": name}), encoding="utf-8")
    manifest: dict[str, Any] = {
        "dataset": name,
        "load_order": LOAD_ORDER,
        "row_counts": {"public.a": 3},
        "entities": [{"table": "public.a"}],
        "degraded": [],
    }
    manifest.update(manifest_overrides)
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def test_reads_step1_manifest(tmp_path: Path) -> None:
    folder = make_dataset(tmp_path)
    dataset = load_dataset("edu", tmp_path)
    assert dataset.root == folder
    assert dataset.dataset_id == "edu"
    assert dataset.load_order == [folder / rel for rel in LOAD_ORDER]
    assert dataset.tables == ["public.a"]
    assert dataset.row_counts == {"public.a": 3}


def test_db_name_is_normalised(tmp_path: Path) -> None:
    make_dataset(tmp_path, "hahlex-edu")
    dataset = load_dataset("hahlex-edu", tmp_path)
    assert dataset.db_name == "hahlex_edu"
    assert dataset.dataset_id == "hahlex-edu"


def test_schemas_exclude_public(tmp_path: Path) -> None:
    make_dataset(tmp_path, entities=[{"table": "public.a"}, {"table": "crm.b"}])
    assert load_dataset("edu", tmp_path).schemas == ["crm"]


def test_refuses_missing_manifest(tmp_path: Path) -> None:
    folder = make_dataset(tmp_path)
    (folder / "manifest.json").unlink()
    with pytest.raises(LabError, match="manifest.json"):
        load_dataset("edu", tmp_path)


def test_refuses_unknown_dataset(tmp_path: Path) -> None:
    with pytest.raises(LabError, match="nope"):
        load_dataset("nope", tmp_path)


def test_refuses_seed_sql_layout(tmp_path: Path) -> None:
    folder = make_dataset(tmp_path)
    (folder / ".lab-dataset").unlink()
    with pytest.raises(LabError, match="lab clone"):
        load_dataset("edu", tmp_path)


def test_refuses_missing_load_order_file(tmp_path: Path) -> None:
    folder = make_dataset(tmp_path)
    (folder / "data" / "_load.sql").unlink()
    with pytest.raises(LabError, match="_load.sql"):
        load_dataset("edu", tmp_path)


def test_refuses_empty_load_order(tmp_path: Path) -> None:
    make_dataset(tmp_path, load_order=[])
    with pytest.raises(LabError, match="load_order"):
        load_dataset("edu", tmp_path)


def test_refuses_load_order_escaping_dataset(tmp_path: Path) -> None:
    (tmp_path / "x.sql").write_text("SELECT 1;\n", encoding="utf-8")
    make_dataset(tmp_path, load_order=["../x.sql"])
    with pytest.raises(LabError, match="outside"):
        load_dataset("edu", tmp_path)


def test_refuses_degraded_without_ack(tmp_path: Path) -> None:
    make_dataset(tmp_path, degraded=[{"table": "public.a", "constraint": "chk"}])
    with pytest.raises(LabError, match="ack-degraded"):
        load_dataset("edu", tmp_path)


def test_accepts_degraded_with_ack(tmp_path: Path) -> None:
    folder = make_dataset(tmp_path, degraded=[{"table": "public.a", "constraint": "chk"}])
    acknowledge_degraded_checks(folder, "reviewer")
    assert load_dataset("edu", tmp_path).dataset_id == "edu"


def test_refuses_ack_of_a_different_degraded_list(tmp_path: Path) -> None:
    folder = make_dataset(tmp_path, degraded=[{"table": "public.a", "constraint": "chk"}])
    acknowledge_degraded_checks(folder, "reviewer")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["degraded"].append({"table": "public.a", "constraint": "chk2"})
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(LabError, match="ack-degraded"):
        load_dataset("edu", tmp_path)


def test_row_counts_none_when_absent(tmp_path: Path) -> None:
    folder = make_dataset(tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    del manifest["row_counts"]
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert load_dataset("edu", tmp_path).row_counts is None
