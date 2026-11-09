"""Fixtures shared by lab tests.

``scratch`` runs a throwaway Postgres container using the same image as the stack.
It publishes no port and is removed afterwards, so these tests never touch the
running Auto-Ontology catalog.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

from lab.common.pg import Psql

SCRATCH_IMAGE = "pgvector/pgvector:pg17"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "docker: needs a local Docker daemon")


@pytest.fixture(scope="session")
def scratch() -> Iterator[Psql]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    name = f"lab-scratch-pg-{os.getpid()}"
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_HOST_AUTH_METHOD=trust", SCRATCH_IMAGE],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start scratch Postgres: {started.stderr.strip()}")
    try:
        psql = Psql(name, "postgres")
        _wait_until_ready(psql)
        yield psql
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _wait_until_ready(psql: Psql, timeout: float = 60.0) -> None:
    """Wait for the server that survives initdb (the image restarts Postgres once)."""
    deadline = time.monotonic() + timeout
    consecutive = 0
    while time.monotonic() < deadline:
        try:
            psql.scalar("postgres", "SELECT 1")
            consecutive += 1
            if consecutive >= 3:
                return
        except Exception:
            consecutive = 0
        time.sleep(1)
    raise TimeoutError("scratch Postgres did not become ready")
