"""End-to-end acceptance suite (A1–A13) on fixtures/accept.sql + accept_seed.sql (§2)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import psycopg
import pytest

from lab.dsn import parse_dsn
from lab.load import ensure_target_database
from scripts.import_dataset import AUTO_ONTOLOGY_ENV, parse_env_file, run_psql


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
ACCEPT_SQL = FIXTURES_DIR / "accept.sql"
ACCEPT_SEED_SQL = FIXTURES_DIR / "accept_seed.sql"


def _get_local_pg_creds() -> tuple[str, str]:
    env = parse_env_file(AUTO_ONTOLOGY_ENV)
    user = env.get("POSTGRES_USER", "postgres")
    password = env.get("POSTGRES_PASSWORD", "change-me")
    return user, password


@pytest.fixture(scope="module")
def acceptance_clone_env(tmp_path_factory: pytest.TempPathFactory):
    """Set up source fixture DB, run `lab clone`, and yield paths + validation report."""
    pg_user, pg_pass = _get_local_pg_creds()
    src_db = "lab_pytest_accept_src"
    tgt_db = "lab_pytest_accept_tgt"
    unmarked_db = "lab_pytest_unmarked_db"

    # 1. Create source fixture DB and unmarked DB
    run_psql("postgres", pg_user, "postgres", command=f"DROP DATABASE IF EXISTS {src_db} WITH (FORCE);")
    run_psql("postgres", pg_user, "postgres", command=f"DROP DATABASE IF EXISTS {tgt_db} WITH (FORCE);")
    run_psql("postgres", pg_user, "postgres", command=f"DROP DATABASE IF EXISTS {unmarked_db} WITH (FORCE);")
    run_psql("postgres", pg_user, "postgres", command=f"CREATE DATABASE {src_db};")
    run_psql("postgres", pg_user, "postgres", command=f"CREATE DATABASE {unmarked_db};")

    run_psql(
        "postgres",
        pg_user,
        src_db,
        stdin_sql=ACCEPT_SQL.read_text(encoding="utf-8"),
        single_transaction=True,
    )
    run_psql(
        "postgres",
        pg_user,
        src_db,
        stdin_sql=ACCEPT_SEED_SQL.read_text(encoding="utf-8"),
        single_transaction=True,
    )

    out_dir = tmp_path_factory.mktemp("accept_out") / "accept_synth"
    src_dsn = f"postgresql://{pg_user}:{pg_pass}@localhost:5432/{src_db}"
    tgt_dsn = f"postgresql://{pg_user}:{pg_pass}@localhost:5432/{tgt_db}"
    unmarked_dsn = f"postgresql://{pg_user}:{pg_pass}@localhost:5432/{unmarked_db}"

    yield {
        "out_dir": out_dir,
        "src_dsn": src_dsn,
        "tgt_dsn": tgt_dsn,
        "unmarked_dsn": unmarked_dsn,
        "pg_user": pg_user,
    }

    # Cleanup
    run_psql("postgres", pg_user, "postgres", command=f"DROP DATABASE IF EXISTS {src_db} WITH (FORCE);")
    run_psql("postgres", pg_user, "postgres", command=f"DROP DATABASE IF EXISTS {tgt_db} WITH (FORCE);")
    run_psql("postgres", pg_user, "postgres", command=f"DROP DATABASE IF EXISTS {unmarked_db} WITH (FORCE);")
    shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_profile_only_prints_candidates_and_leaks_no_canary(acceptance_clone_env, tmp_path: Path) -> None:
    prof_dir = tmp_path / "prof_only"
    child_env = {
        **os.environ,
        "LAB_SOURCE_DSN": acceptance_clone_env["src_dsn"],
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "lab",
            "clone",
            "--from-env",
            "LAB_SOURCE_DSN",
            "--out",
            str(prof_dir),
            "--profile-only",
        ],
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "public.subscription_tier.tier_level" in proc.stdout
    assert "public.activity_event.event_kind" in proc.stdout
    assert "public.payment.card_type" in proc.stdout  # listed under Vetoed
    assert "CANARY_7f3a" not in proc.stdout
    assert "987654321012" not in proc.stdout
    assert "1901-02-03" not in proc.stdout

    profile_text = (prof_dir / "profile.json").read_text(encoding="utf-8")
    assert "CANARY_7f3a" not in profile_text
    assert "987654321012" not in profile_text
    assert "1901-02-03" not in profile_text


def test_drop_refuses_unmarked_database(acceptance_clone_env) -> None:
    unmarked_parsed = parse_dsn(acceptance_clone_env["unmarked_dsn"])
    with pytest.raises(PermissionError, match="Refusing to --drop database"):
        ensure_target_database(unmarked_parsed, "accept_synth", drop=True)


def test_full_clone_passes_acceptance_checks_a1_to_a13(acceptance_clone_env) -> None:
    out_dir: Path = acceptance_clone_env["out_dir"]
    child_env = {
        **os.environ,
        "LAB_SOURCE_DSN": acceptance_clone_env["src_dsn"],
        "LAB_TARGET_DSN": acceptance_clone_env["tgt_dsn"],
        "LAB_RO_PASSWORD": "ro_secret_test_991",
    }
    cmd = [
        sys.executable,
        "-m",
        "lab",
        "clone",
        "--from-env",
        "LAB_SOURCE_DSN",
        "--to-env",
        "LAB_TARGET_DSN",
        "--out",
        str(out_dir),
        "--scale",
        "0.1",
        "--seed",
        "42",
        "--allow-categorical",
        "public.subscription_tier.tier_level",
        "--allow-categorical",
        "public.activity_event.event_kind",
        "--allow-categorical",
        "public.payment.card_type",
        "--override-pii-veto",
        "public.payment.card_type=payment network category",
        "--implicit-fk",
        "public.payment(course_code)=public.course(code)",
        "--drop",
    ]
    proc = subprocess.run(cmd, env=child_env, text=True, capture_output=True)
    # Exit code 2 is expected because ck_payment_unsatisfiable_hash is deliberately unsatisfiable (§1.5, §2.1)
    assert proc.returncode == 2, f"Unexpected exit {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"

    report = json.loads((out_dir / "validate_report.json").read_text(encoding="utf-8"))
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest.get("row_counts"), dict) and len(manifest["row_counts"]) == 15
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in manifest["row_counts"].items())
    for fn in ("profile.json", "manifest.json", "validate_report.json", ".lab-dataset"):
        assert "ro_secret_test_991" not in (out_dir / fn).read_text(encoding="utf-8")
    assert report["overall_status"] == "degraded"
    assert report["exit_code"] == 2

    checks = report["checks"]
    for chk_name, chk_res in checks.items():
        assert chk_res["status"] == "pass", f"{chk_name} failed: {chk_res['violations']}"

    # Verify degraded contains only the deliberately unsatisfiable CHECK
    degraded = report["degraded"]
    assert len(degraded) == 1
    assert degraded[0]["table"] == "public.payment"
    assert degraded[0]["constraint"] == "ck_payment_unsatisfiable_hash"

    # Verify repaired_checks contains the repairable CHECK on course (A13)
    repaired = report["repaired_checks"]
    assert any(
        r["table"] == "public.course" and r["constraint"] == "ck_course_seat_gap" and r["repair_rate"] == 1.0
        for r in repaired
    )

    # Test ack-degraded workflow (§1.5)
    assert report["degraded_acknowledged"] is False
    ack_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "lab",
            "ack-degraded",
            "--out",
            str(out_dir),
            "--reviewer",
            "qa-engineer",
        ],
        text=True,
        capture_output=True,
    )
    assert ack_proc.returncode == 2
    assert (out_dir / "degraded_ack.json").is_file()

    # Re-validate and confirm degraded_acknowledged is now True
    val_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "lab",
            "validate",
            "--to-env",
            "LAB_TARGET_DSN",
            "--out",
            str(out_dir),
            "--scale",
            "0.1",
            "--seed",
            "42",
            "--no-determinism-check",
        ],
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert val_proc.returncode == 2
    updated_report = json.loads((out_dir / "validate_report.json").read_text(encoding="utf-8"))
    assert updated_report["degraded_acknowledged"] is True
