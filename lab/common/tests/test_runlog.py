from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from lab.common.runlog import RunDir

NOW = datetime(2026, 11, 7, 1, 2, 3, tzinfo=timezone.utc)


def test_creates_utc_timestamped_directory_and_writes_json(tmp_path: Path) -> None:
    run = RunDir.create(root=tmp_path, now=NOW)
    assert run.path == tmp_path / "20261107T010203Z"
    written = run.write_json("import.json", {"dataset": "edu", "seconds": 1.5, "name": "tiếng Việt"})
    assert written == run.path / "import.json"
    assert json.loads(written.read_text(encoding="utf-8")) == {"dataset": "edu", "seconds": 1.5, "name": "tiếng Việt"}


def test_two_runs_in_the_same_second_do_not_share_a_directory(tmp_path: Path) -> None:
    first = RunDir.create(root=tmp_path, now=NOW)
    second = RunDir.create(root=tmp_path, now=NOW)
    assert first.path != second.path
    assert second.path.name == "20261107T010203Z-1"


def test_json_is_stable_for_diffing(tmp_path: Path) -> None:
    run = RunDir.create(root=tmp_path, now=NOW)
    path = run.write_json("a.json", {"b": 1, "a": 2})
    assert path.read_text(encoding="utf-8") == '{\n  "a": 2,\n  "b": 1\n}\n'
