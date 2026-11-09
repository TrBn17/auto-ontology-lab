"""Load the frozen environment description and the API tokens."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from lab.common.errors import LabError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FREEZE = REPO_ROOT / "config" / "freeze.yaml"
TOKEN_PREFIX = "auto_ontology_"
_TOKEN_ENV = {"admin": "LAB_ADMIN_TOKEN", "viewer": "LAB_VIEWER_TOKEN"}

Role = Literal["admin", "viewer"]


@dataclass(frozen=True)
class Containers:
    postgres: str
    backend: str
    ingestion: str
    frontend: str


@dataclass(frozen=True)
class LabConfig:
    path: Path
    upstream_commit: str
    alembic_head: str
    gateway_url: str
    ingestion_url: str
    containers: Containers
    catalog_db: str
    pg_superuser: str
    models: dict[str, dict[str, str]]
    flags: dict[str, Any]


def load_config(path: Path | None = None) -> LabConfig:
    """Read freeze.yaml, naming the first missing key rather than raising KeyError."""
    freeze_path = path or DEFAULT_FREEZE
    if not freeze_path.is_file():
        raise LabError(f"{freeze_path} not found. Copy config/freeze.yaml.example to it and fill in the models.")
    raw = yaml.safe_load(freeze_path.read_text(encoding="utf-8")) or {}

    upstream = _require(raw, "upstream.auto_ontology")
    containers = _require(raw, "containers")
    return LabConfig(
        path=freeze_path,
        upstream_commit=_require(upstream, "commit", "upstream.auto_ontology."),
        alembic_head=_require(upstream, "alembic_head", "upstream.auto_ontology."),
        gateway_url=str(_require(raw, "endpoints.gateway")).rstrip("/"),
        ingestion_url=str(_require(raw, "endpoints.ingestion_internal")).rstrip("/"),
        containers=Containers(
            postgres=_require(containers, "postgres", "containers."),
            backend=_require(containers, "backend", "containers."),
            ingestion=_require(containers, "ingestion", "containers."),
            frontend=_require(containers, "frontend", "containers."),
        ),
        catalog_db=_require(raw, "catalog.database"),
        pg_superuser=_require(raw, "catalog.superuser"),
        models=raw.get("models") or {},
        flags=raw.get("flags") or {},
    )


def require_token(role: Role) -> str:
    """Return the API token for a role from the environment."""
    name = _TOKEN_ENV[role]
    token = os.environ.get(name, "").strip()
    if not token:
        raise LabError(
            f"{name} is not set. Tokens can only be minted from a browser session: sign in to the web UI "
            f"with the {role} role, create a token, then export it as {name}."
        )
    if not token.startswith(TOKEN_PREFIX):
        # The gateway does not treat other x-api-key values as tokens, which would surface as a bare 401.
        raise LabError(f"{name} does not start with {TOKEN_PREFIX!r}, so it is not an Auto-Ontology API token.")
    return token


def _require(mapping: dict[str, Any], dotted: str, prefix: str = "") -> Any:
    node: Any = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or node.get(part) in (None, ""):
            raise LabError(f"freeze file is missing {prefix}{dotted}")
        node = node[part]
    return node
