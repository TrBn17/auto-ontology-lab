"""Per-run artefact directories under reports/runs/."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab.common.config import REPO_ROOT

DEFAULT_ROOT = REPO_ROOT / "reports" / "runs"


@dataclass(frozen=True)
class RunDir:
    path: Path

    @classmethod
    def create(cls, *, root: Path | None = None, now: datetime | None = None) -> RunDir:
        """Create reports/runs/<UTC timestamp>/, adding a suffix if that name is taken."""
        base = root or DEFAULT_ROOT
        base.mkdir(parents=True, exist_ok=True)
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        attempt = 0
        while True:
            path = base / (stamp if attempt == 0 else f"{stamp}-{attempt}")
            try:
                path.mkdir()
                return cls(path)
            except FileExistsError:
                attempt += 1

    def write_json(self, name: str, data: Any) -> Path:
        """Write sorted, indented JSON so runs can be compared with a plain diff."""
        target = self.path / name
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        target.write_text(text + "\n", encoding="utf-8", newline="\n")
        return target
