from __future__ import annotations

from pathlib import Path

import pytest

from lab.common.config import load_config, require_token
from lab.common.errors import LabError

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_example_freeze_loads_with_actual_compose_container_names() -> None:
    cfg = load_config(REPO_ROOT / "config" / "freeze.yaml.example")
    assert cfg.upstream_commit == "c447003f72a822301e927196a4ecb5b597c308d5"
    assert cfg.gateway_url == "http://localhost:3000"
    assert cfg.catalog_db == "auto_ontology"
    assert cfg.pg_superuser == "postgres"
    # docker-compose.yml container_name values, not the service names.
    assert cfg.containers.postgres == "postgres"
    assert cfg.containers.backend == "auto-ontology"
    assert cfg.containers.ingestion == "auto-ontology-ingestion-service"
    assert cfg.containers.frontend == "auto-ontology-frontend"


def test_missing_freeze_file_says_how_to_create_it(tmp_path: Path) -> None:
    with pytest.raises(LabError, match=r"freeze\.yaml\.example"):
        load_config(tmp_path / "freeze.yaml")


def test_freeze_missing_a_required_key_names_it(tmp_path: Path) -> None:
    path = tmp_path / "freeze.yaml"
    path.write_text("upstream: {}\n", encoding="utf-8")
    with pytest.raises(LabError, match="upstream.auto_ontology"):
        load_config(path)


def test_require_token_reads_role_specific_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_VIEWER_TOKEN", "auto_ontology_abc")
    assert require_token("viewer") == "auto_ontology_abc"


def test_require_token_missing_explains_minting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAB_ADMIN_TOKEN", raising=False)
    with pytest.raises(LabError, match="LAB_ADMIN_TOKEN"):
        require_token("admin")


def test_require_token_rejects_wrong_prefix_without_echoing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gateway ignores x-api-key values lacking this prefix, which would surface as a confusing 401.
    monkeypatch.setenv("LAB_ADMIN_TOKEN", "sk-secret-value")
    with pytest.raises(LabError) as exc:
        require_token("admin")
    assert "auto_ontology_" in str(exc.value)
    assert "sk-secret-value" not in str(exc.value)
