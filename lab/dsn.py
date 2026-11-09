"""Safe DSN handling and password-in-argv protection (§1.4)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
import getpass
from pathlib import Path
import sys
from urllib.parse import quote, unquote, urlparse, urlunparse


_URI_WITH_PASS_RE = re.compile(r"^[a-zA-Z0-9+.-]+://[^/:@\s]+:[^@\s]+@")
_SHORTHAND_WITH_PASS_RE = re.compile(r"^[^/:@\s]+:[^/:@\s]+@[^/\s]+")
_KV_WITH_PASS_RE = re.compile(r"(?:^|\s)password\s*=\s*\S+", re.IGNORECASE)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LAB_DOTENV = _REPO_ROOT / ".env"
_AUTO_ONTOLOGY_ENV = _REPO_ROOT / "auto-ontology" / ".env"


def load_lab_dotenv(dotenv_path: Path | None = None) -> None:
    """Load non-empty key=value pairs from root `.env` into `os.environ` if not already set."""
    target = dotenv_path or _LAB_DOTENV
    if not target.is_file():
        return
    try:
        for raw_line in target.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            key = k.strip()
            val = v.strip().strip("'\"")
            if key and val and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


@dataclass(frozen=True)
class ParsedDSN:
    """Normalized PostgreSQL connection parameters."""

    raw_dsn: str
    host: str
    port: str
    user: str
    password: str | None
    dbname: str

    @property
    def safe_dsn(self) -> str:
        """Return a passwordless URI suitable for CLI arguments or logging."""
        parsed = urlparse(self.raw_dsn)
        if parsed.scheme and parsed.hostname:
            user_part = f"{parsed.username}@" if parsed.username else ""
            port_part = f":{parsed.port}" if parsed.port else ""
            netloc = f"{user_part}{parsed.hostname}{port_part}"
            return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
        return re.sub(r"(password\s*=\s*)\S+", r"\1***", self.raw_dsn, flags=re.IGNORECASE)

    def to_pg_env(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        """Build an environment dictionary with PG* variables so no password appears in argv."""
        env = dict(base_env if base_env is not None else os.environ)
        if self.host:
            env["PGHOST"] = self.host
        if self.port:
            env["PGPORT"] = str(self.port)
        if self.user:
            env["PGUSER"] = self.user
        if self.dbname:
            env["PGDATABASE"] = self.dbname
        if self.password is not None:
            env["PGPASSWORD"] = self.password
        return env


def dsn_contains_password(dsn: str) -> bool:
    """Return True if the DSN string embeds a password."""
    text = dsn.strip()
    if _URI_WITH_PASS_RE.search(text):
        return True
    if _SHORTHAND_WITH_PASS_RE.search(text):
        return True
    if _KV_WITH_PASS_RE.search(text):
        return True
    parsed = urlparse(text)
    if parsed.scheme and parsed.password is not None:
        return True
    return False


def assert_argv_has_no_passwords(argv: list[str]) -> None:
    """Refuse execution if any command-line argument embeds a database password (§1.4)."""
    for arg in argv:
        if dsn_contains_password(arg):
            raise ValueError(
                "Security violation (§1.4): Database passwords must not be passed in command-line arguments. "
                "Use --from-env / --to-env with environment variables or ~/.pgpass instead."
            )
        if "=" in arg:
            _, _, val = arg.partition("=")
            if dsn_contains_password(val):
                raise ValueError(
                    "Security violation (§1.4): Database passwords must not be passed in command-line arguments. "
                    "Use --from-env / --to-env with environment variables or ~/.pgpass instead."
                )


def _lookup_pgpass(host: str, port: str, dbname: str, user: str) -> str | None:
    """Look up a password in PGPASSFILE, ~/.pgpass, or %APPDATA%/postgresql/pgpass.conf."""
    candidates: list[Path] = []
    env_pgpass = os.environ.get("PGPASSFILE")
    if env_pgpass:
        candidates.append(Path(env_pgpass))
    candidates.append(Path.home() / ".pgpass")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "postgresql" / "pgpass.conf")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = re.split(r"(?<!\\):", line, maxsplit=4)
                if len(parts) != 5:
                    continue
                p_host, p_port, p_db, p_user, p_pass = [p.replace(r"\:", ":").replace(r"\\", "\\") for p in parts]
                if (
                    p_host in ("*", host)
                    and p_port in ("*", str(port))
                    and p_db in ("*", dbname)
                    and p_user in ("*", user)
                ):
                    return p_pass
        except OSError:
            continue
    return None


def _lookup_local_compose_env() -> tuple[str | None, str | None]:
    """Read POSTGRES_USER and POSTGRES_PASSWORD from auto-ontology/.env if available."""
    if not _AUTO_ONTOLOGY_ENV.is_file():
        return None, None
    user: str | None = None
    password: str | None = None
    try:
        for raw_line in _AUTO_ONTOLOGY_ENV.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            key = k.strip()
            val = v.strip().strip("'\"")
            if key == "POSTGRES_USER":
                user = val
            elif key == "POSTGRES_PASSWORD":
                password = val
    except OSError:
        pass
    return user, password


def _resolve_missing_password(
    host: str,
    port: str,
    dbname: str,
    user: str,
    *,
    role_label: str = "database",
    prompt_password: bool = False,
) -> str | None:
    """Resolve password when not embedded in DSN: env vars -> .pgpass -> local .env -> interactive prompt."""
    load_lab_dotenv()
    if role_label == "from" and os.environ.get("LAB_SOURCE_PASSWORD"):
        return os.environ["LAB_SOURCE_PASSWORD"]
    if role_label == "to" and os.environ.get("LAB_TARGET_PASSWORD"):
        return os.environ["LAB_TARGET_PASSWORD"]
    if os.environ.get("PGPASSWORD"):
        return os.environ["PGPASSWORD"]

    pgpass_pw = _lookup_pgpass(host, port, dbname, user)
    if pgpass_pw is not None:
        return pgpass_pw

    if host.lower() in ("localhost", "127.0.0.1", "::1"):
        _, local_pw = _lookup_local_compose_env()
        if local_pw:
            return local_pw

    if prompt_password and sys.stdin is not None and sys.stdin.isatty():
        try:
            prompted = getpass.getpass(f"Password for {user}@{host}:{port}/{dbname}: ")
            if prompted:
                return prompted
        except (EOFError, KeyboardInterrupt):
            pass

    return None


def _build_uri(host: str, port: str, user: str, password: str | None, dbname: str) -> str:
    """Build a normalized postgresql:// URI for psycopg."""
    u_enc = quote(user, safe="")
    if password is not None:
        p_enc = quote(password, safe="")
        auth = f"{u_enc}:{p_enc}@"
    else:
        auth = f"{u_enc}@" if u_enc else ""
    return f"postgresql://{auth}{host}:{port}/{dbname}"


def _normalize_shorthand_dsn(text: str) -> str:
    """Expand shorthand connection strings like `postgres@localhost:5432/mydb` or `postgres@localhost/mydb`."""
    if "://" in text or "=" in text:
        return text
    # Plain database name (e.g. "lms_synth") -> local postgres
    if re.match(r"^[a-zA-Z0-9_-]+$", text):
        return f"postgresql://localhost:5432/{text}"
    # Shorthand like user@host:port/db, user@host/db, host:port/db, host/db, user@host
    return f"postgresql://{text}"


def parse_dsn(
    dsn: str,
    *,
    role_label: str = "database",
    prompt_password: bool = False,
) -> ParsedDSN:
    """Parse a PostgreSQL URI, shorthand (`user@host[:port]/db`), or key=value connection string."""
    text = _normalize_shorthand_dsn(dsn.strip())
    parsed = urlparse(text)
    if parsed.scheme and (parsed.hostname or parsed.path):
        host = parsed.hostname or os.environ.get("PGHOST", "localhost")
        port = str(parsed.port or os.environ.get("PGPORT", "5432"))
        local_user, _ = _lookup_local_compose_env() if host.lower() in ("localhost", "127.0.0.1", "::1") else (None, None)
        user = unquote(parsed.username) if parsed.username else os.environ.get("PGUSER", local_user or "postgres")
        dbname = parsed.path.lstrip("/") or os.environ.get("PGDATABASE", "postgres")
        password = (
            unquote(parsed.password)
            if parsed.password is not None
            else _resolve_missing_password(
                host,
                port,
                dbname,
                user,
                role_label=role_label,
                prompt_password=prompt_password,
            )
        )
        return ParsedDSN(
            raw_dsn=_build_uri(host, port, user, password, dbname),
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=dbname,
        )

    # Fallback key=value libpq format
    kv: dict[str, str] = {}
    for match in re.finditer(r"(\w+)\s*=\s*(?:'([^']*)'|(\S+))", text):
        key = match.group(1).lower()
        val = match.group(2) if match.group(2) is not None else match.group(3)
        kv[key] = val

    host = kv.get("host", os.environ.get("PGHOST", "localhost"))
    port = kv.get("port", os.environ.get("PGPORT", "5432"))
    user = kv.get("user", os.environ.get("PGUSER", "postgres"))
    dbname = kv.get("dbname", os.environ.get("PGDATABASE", "postgres"))
    password = kv.get("password") or _resolve_missing_password(
        host,
        port,
        dbname,
        user,
        role_label=role_label,
        prompt_password=prompt_password,
    )

    return ParsedDSN(
        raw_dsn=_build_uri(host, port, user, password, dbname),
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
    )


def resolve_dsn(
    *,
    env_var: str | None = None,
    direct_dsn: str | None = None,
    role_label: str = "database",
    default_dbname: str | None = None,
    prompt_password: bool = True,
) -> ParsedDSN:
    """Resolve a DSN from shorthand/direct DSN, environment variable, or local default."""
    load_lab_dotenv()
    if direct_dsn:
        if dsn_contains_password(direct_dsn):
            raise ValueError(
                f"Refusing {role_label} DSN with embedded password in arguments (§1.4). "
                f"Pass passwordless shorthand like 'postgres@localhost/db' (password via env/.pgpass) "
                f"or use --{role_label}-env <ENV_VAR>."
            )
        return parse_dsn(direct_dsn, role_label=role_label, prompt_password=prompt_password)

    if env_var:
        val = os.environ.get(env_var, "").strip()
        if not val:
            raise ValueError(f"Environment variable {env_var!r} for {role_label} DSN is not set or empty.")
        return parse_dsn(val, role_label=role_label, prompt_password=prompt_password)

    # Smart automatic fallback to standard env vars
    default_env = "LAB_SOURCE_DSN" if role_label == "from" else "LAB_TARGET_DSN"
    if os.environ.get(default_env, "").strip():
        return parse_dsn(os.environ[default_env].strip(), role_label=role_label, prompt_password=prompt_password)

    # For target DB ("to"), if default_dbname is known, auto-connect to local Postgres
    if role_label == "to" and default_dbname:
        safe_db = re.sub(r"[^a-zA-Z0-9_]+", "_", default_dbname.strip().lower()).strip("_") or "lab_synth"
        return parse_dsn(f"localhost:5432/{safe_db}", role_label="to", prompt_password=prompt_password)

    raise ValueError(
        f"Missing {role_label} DSN: pass '{'user@host/db' if role_label == 'from' else '--to user@host/db'}' "
        f"or set {default_env} / --{role_label}-env <ENV_VAR>."
    )
