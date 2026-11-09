"""CLI entrypoint for `python -m lab` (clone, profile, generate, load, validate, ack-degraded)."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import re
import sys
from typing import Any
import yaml

from lab import doctor
from lab.common.errors import LabError
from lab.ddl import _quote_fqn, _quote_ident, extract_ddl_artifacts, sanitize_ddl, write_ddl_files
from lab.dsn import assert_argv_has_no_passwords, load_lab_dotenv, parse_dsn, resolve_dsn
from lab.generate import generate_dataset
from lab.table_select import exclusion_regex, select_tables
from lab.introspect import introspect_source, print_candidates_report
from lab.load import load_dataset_to_target
from lab.plan import UnbreakableFKCycleError, build_generation_plan
from lab.validate import validate_dataset
from lab.writers import acknowledge_degraded_checks, write_dataset_artifacts


def _resolve_out_dir(raw_out: str) -> Path:
    """Resolve dataset name or path. Bare names (e.g. 'lms_synth') map to 'datasets/lms_synth'."""
    text = raw_out.strip()
    if "/" in text or "\\" in text or text.startswith("."):
        return Path(text)
    p = Path(text)
    if p.exists():
        return p
    return Path("datasets") / text


def _looks_like_dsn(text: str) -> bool:
    """Return True if a positional argument looks like a connection string (`user@host/db`, `host:5432/db`, `postgresql://...`)."""
    t = text.strip()
    if "://" in t or "@" in t or "=" in t:
        return True
    if re.match(r"^[a-zA-Z0-9._-]+:\d+/", t):
        return True
    if t.startswith("localhost/") or t.startswith("127.0.0.1/"):
        return True
    return False


def _write_clone_yaml_template(out_dir: Path, profile: dict[str, Any]) -> Path:
    """Write a user-friendly clone.yaml with discovered categorical candidates and vetoed columns."""
    yaml_path = out_dir / "clone.yaml"
    if yaml_path.is_file():
        return yaml_path

    candidates: list[str] = []
    vetoed: list[tuple[str, str]] = []
    tables = profile.get("tables") or {}
    for tbl_fqn in sorted(tables.keys()):
        cols = tables[tbl_fqn].get("columns") or {}
        for col_name, c_meta in cols.items():
            fqn = f"{tbl_fqn}.{col_name}"
            if c_meta.get("categorical_candidate"):
                candidates.append(f"#   - {fqn}  # {c_meta.get('distinct_count')} distinct values")
            elif c_meta.get("categorical_vetoed"):
                pat = c_meta.get("pii_matched_pattern") or "pii"
                vetoed.append((fqn, f"matched {pat!r} ({c_meta.get('distinct_count')} distinct values)"))

    lines = [
        "# Configuration for `python -m lab clone`",
        "# Uncomment the columns you approve after reviewing `--mode inspect`.",
        "",
        "scale: 0.1",
        "seed: 42",
        "",
        "allow_categorical:",
    ]
    if candidates:
        lines.extend(candidates)
    else:
        lines.append("  []")

    lines.extend([
        "",
        "override_pii_veto:",
    ])
    if vetoed:
        for fqn, note in vetoed:
            lines.append(f"#   - \"{fqn}=<state reason here>\"  # {note}")
    else:
        lines.append("  []")

    lines.extend([
        "",
        "implicit_fk:",
        "#   - \"public.child_table(col)=public.parent_table(col)\"",
        "",
    ])
    yaml_path.write_text("\n".join(lines), encoding="utf-8")
    return yaml_path


def _apply_clone_yaml_defaults(args: argparse.Namespace, out_dir: Path) -> None:
    """Merge settings from `<out_dir>/clone.yaml` if present (CLI flags take precedence or append)."""
    yaml_path = out_dir / "clone.yaml"
    if not yaml_path.is_file():
        return
    try:
        cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(cfg, dict):
        return

    for key, attr in (
        ("allow_categorical", "allow_categorical"),
        ("override_pii_veto", "override_pii_veto"),
        ("implicit_fk", "implicit_fk"),
        ("pii_extra", "pii_extra"),
    ):
        vals = cfg.get(key)
        if isinstance(vals, list):
            current = getattr(args, attr, None) or []
            for item in vals:
                if item and str(item) not in current:
                    current.append(str(item))
            setattr(args, attr, current)

    if not getattr(args, "exclude", None) and cfg.get("exclude"):
        args.exclude = str(cfg["exclude"])



def _flatten_multi_arg(items: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in items or []:
        # Do not split on commas inside parentheses like child(c1,c2)=parent(p1,p2)
        if "(" in raw:
            out.append(raw.strip())
        else:
            for part in raw.split(","):
                if part.strip():
                    out.append(part.strip())
    return out


def _strip_degraded_from_existing_02_tables(
    ddl_02_path: Path,
    degraded_set: set[tuple[str, str]],
) -> None:
    """Remove degraded CHECK constraints from an existing ddl/02_tables.sql file."""
    if not ddl_02_path.is_file() or not degraded_set:
        return
    lines = ddl_02_path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    for line in lines:
        drop_line = False
        for tbl_fqn, chk_name in degraded_set:
            q_tbl = _quote_fqn(tbl_fqn)
            q_chk = _quote_ident(chk_name)
            if f"ALTER TABLE {q_tbl} ADD CONSTRAINT {q_chk} " in line:
                drop_line = True
                break
        if not drop_line:
            kept.append(line)
    ddl_02_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _print_validation_summary(report: dict[str, Any]) -> None:
    print(f"\n=== Validation Report: {report.get('dataset')} (status: {report.get('overall_status')}, exit_code: {report.get('exit_code')}) ===")
    checks = report.get("checks") or {}
    for name, res in checks.items():
        status = res.get("status", "unknown").upper()
        violations = res.get("violations") or []
        print(f"  [{status:7s}] {name}")
        for v in violations[:5]:
            print(f"            - {v}")
        if len(violations) > 5:
            print(f"            - ... and {len(violations) - 5} more violation(s)")

    degraded = report.get("degraded") or []
    if degraded:
        ack_str = "ACKNOWLEDGED" if report.get("degraded_acknowledged") else "UNACKNOWLEDGED"
        print(f"\n  Degraded CHECK constraints ({len(degraded)}) [{ack_str}]:")
        for d in degraded:
            print(
                f"    - {d.get('table')}.{d.get('constraint')}: "
                f"failed={d.get('rows_failed')}, repaired={d.get('rows_repaired')}, "
                f"rate={d.get('repair_rate')} ({d.get('reason')})"
            )
    print()


def _cmd_profile(args: argparse.Namespace) -> int:
    source_dsn = resolve_dsn(env_var=args.from_env, direct_dsn=args.from_dsn, role_label="from")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    profile = introspect_source(
        source_dsn,
        allow_categorical=_flatten_multi_arg(args.allow_categorical),
        pii_extra=_flatten_multi_arg(args.pii_extra),
        override_pii_veto=args.override_pii_veto or [],
        implicit_fks=args.implicit_fk or [],
        exclude_regex=args.exclude,
    )
    (out_dir / "profile.json").write_text(
        json.dumps(profile, indent=2) + "\n",
        encoding="utf-8",
    )

    ddl_artifacts = extract_ddl_artifacts(
        source_dsn,
        excluded_tables=profile.get("excluded_tables") or [],
    )
    write_ddl_files(out_dir / "ddl", ddl_artifacts)
    print_candidates_report(profile)
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    # `lab clone --mode regen` shares this path, and its parser has no --profile option.
    profile_arg = getattr(args, "profile", None)
    profile_path = Path(profile_arg) if profile_arg else (out_dir / "profile.json")
    if not profile_path.is_file():
        print(f"Error: profile.json not found at {profile_path}", file=sys.stderr)
        return 1

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    scratch_dsn = None
    if args.to_env or args.to_dsn:
        scratch_dsn = resolve_dsn(env_var=args.to_env, direct_dsn=args.to_dsn, role_label="to")

    try:
        plan = build_generation_plan(profile, scale=args.scale, max_rows=args.max_rows)
        gen_result = generate_dataset(
            profile,
            scale=args.scale,
            seed=args.seed,
            max_rows=args.max_rows,
            locale=args.locale,
            check_max_attempts=args.check_max_attempts,
            check_min_repair_rate=args.check_min_repair_rate,
            scratch_dsn=scratch_dsn,
        )
    except UnbreakableFKCycleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if out_dir.resolve() != profile_path.parent.resolve():
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "profile.json").write_text(
            json.dumps(profile, indent=2) + "\n",
            encoding="utf-8",
        )

    degraded_set = {
        (str(d["table"]), str(d["constraint"])) for d in gen_result.degraded_checks
    }
    _strip_degraded_from_existing_02_tables(out_dir / "ddl" / "02_tables.sql", degraded_set)

    write_dataset_artifacts(
        out_dir,
        profile,
        gen_result,
        plan.table_order,
        dataset_name=out_dir.name,
        seed=args.seed,
        scale=args.scale,
    )
    return 2 if gen_result.degraded_checks else 0


def _cmd_load(args: argparse.Namespace) -> int:
    target_dsn = resolve_dsn(env_var=args.to_env, direct_dsn=args.to_dsn, role_label="to")
    out_dir = Path(args.out)
    load_dataset_to_target(target_dsn, out_dir, drop=args.drop)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    target_dsn = resolve_dsn(env_var=args.to_env, direct_dsn=args.to_dsn, role_label="to")
    out_dir = Path(args.out)
    exit_code, report = validate_dataset(
        target_dsn,
        out_dir,
        scale=args.scale,
        seed=args.seed,
        max_rows=args.max_rows,
        locale=args.locale,
        check_determinism=not args.no_determinism_check,
    )
    _print_validation_summary(report)
    return exit_code


def _cmd_ack_degraded(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    ack = acknowledge_degraded_checks(out_dir, args.reviewer)
    degraded = ack.get("degraded") or []
    print(f"Acknowledged {len(degraded)} degraded CHECK constraint(s) for dataset {ack['dataset']!r}:")
    for d in degraded:
        print(f"  - {d.get('table')}.{d.get('constraint')}: {d.get('reason')}")
    print(f"Reviewer: {ack['reviewer']} | Hash: {ack['degraded_sha256']}")
    return 2 if degraded else 0


def _resolve_clone_inputs(args: argparse.Namespace) -> tuple[str | None, Path]:
    """Resolve optional positional [source] and [dataset] arguments alongside --from and --out."""
    pos: list[str] = list(getattr(args, "targets", None) or [])
    from_dsn: str | None = getattr(args, "from_dsn", None)
    raw_out: str | None = getattr(args, "out", None)

    if len(pos) > 2:
        raise ValueError(f"Too many positional arguments for clone: {pos!r}. Expected '[source] [dataset]'.")
    if len(pos) == 2:
        if not from_dsn:
            from_dsn = pos[0]
        if not raw_out:
            raw_out = pos[1]
    elif len(pos) == 1:
        item = pos[0]
        if raw_out and not from_dsn:
            from_dsn = item
        elif from_dsn and not raw_out:
            raw_out = item
        elif _looks_like_dsn(item):
            from_dsn = item
        else:
            raw_out = item

    if not raw_out:
        if from_dsn or getattr(args, "from_env", None):
            src_parsed = resolve_dsn(
                env_var=getattr(args, "from_env", None),
                direct_dsn=from_dsn,
                role_label="from",
                prompt_password=False,
            )
            raw_out = f"{src_parsed.dbname}_synth"
        else:
            raise ValueError(
                "Missing dataset name or output directory. "
                "Usage: python -m lab clone [user@host/db] <dataset> [--mode {full,inspect,regen,check}]"
            )

    return from_dsn, _resolve_out_dir(raw_out)


def _cmd_clone(args: argparse.Namespace) -> int:
    from_dsn, out_dir = _resolve_clone_inputs(args)
    args.from_dsn = from_dsn
    args.out = str(out_dir)

    mode: str = getattr(args, "mode", "full") or "full"
    if getattr(args, "profile_only", False):
        mode = "inspect"

    # Mode 4: check / ack
    if mode == "check":
        if getattr(args, "ack", None):
            ack = acknowledge_degraded_checks(out_dir, args.ack)
            degraded = ack.get("degraded") or []
            print(f"Acknowledged {len(degraded)} degraded CHECK constraint(s) for dataset {ack['dataset']!r}:")
            for d in degraded:
                print(f"  - {d.get('table')}.{d.get('constraint')}: {d.get('reason')}")
            print(f"Reviewer: {ack['reviewer']} | Hash: {ack['degraded_sha256']}")

        target_dsn = resolve_dsn(
            env_var=args.to_env,
            direct_dsn=args.to_dsn,
            role_label="to",
            default_dbname=out_dir.name,
        )
        exit_code, report = validate_dataset(
            target_dsn,
            out_dir,
            scale=args.scale,
            seed=args.seed,
            max_rows=args.max_rows,
            locale=args.locale,
            check_determinism=not args.no_determinism_check,
        )
        _print_validation_summary(report)
        return exit_code

    # Mode 3: regen (offline regeneration from existing profile.json -> load -> validate)
    if mode == "regen":
        rc = _cmd_generate(args)
        if rc == 1 or args.dry_run:
            return rc
        target_dsn = resolve_dsn(
            env_var=args.to_env,
            direct_dsn=args.to_dsn,
            role_label="to",
            default_dbname=out_dir.name,
        )
        load_dataset_to_target(target_dsn, out_dir, drop=args.drop)
        exit_code, report = validate_dataset(
            target_dsn,
            out_dir,
            scale=args.scale,
            seed=args.seed,
            max_rows=args.max_rows,
            locale=args.locale,
            check_determinism=not args.no_determinism_check,
        )
        _print_validation_summary(report)
        return exit_code

    # Mode 1 (inspect) & Mode 2 (full)
    _apply_clone_yaml_defaults(args, out_dir)
    source_dsn = resolve_dsn(env_var=args.from_env, direct_dsn=args.from_dsn, role_label="from")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Introspect source DB (read-only) and extract DDL
    profile = introspect_source(
        source_dsn,
        allow_categorical=_flatten_multi_arg(args.allow_categorical),
        pii_extra=_flatten_multi_arg(args.pii_extra),
        override_pii_veto=args.override_pii_veto or [],
        implicit_fks=args.implicit_fk or [],
        exclude_regex=args.exclude,
    )
    if args.auto_select:
        selection = select_tables(profile)
        if not selection.kept:
            print("Error: --auto-select found no table with data.", file=sys.stderr)
            return 1
        print(selection.summary())
        for table in sorted(selection.kept):
            print(f"  {table}")
        # Profile again so tables, foreign keys and DDL all agree on the selection.
        profile = introspect_source(
            source_dsn,
            allow_categorical=_flatten_multi_arg(args.allow_categorical),
            pii_extra=_flatten_multi_arg(args.pii_extra),
            override_pii_veto=args.override_pii_veto or [],
            implicit_fks=args.implicit_fk or [],
            exclude_regex=exclusion_regex(selection, args.exclude),
        )
    (out_dir / "profile.json").write_text(
        json.dumps(profile, indent=2) + "\n",
        encoding="utf-8",
    )

    ddl_artifacts = extract_ddl_artifacts(
        source_dsn,
        excluded_tables=profile.get("excluded_tables") or [],
    )
    write_ddl_files(out_dir / "ddl", ddl_artifacts)
    yaml_path = _write_clone_yaml_template(out_dir, profile)

    if mode == "inspect":
        print_candidates_report(profile)
        print(f"Config template ready at: {yaml_path}")
        return 0

    target_dsn = None
    if not args.dry_run:
        target_dsn = resolve_dsn(
            env_var=args.to_env,
            direct_dsn=args.to_dsn,
            role_label="to",
            default_dbname=out_dir.name,
        )
    elif args.to_env or args.to_dsn:
        target_dsn = resolve_dsn(
            env_var=args.to_env,
            direct_dsn=args.to_dsn,
            role_label="to",
            default_dbname=out_dir.name,
        )

    # 2. Generate synthetic rows strictly from profile.json (never connects to source_dsn)
    try:
        plan = build_generation_plan(profile, scale=args.scale, max_rows=args.max_rows)
        gen_result = generate_dataset(
            profile,
            scale=args.scale,
            seed=args.seed,
            max_rows=args.max_rows,
            locale=args.locale,
            check_max_attempts=args.check_max_attempts,
            check_min_repair_rate=args.check_min_repair_rate,
            scratch_dsn=target_dsn,
        )
    except UnbreakableFKCycleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Rewrite DDL excluding any degraded CHECK constraints
    degraded_set = {
        (str(d["table"]), str(d["constraint"])) for d in gen_result.degraded_checks
    }
    write_ddl_files(out_dir / "ddl", ddl_artifacts, degraded_checks=degraded_set)

    # 3. Write CSVs, data/_load.sql, manifest.json, .lab-dataset
    write_dataset_artifacts(
        out_dir,
        profile,
        gen_result,
        plan.table_order,
        dataset_name=out_dir.name,
        seed=args.seed,
        scale=args.scale,
    )

    if args.dry_run:
        return 2 if gen_result.degraded_checks else 0

    assert target_dsn is not None
    # 4. Load into target DB
    load_dataset_to_target(target_dsn, out_dir, drop=args.drop)

    # 5. Validate target DB against profile.json
    exit_code, report = validate_dataset(
        target_dsn,
        out_dir,
        scale=args.scale,
        seed=args.seed,
        max_rows=args.max_rows,
        locale=args.locale,
        check_determinism=not args.no_determinism_check,
    )
    _print_validation_summary(report)
    if getattr(args, "ack", None) and report.get("degraded"):
        ack = acknowledge_degraded_checks(out_dir, args.ack)
        print(f"Acknowledged {len(ack.get('degraded') or [])} degraded CHECK(s) by reviewer {ack['reviewer']!r}.")
    return exit_code


def _add_common_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--from-env", help="Environment variable holding the read-only source PostgreSQL DSN (default: LAB_SOURCE_DSN).")
    p.add_argument("--from", dest="from_dsn", help="Passwordless source DSN or shorthand (e.g. 'postgres@localhost:5432/mydb').")
    p.add_argument("--allow-categorical", action="append", default=[], help="Allowlist 'schema.table.col' to read GROUP BY frequencies.")
    p.add_argument("--pii-extra", action="append", default=[], help="Additional regex pattern(s) treated as PII column names.")
    p.add_argument("--override-pii-veto", action="append", default=[], help="Override PII veto with mandatory reason: 'schema.table.col=<reason>'.")
    p.add_argument("--implicit-fk", action="append", default=[], help="Declare implicit FK: 'schema.child(col)=schema.parent(col)'.")
    p.add_argument("--exclude", help="Additional regex of table names to exclude.")


def _add_common_gen_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scale", type=float, default=0.1, help="Row scaling factor (default: 0.1).")
    p.add_argument("--seed", type=int, default=42, help="Deterministic RNG seed (default: 42).")
    p.add_argument("--max-rows", type=int, default=50_000, help="Maximum rows per table (default: 50000).")
    p.add_argument("--locale", default="en_US", help="Faker locale for synthetic text (default: en_US).")
    p.add_argument("--check-max-attempts", type=int, default=20, help="Max repair attempts per CHECK constraint (default: 20).")
    p.add_argument("--check-min-repair-rate", type=float, default=0.20, help="Minimum repair rate before dropping a CHECK as degraded (default: 0.20).")


def _add_common_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--to-env", help="Environment variable holding the target PostgreSQL DSN (default: LAB_TARGET_DSN).")
    p.add_argument("--to", dest="to_dsn", help="Passwordless target DSN or shorthand (e.g. 'postgres@localhost:5432/mydb').")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lab",
        description="Auto-Ontology Lab CLI",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # clone
    p_clone = sub.add_parser(
        "clone",
        help="Clone a source PostgreSQL DB into a privacy-preserving synthetic DB.",
    )
    p_clone.add_argument(
        "targets",
        nargs="*",
        help="Optional positional '[source] [dataset]' (e.g. 'postgres@localhost:5432/prod_db lms_synth').",
    )
    p_clone.add_argument(
        "-m",
        "--mode",
        choices=["full", "inspect", "regen", "check"],
        default="full",
        help="Execution mode: 'full' (default: profile+generate+load+validate), 'inspect' (profile & write clone.yaml), 'regen' (offline regenerate+load+validate), 'check' (validate / ack).",
    )
    _add_common_source_args(p_clone)
    _add_common_target_args(p_clone)
    _add_common_gen_args(p_clone)
    p_clone.add_argument("--out", help="Output dataset name or directory (e.g. 'lms_synth' or 'datasets/lms_synth').")
    p_clone.add_argument("--ack", metavar="REVIEWER", help="Acknowledge degraded CHECK constraints under this reviewer name.")
    p_clone.add_argument("--profile-only", action="store_true", help="Alias for '--mode inspect'.")
    p_clone.add_argument(
        "--auto-select",
        action="store_true",
        help="Keep only tables with data and the parents their foreign keys need. Report this with any result.",
    )
    p_clone.add_argument("--dry-run", action="store_true", help="Profile and generate files without loading into target DB.")
    p_clone.add_argument("--drop", action="store_true", help="Drop existing target database (only if it carries the lab_synth marker).")
    p_clone.add_argument("--no-determinism-check", action="store_true", help="Skip subprocess determinism re-run during validation.")

    # profile
    p_prof = sub.add_parser("profile", help="Introspect source DB into profile.json and ddl/.")
    _add_common_source_args(p_prof)
    p_prof.add_argument("--out", required=True, help="Output dataset directory.")

    # generate
    p_gen = sub.add_parser("generate", help="Generate synthetic data from profile.json without connecting to source.")
    p_gen.add_argument("--profile", help="Path to profile.json (defaults to <out>/profile.json).")
    p_gen.add_argument("--out", required=True, help="Output dataset directory.")
    _add_common_target_args(p_gen)
    _add_common_gen_args(p_gen)

    # load
    p_load = sub.add_parser("load", help="Load a generated synthetic dataset into a target PostgreSQL DB.")
    _add_common_target_args(p_load)
    p_load.add_argument("--out", required=True, help="Dataset directory to load.")
    p_load.add_argument("--drop", action="store_true", help="Drop existing target database if it carries the lab_synth marker.")

    # validate
    p_val = sub.add_parser("validate", help="Run acceptance checks A1-A9, A11-A13 on a loaded target DB.")
    _add_common_target_args(p_val)
    _add_common_gen_args(p_val)
    p_val.add_argument("--out", required=True, help="Dataset directory.")
    p_val.add_argument("--no-determinism-check", action="store_true", help="Skip subprocess determinism check.")

    # ack-degraded
    p_ack = sub.add_parser("ack-degraded", help="Acknowledge degraded CHECK constraints after human review (§1.5).")
    p_ack.add_argument("--out", required=True, help="Dataset directory.")
    p_ack.add_argument("--reviewer", required=True, help="Name or identifier of the human reviewer.")

    # doctor
    p_doc = sub.add_parser("doctor", help="Report whether the stack matches the frozen environment.")
    doctor.add_arguments(p_doc)

    # import (Step 2: lab.importer.cli)
    imp_mod = _try_import_cmd_module("lab.importer.cli")
    if imp_mod is not None:
        p_imp = sub.add_parser("import", help="Load a dataset into the stack, compile it, and save the T0 snapshot.")
        imp_mod.add_arguments(p_imp)

    # ask (Step 2/4: lab.ask)
    ask_mod = _try_import_cmd_module("lab.ask")
    if ask_mod is not None:
        p_ask = sub.add_parser("ask", help="Run probe or benchmark questions through the gateway.")
        ask_mod.add_arguments(p_ask)

    # measure (Step 2: lab.measure.cli)
    measure_mod = _try_import_cmd_module("lab.measure.cli")
    if measure_mod is not None:
        p_measure = sub.add_parser("measure", help="Run the discovery, Text-to-SQL, evolution and audit suites from T0.")
        measure_mod.add_arguments(p_measure)

    # report (Step 3: lab.report)
    report_mod = _try_import_cmd_module("lab.report")
    if report_mod is not None:
        p_report = sub.add_parser("report", help="Write the dataset's results file from its newest run of each suite.")
        report_mod.add_arguments(p_report)

    return parser


def _try_import_cmd_module(mod_name: str) -> Any | None:
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == mod_name or mod_name.startswith(exc.name + ".")):
            return None
        raise
    if hasattr(mod, "add_arguments") and hasattr(mod, "run"):
        return mod
    return None


def main(argv: list[str] | None = None) -> int:
    load_lab_dotenv()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_argv_has_no_passwords(raw_argv)
        parser = build_parser()
        args = parser.parse_args(raw_argv)

        if args.subcommand == "clone":
            return _cmd_clone(args)
        if args.subcommand == "profile":
            return _cmd_profile(args)
        if args.subcommand == "generate":
            return _cmd_generate(args)
        if args.subcommand == "load":
            return _cmd_load(args)
        if args.subcommand == "validate":
            return _cmd_validate(args)
        if args.subcommand == "ack-degraded":
            return _cmd_ack_degraded(args)
        if args.subcommand == "doctor":
            return doctor.run(args)
        if args.subcommand == "import":
            imp_mod = _try_import_cmd_module("lab.importer.cli")
            if imp_mod is not None:
                return int(imp_mod.run(args))
        if args.subcommand == "ask":
            ask_mod = _try_import_cmd_module("lab.ask")
            if ask_mod is not None:
                return int(ask_mod.run(args))
        if args.subcommand == "measure":
            measure_mod = _try_import_cmd_module("lab.measure.cli")
            if measure_mod is not None:
                return int(measure_mod.run(args))
        if args.subcommand == "report":
            report_mod = _try_import_cmd_module("lab.report")
            if report_mod is not None:
                return int(report_mod.run(args))
        return 1
    except (ValueError, PermissionError, FileNotFoundError, LabError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
