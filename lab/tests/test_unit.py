"""Unit tests for Step 1 (`lab clone`) modules: DSN guards, PII veto, DDL sanitizer, quotas, and planning."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from lab.cli import _apply_clone_yaml_defaults, _resolve_clone_inputs, _write_clone_yaml_template, build_parser
from lab.ddl import build_pg_dump_command, sanitize_ddl
from lab.dsn import assert_argv_has_no_passwords, dsn_contains_password, load_lab_dotenv, parse_dsn, resolve_dsn
from lab.generate import allocate_categorical_column, allocate_null_mask, _make_rng
from lab.pii import PIIMatcher
from lab.plan import UnbreakableFKCycleError, build_generation_plan, compute_target_row_count


def test_dsn_password_in_argv_rejected() -> None:
    assert dsn_contains_password("postgresql://ro_user:secret123@prod:5432/lms") is True
    assert dsn_contains_password("host=localhost user=postgres password=secret dbname=lms") is True
    assert dsn_contains_password("postgresql://ro_user@prod:5432/lms") is False

    with pytest.raises(ValueError, match="Security violation"):
        assert_argv_has_no_passwords(["clone", "--from", "postgresql://u:p@localhost:5432/db"])

    with pytest.raises(ValueError, match="Refusing from DSN with embedded password"):
        resolve_dsn(direct_dsn="postgresql://u:p@localhost:5432/db", role_label="from")


def test_pg_dump_command_never_exposes_password_in_argv() -> None:
    dsn = parse_dsn("postgresql://ro_user:TopSecretPass99!@db.internal:5432/lms")
    cmd, env = build_pg_dump_command(dsn, docker_container="postgres")
    for arg in cmd:
        assert "TopSecretPass99!" not in arg
        assert not dsn_contains_password(arg)
    assert env["PGPASSWORD"] == "TopSecretPass99!"


def test_pii_matcher_and_veto_overrides() -> None:
    # Empty reason must be refused
    with pytest.raises(ValueError, match="Refusing empty reason"):
        PIIMatcher(raw_overrides=["public.payment.card_type="])

    with pytest.raises(ValueError, match="Refusing empty reason"):
        PIIMatcher(raw_overrides=["public.payment.card_type=   "])

    matcher = PIIMatcher(
        extra_patterns=["national_id"],
        raw_overrides=["public.payment.card_type=payment network category"],
    )
    assert matcher.match_column("student_email") == "email"
    assert matcher.match_column("birth_date") == "birth"
    assert matcher.match_column("card_type") == "card"
    assert matcher.match_column("national_id_num") == "national_id"
    assert matcher.match_column("course_code") is None
    assert matcher.is_veto_overridden("public.payment.card_type") is True
    assert matcher.is_veto_overridden("public.student.email") is False

    audit = matcher.build_audit_overrides(
        {
            "public.payment.card_type": "card_type",
            "public.student.email": "email",
        }
    )
    assert audit == [
        {
            "column": "public.payment.card_type",
            "matched_pattern": "card",
            "reason": "payment network category",
        }
    ]


def test_ddl_sanitizer_strips_rls_triggers_comments_and_owners() -> None:
    raw_sql = """
    \\restrict 12345
    \\connect prod_db
    SET default_tablespace = 'pg_default';
    CREATE TABLE public.accounts (id int PRIMARY KEY, name text);
    ALTER TABLE public.accounts OWNER TO prod_admin;
    ALTER TABLE public.accounts ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.accounts FORCE ROW LEVEL SECURITY;
    CREATE POLICY acc_pol ON public.accounts USING (true);
    CREATE TRIGGER trg_acc BEFORE INSERT ON public.accounts FOR EACH ROW EXECUTE FUNCTION fn();
    ALTER TABLE public.accounts ENABLE TRIGGER trg_acc;
    COMMENT ON TABLE public.accounts IS 'Sensitive CANARY_7f3a comment';
    COMMENT ON COLUMN public.accounts.name IS 'Sensitive column comment';
    GRANT SELECT ON public.accounts TO public;
    REVOKE ALL ON public.accounts FROM anon;
    \\unrestrict 12345
    """
    cleaned = sanitize_ddl(raw_sql)
    assert "CREATE TABLE public.accounts" in cleaned
    assert "ROW LEVEL SECURITY" not in cleaned
    assert "CREATE POLICY" not in cleaned
    assert "TRIGGER" not in cleaned
    assert "COMMENT ON" not in cleaned
    assert "CANARY_7f3a" not in cleaned
    assert "OWNER TO" not in cleaned
    assert "GRANT" not in cleaned
    assert "REVOKE" not in cleaned
    assert "\\restrict" not in cleaned
    assert "\\connect" not in cleaned


def test_row_count_formula_a4() -> None:
    assert compute_target_row_count(0, 0.1) == 0
    assert compute_target_row_count(2, 0.1) == 2
    assert compute_target_row_count(5, 0.1) == 5
    assert compute_target_row_count(40, 0.1) == 40
    assert compute_target_row_count(100, 0.1) == 50
    assert compute_target_row_count(11_000, 0.1) == 1_100
    assert compute_target_row_count(1_000_000, 0.1, max_rows=50_000) == 50_000


def test_exact_quota_and_first_1000_stratification() -> None:
    rng = _make_rng(42, "test_quota")
    weights = {"view": 0.55, "submit": 0.30, "grade": 0.15}
    vals = allocate_categorical_column(1500, 0.20, True, weights, rng)
    assert len(vals) == 1500

    # Total null rate check (A6)
    total_nulls = sum(1 for v in vals if v is None)
    assert abs(total_nulls / 1500 - 0.20) <= 0.001

    # First 1,000 rows check (A11)
    first_1000 = vals[:1000]
    first_nulls = sum(1 for v in first_1000 if v is None)
    assert abs(first_nulls / 1000 - 0.20) <= 0.01

    first_non_null = [v for v in first_1000 if v is not None]
    l1 = sum(
        abs(weights[k] - (first_non_null.count(k) / len(first_non_null)))
        for k in weights
    )
    assert l1 <= 0.02


def test_fk_cycle_breaking_and_unbreakable_error() -> None:
    # Breakable cycle (one edge is nullable)
    profile_ok = {
        "tables": {
            "public.a": {"row_count": 50, "pk": ["id"], "columns": {"id": {}, "b_id": {}}},
            "public.b": {"row_count": 50, "pk": ["id"], "columns": {"id": {}, "a_id": {}}},
        },
        "foreign_keys": [
            {
                "name": "fk_a_b",
                "kind": "fk",
                "from": {"table": "public.a", "columns": ["b_id"]},
                "to": {"table": "public.b", "columns": ["id"]},
                "nullable": True,
            },
            {
                "name": "fk_b_a",
                "kind": "fk",
                "from": {"table": "public.b", "columns": ["a_id"]},
                "to": {"table": "public.a", "columns": ["id"]},
                "nullable": False,
            },
        ],
    }
    plan = build_generation_plan(profile_ok)
    assert set(plan.table_order) == {"public.a", "public.b"}
    assert len(plan.deferred_cycle_fks) == 1
    assert plan.deferred_cycle_fks[0]["name"] == "fk_a_b"

    # Unbreakable cycle (no nullable edge) -> must raise UnbreakableFKCycleError
    profile_bad = {
        "tables": profile_ok["tables"],
        "foreign_keys": [
            {**fk, "nullable": False} for fk in profile_ok["foreign_keys"]
        ],
    }
    with pytest.raises( UnbreakableFKCycleError):
        build_generation_plan(profile_bad)


def test_shorthand_dsn_parsing_and_password_guard() -> None:
    # Shorthand without scheme
    d1 = parse_dsn("postgres@localhost:5432/lms_prod", prompt_password=False)
    assert d1.user == "postgres"
    assert d1.host == "localhost"
    assert d1.port == "5432"
    assert d1.dbname == "lms_prod"
    assert d1.raw_dsn.startswith("postgresql://postgres")

    # Shorthand without port
    d2 = parse_dsn("ro_user@db.internal/lms_prod", prompt_password=False)
    assert d2.user == "ro_user"
    assert d2.host == "db.internal"
    assert d2.port == "5432"
    assert d2.dbname == "lms_prod"

    # Shorthand with embedded password must be caught by argv guard
    assert dsn_contains_password("postgres:secret123@localhost:5432/lms") is True
    with pytest.raises(ValueError, match="Security violation"):
        assert_argv_has_no_passwords(["clone", "postgres:secret123@localhost/lms"])


def test_simplified_clone_cli_modes_and_yaml(tmp_path: Path) -> None:
    parser = build_parser()

    # 1. `lab clone postgres@localhost:5432/prod_db lms_synth --mode inspect`
    args1 = parser.parse_args(["clone", "postgres@localhost:5432/prod_db", "lms_synth", "--mode", "inspect"])
    from_dsn1, out_dir1 = _resolve_clone_inputs(args1)
    assert from_dsn1 == "postgres@localhost:5432/prod_db"
    assert out_dir1 == Path("datasets/lms_synth")

    # 2. Single positional DSN auto-names dataset `<dbname>_synth`
    args2 = parser.parse_args(["clone", "postgres@localhost/hahlex_prod"])
    from_dsn2, out_dir2 = _resolve_clone_inputs(args2)
    assert from_dsn2 == "postgres@localhost/hahlex_prod"
    assert out_dir2 == Path("datasets/hahlex_prod_synth")

    # 3. Single positional dataset name for `--mode regen` / `--mode check`
    args3 = parser.parse_args(["clone", "lms_synth", "--mode", "check", "--ack", "qa-lead"])
    from_dsn3, out_dir3 = _resolve_clone_inputs(args3)
    assert from_dsn3 is None
    assert out_dir3 == Path("datasets/lms_synth")
    assert args3.ack == "qa-lead"

    # 4. `clone.yaml` template generation and auto-loading
    ds_dir = tmp_path / "lms_synth"
    ds_dir.mkdir(parents=True)
    dummy_profile = {
        "tables": {
            "public.course": {
                "columns": {
                    "status": {"categorical_candidate": True, "distinct_count": 3},
                    "card_type": {"categorical_vetoed": True, "pii_matched_pattern": "card", "distinct_count": 4},
                }
            }
        }
    }
    yaml_path = _write_clone_yaml_template(ds_dir, dummy_profile)
    assert yaml_path.is_file()
    yaml_text = yaml_path.read_text(encoding="utf-8")
    assert "public.course.status" in yaml_text
    assert "public.course.card_type" in yaml_text

    # Uncomment / write active config and verify _apply_clone_yaml_defaults merges it
    yaml_path.write_text(
        "allow_categorical:\n  - public.course.status\n"
        "override_pii_veto:\n  - public.course.card_type=payment category\n"
        "implicit_fk:\n  - public.payment(course_code)=public.course(code)\n",
        encoding="utf-8",
    )
    args4 = parser.parse_args(["clone", "postgres@localhost/prod_db", str(ds_dir)])
    _apply_clone_yaml_defaults(args4, ds_dir)
    assert "public.course.status" in args4.allow_categorical
    assert "public.course.card_type=payment category" in args4.override_pii_veto
    assert "public.payment(course_code)=public.course(code)" in args4.implicit_fk


def test_dotenv_loading_into_shorthand_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LAB_SOURCE_PASSWORD=from_dotenv_secret_77\n"
        "LAB_RO_PASSWORD=custom_ro_pass\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LAB_SOURCE_PASSWORD", raising=False)
    monkeypatch.delenv("LAB_RO_PASSWORD", raising=False)
    load_lab_dotenv(env_file)

    assert os.environ.get("LAB_SOURCE_PASSWORD") == "from_dotenv_secret_77"
    assert os.environ.get("LAB_RO_PASSWORD") == "custom_ro_pass"

    parsed = parse_dsn("ro_user@remote.db.internal/lms_prod", role_label="from", prompt_password=False)
    assert parsed.password == "from_dotenv_secret_77"


