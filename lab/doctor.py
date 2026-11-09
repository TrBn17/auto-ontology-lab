"""`lab doctor`: report whether the stack matches the frozen environment. Changes nothing.

This replaces `ensure_clean_env` from scripts/import_dataset.py, which edited
auto-ontology/.env and restarted containers. Doctor only reports, so importing a
dataset has no side effects on the environment.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from lab.common.config import REPO_ROOT, LabConfig, load_config, require_token
from lab.common.errors import LabError
from lab.common.gateway import Gateway
from lab.common.pg import Psql

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_PLACEHOLDER_KEY = "nvapi-your-api-key-here"
_API_KEY_NAME = re.compile(r"^[A-Z_]*API_KEY$")
_NEXT_RUN = re.compile(r"(\w+): next run at (\S+)")
_ROLE_ENV = {
    "reasoning": "REASONING_MODEL",
    "non_reasoning": "NON_REASONING_MODEL",
    "embed": "EMBED_MODEL",
    "rerank": "RERANK_MODEL",
}
# semanticCompilation:read is in the admin role only (frontend/auth/access.json).
_ROLE_PROBE_PATH = "/api/configurations/semantic-compilation"
_EXPECTED_PROBE_STATUS = {"admin": 200, "viewer": 403}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def check_upstream_commit(actual: str, pinned: str) -> Check:
    if actual == pinned:
        return Check("upstream commit", PASS, actual[:8])
    return Check("upstream commit", FAIL, f"auto-ontology/ is at {actual[:8]}, freeze pins {pinned[:8]}")


def check_image_age(image_created: str, commit_time: str | None, pinned: str, *, revision: str = "") -> Check:
    """Trust the image's revision label; without one, an older image cannot contain the pin."""
    name = "running image"
    if revision:
        if revision == pinned:
            return Check(name, PASS, f"built from {pinned[:8]}")
        return Check(name, FAIL, f"built from {revision[:8]}, freeze pins {pinned[:8]}")
    if commit_time is None:
        return Check(name, FAIL, f"commit {pinned[:8]} not found in auto-ontology/, so the image cannot be checked")
    built = _parse_time(image_created)
    if built < _parse_time(commit_time):
        return Check(name, FAIL, f"built {image_created[:19]}Z, before {pinned[:8]} ({commit_time}); rebuild at the pin")
    return Check(name, WARN, f"built after {pinned[:8]}, but the build source is not recorded, so this is not proof")


def check_alembic_head(actual: str | None, pinned: str) -> Check:
    if actual == pinned:
        return Check("catalog schema revision", PASS, pinned)
    return Check("catalog schema revision", FAIL, f"catalog is at {actual}, freeze pins {pinned}")


def check_env_file(text: str) -> Check:
    """Check auto-ontology/.env. Details name variables and never include their values."""
    problems: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key == "CONNECTION_STRINGS" and value:
            problems.append("CONNECTION_STRINGS is set; leave it empty and register connections through the API")
        if _API_KEY_NAME.match(key) and value == _PLACEHOLDER_KEY:
            problems.append(f"{key} still holds the placeholder")
    if problems:
        return Check("auto-ontology/.env", FAIL, "; ".join(problems))
    return Check("auto-ontology/.env", PASS, "")


def check_frozen_models(frozen: dict[str, dict[str, str]], running: dict[str, str | None]) -> Check:
    problems: list[str] = []
    for role, actual in running.items():
        pinned = (frozen.get(role) or {}).get("model") or ""
        if not pinned:
            problems.append(f"{role} is blank in the freeze file (running: {actual})")
        elif pinned != actual:
            problems.append(f"{role}: freeze pins {pinned}, running {actual}")
    if problems:
        return Check("frozen models", FAIL, "; ".join(problems))
    return Check("frozen models", PASS, "")


def check_embed_match(backend: str | None, ingestion: str | None) -> Check:
    if backend == ingestion:
        return Check("EMBED_MODEL across services", PASS, str(backend))
    return Check("EMBED_MODEL across services", FAIL, f"backend {backend}, ingestion {ingestion}")


def check_next_runs(logs: str, now: datetime, *, window_hours: float) -> Check:
    """Warn when a 24h scheduler pass falls inside the coming measurement window."""
    latest = {match[1]: match[2] for match in _NEXT_RUN.finditer(logs)}
    cutoff = now + timedelta(hours=window_hours)
    soon = []
    for name, stamp in sorted(latest.items()):
        when = datetime.fromisoformat(stamp)
        if now <= when <= cutoff:
            soon.append(f"{name} at {stamp}")
    if soon:
        return Check("scheduled passes", WARN, f"within {window_hours:g}h and could change state mid-run: {', '.join(soon)}")
    return Check("scheduled passes", PASS, f"none within {window_hours:g}h")


def check_other_connections(names: Iterable[str]) -> Check:
    found = sorted(names)
    if found:
        return Check(
            "registered connections",
            WARN,
            f"{', '.join(found)}: every compile pass also spends LLM calls on these and can fail because of them",
        )
    return Check("registered connections", PASS, "none")


def check_token_role(role: str, status: int | None) -> Check:
    name = f"{role} token"
    expected = _EXPECTED_PROBE_STATUS[role]
    if status == expected:
        return Check(name, PASS, f"{_ROLE_PROBE_PATH} returned {status}")
    return Check(name, FAIL, f"{_ROLE_PROBE_PATH} returned {status}, expected {expected}")


def exit_code(checks: Iterable[Check]) -> int:
    return 1 if any(check.status == FAIL for check in checks) else 0


def collect(cfg: LabConfig, *, window_hours: float) -> list[Check]:
    psql = Psql(cfg.containers.postgres, cfg.pg_superuser)
    checks = [_gateway_health(cfg)]
    checks += [_container_state(name) for name in _names(cfg)]
    checks.append(check_upstream_commit(_upstream_head(), cfg.upstream_commit))
    checks.append(_attempt("running image", lambda: check_image_age(
        _image_created(cfg.containers.backend), _commit_time(cfg.upstream_commit), cfg.upstream_commit,
        revision=_image_revision(cfg.containers.backend))))
    checks.append(_attempt("catalog schema revision", lambda: check_alembic_head(
        psql.scalar(cfg.catalog_db, "SELECT version_num FROM public.alembic_version"), cfg.alembic_head)))
    env_path = REPO_ROOT / "auto-ontology" / ".env"
    if env_path.is_file():
        checks.append(check_env_file(env_path.read_text(encoding="utf-8")))
    else:
        checks.append(Check("auto-ontology/.env", FAIL, "file not found"))
    running = {role: _container_env(cfg.containers.backend, var) for role, var in _ROLE_ENV.items()}
    checks.append(check_frozen_models(cfg.models, running))
    checks.append(check_embed_match(running["embed"], _container_env(cfg.containers.ingestion, _ROLE_ENV["embed"])))
    checks.append(_attempt("registered connections", lambda: check_other_connections(
        row[0] for row in psql.query(cfg.catalog_db, "SELECT name FROM catalog_database"))))
    checks.append(_attempt("scheduled passes", lambda: check_next_runs(
        _full_log(cfg.containers.ingestion), datetime.now(timezone.utc), window_hours=window_hours)))
    for role in ("admin", "viewer"):
        checks.append(_token_check(cfg, role))
    return checks


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--freeze", type=Path, default=None, help="Freeze file (default: config/freeze.yaml).")
    parser.add_argument(
        "--window-hours", type=float, default=8.0, help="Warn about scheduled passes this soon (default: 8)."
    )


def run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.freeze)
    except LabError as exc:
        print(f"FAIL  freeze file: {exc}", file=sys.stderr)
        return 1
    checks = collect(cfg, window_hours=args.window_hours)
    for check in checks:
        print(f"{check.status:<5} {check.name}" + (f": {check.detail}" if check.detail else ""))
    return exit_code(checks)


def _attempt(name: str, build) -> Check:
    try:
        return build()
    except (LabError, OSError) as exc:
        return Check(name, FAIL, str(exc))


def _gateway_health(cfg: LabConfig) -> Check:
    try:
        status = httpx.get(f"{cfg.gateway_url}/api/health", timeout=10).status_code
    except httpx.HTTPError as exc:
        return Check("gateway health", FAIL, f"{cfg.gateway_url} unreachable: {exc}")
    return Check("gateway health", PASS if status == 200 else FAIL, f"returned {status}")


def _names(cfg: LabConfig) -> list[str]:
    c = cfg.containers
    return [c.postgres, c.backend, c.ingestion, c.frontend]


def _container_state(name: str) -> Check:
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}", name],
        capture_output=True,
        text=True,
    )
    state = proc.stdout.strip()
    healthy = proc.returncode == 0 and state == "running/healthy"
    return Check(f"container {name}", PASS if healthy else FAIL, state or "not found")


def _parse_time(stamp: str) -> datetime:
    # Docker reports nanoseconds, which datetime cannot parse; seconds are precise enough here.
    return datetime.fromisoformat(re.sub(r"\.\d+", "", stamp).replace("Z", "+00:00"))


def _image_created(container: str) -> str:
    image = subprocess.run(["docker", "inspect", container, "--format", "{{.Image}}"], capture_output=True, text=True)
    created = subprocess.run(
        ["docker", "image", "inspect", image.stdout.strip(), "--format", "{{.Created}}"], capture_output=True, text=True
    )
    if image.returncode != 0 or created.returncode != 0:
        raise LabError(f"could not read the image of container {container}")
    return created.stdout.strip()


def _image_revision(container: str) -> str:
    proc = subprocess.run(
        ["docker", "inspect", container, "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}'],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _commit_time(commit: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT / "auto-ontology"), "log", "-1", "--format=%cI", commit],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _upstream_head() -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT / "auto-ontology"), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return proc.stdout.strip() or "unknown"


def _container_env(container: str, name: str) -> str | None:
    """Read one named variable. Only model names are ever requested, never keys."""
    proc = subprocess.run(["docker", "exec", container, "printenv", name], capture_output=True, text=True)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _full_log(container: str) -> str:
    proc = subprocess.run(["docker", "logs", container], capture_output=True)
    return (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")


def _token_check(cfg: LabConfig, role: str) -> Check:
    try:
        token = require_token(role)  # type: ignore[arg-type]
    except LabError as exc:
        return Check(f"{role} token", FAIL, str(exc))
    try:
        return check_token_role(role, Gateway(cfg.gateway_url, token).status_of(_ROLE_PROBE_PATH))
    except httpx.HTTPError:
        return check_token_role(role, None)


def main() -> int:
    parser = argparse.ArgumentParser(prog="lab doctor", description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
