"""Run Postgres client tools inside the stack's container.

SQL always travels over stdin, so nothing sensitive reaches a process list. The host
needs no Postgres client, and the tool versions always match the server.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from lab.common.errors import LabError

_FIELD_SEP = "\x1f"
_PASSWORD_CLAUSE = re.compile(r"(PASSWORD\s+)'(?:[^']|'')*'?", re.IGNORECASE)
_CHUNK = 1024 * 1024


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def scrub_secrets(text: str) -> str:
    """Mask password clauses, because psql echoes the failing statement in its errors."""
    return _PASSWORD_CLAUSE.sub(r"\1'***'", text)


def unsafe_sql_reason(sql: str) -> str | None:
    """Explain why LLM-written SQL must not reach psql, or return None when it is one plain statement.

    `lab_ro` can leave read-only mode (docs/FINDINGS.md F6), so a second statement could write.
    A single statement stays in the read-only transaction it started in.
    """
    if "\\" in sql:
        return "SQL contains a backslash, which psql could read as a meta-command"
    quote = ""
    body = sql.strip().rstrip(";")
    for char in body:
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == ";":
            return "SQL contains more than one statement"
    return None


class Psql:
    """Postgres client tools for one container, connecting as one role."""

    def __init__(self, container: str, user: str) -> None:
        self.container = container
        self.user = user

    def query(self, database: str, sql: str) -> list[tuple[str, ...]]:
        """Return rows as text tuples. SQL NULL and the empty string both come back as ''."""
        stdout = self._run_psql(database, sql, "-t", "-A", "-0", "-F", _FIELD_SEP)
        records = [record for record in stdout.decode("utf-8").split("\0") if record]
        return [tuple(record.split(_FIELD_SEP)) for record in records]

    def scalar(self, database: str, sql: str) -> str | None:
        """Return the first column of the first row, or None when there are no rows."""
        rows = self.query(database, sql)
        return rows[0][0] if rows else None

    def execute(self, database: str, sql: str, *, single_transaction: bool = False) -> None:
        self._run_psql(database, sql, *(["-1"] if single_transaction else []))

    def run_file(self, database: str, path: Path, *, prefix: str = "", single_transaction: bool = False) -> None:
        """Stream a SQL file as raw bytes, with optional SQL sent ahead of it."""
        cmd = self._exec("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", database)
        if single_transaction:
            cmd.append("-1")
        # stderr goes to a file: a pipe could fill up and deadlock against the stdin writer.
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            assert proc.stdin is not None
            try:
                proc.stdin.write(prefix.encode("utf-8"))
                with path.open("rb") as source:
                    while chunk := source.read(_CHUNK):
                        proc.stdin.write(chunk)
            except BrokenPipeError:
                pass  # psql stopped at an error; its exit code and stderr report it below.
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            returncode = proc.wait()
            errors.seek(0)
            self._check(returncode, errors.read(), "psql")

    def dump_to_file(self, tool_args: list[str], dest: Path) -> None:
        """Write pg_dump output to dest atomically, leaving nothing behind on failure."""
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name, suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                proc = subprocess.run(self._exec("pg_dump", *tool_args), stdout=out, stderr=subprocess.PIPE)
            self._check(proc.returncode, proc.stderr, "pg_dump")
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)

    def restore_from_file(self, tool_args: list[str], path: Path) -> None:
        """Feed a custom-format dump to pg_restore over stdin."""
        with path.open("rb") as source:
            proc = subprocess.run(
                self._exec("pg_restore", *tool_args), stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
        self._check(proc.returncode, proc.stderr, "pg_restore")

    def _run_psql(self, database: str, sql: str, *extra: str) -> bytes:
        cmd = self._exec("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", database, *extra)
        proc = subprocess.run(cmd, input=sql.encode("utf-8"), capture_output=True)
        self._check(proc.returncode, proc.stderr, "psql")
        return proc.stdout

    def _exec(self, tool: str, *args: str) -> list[str]:
        return ["docker", "exec", "-i", self.container, tool, "-U", self.user, *args]

    @staticmethod
    def _check(returncode: int, stderr: bytes, tool: str) -> None:
        if returncode != 0:
            detail = scrub_secrets(stderr.decode("utf-8", errors="replace")).strip()
            raise LabError(f"{tool} failed with exit code {returncode}: {detail}")
