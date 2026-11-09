from __future__ import annotations

from datetime import datetime, timezone

from lab.doctor import (
    FAIL,
    PASS,
    WARN,
    check_alembic_head,
    check_embed_match,
    check_env_file,
    check_frozen_models,
    check_image_age,
    check_next_runs,
    check_other_connections,
    check_token_role,
    check_upstream_commit,
    exit_code,
    Check,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def test_upstream_commit_mismatch_fails_and_shows_both() -> None:
    check = check_upstream_commit("c4495670aaaa", "c447003f72a8")
    assert check.status == FAIL
    assert "c4495670" in check.detail and "c447003f" in check.detail


def test_upstream_commit_match_passes() -> None:
    assert check_upstream_commit("abc", "abc").status == PASS


def test_image_built_before_the_pinned_commit_fails() -> None:
    # The stack runs an image, so a checkout at the pin proves nothing about what is running.
    check = check_image_age("2026-11-06T15:33:00Z", "2026-11-06T10:45:57-06:00", "abc12345")
    assert check.status == FAIL
    assert "abc12345" in check.detail


def test_image_built_after_the_pinned_commit_warns_because_it_cannot_prove_the_source() -> None:
    check = check_image_age("2026-11-07T01:00:00.123456789Z", "2026-11-06T10:45:57-06:00", "abc12345")
    assert check.status == WARN


def test_revision_label_matching_the_pin_passes_even_with_any_timestamp() -> None:
    check = check_image_age("2026-11-07T01:00:00Z", "2026-11-06T10:45:57-06:00", "abc12345", revision="abc12345")
    assert check.status == PASS


def test_revision_label_for_another_commit_fails() -> None:
    check = check_image_age("2026-11-07T01:00:00Z", "2026-11-06T10:45:57-06:00", "abc12345", revision="def67890")
    assert check.status == FAIL
    assert "def67890" in check.detail


def test_image_age_with_unknown_commit_fails() -> None:
    assert check_image_age("2026-11-07T01:00:00Z", None, "abc12345").status == FAIL


def test_alembic_head_mismatch_fails() -> None:
    assert check_alembic_head("a", "b").status == FAIL
    assert check_alembic_head("a", "a").status == PASS


def test_env_file_flags_connection_strings_without_printing_them() -> None:
    check = check_env_file("CONNECTION_STRINGS=postgresql://u:secretpw@h/db\nDEFAULT_MODELS_API_KEY=nvapi-real\n")
    assert check.status == FAIL
    assert "CONNECTION_STRINGS" in check.detail
    assert "secretpw" not in check.detail


def test_env_file_flags_placeholder_api_keys_without_printing_real_keys() -> None:
    check = check_env_file("CONNECTION_STRINGS=\nREASONING_API_KEY=nvapi-your-api-key-here\nEMBED_API_KEY=nvapi-real-secret\n")
    assert check.status == FAIL
    assert "REASONING_API_KEY" in check.detail
    assert "nvapi-real-secret" not in check.detail


def test_env_file_clean_passes() -> None:
    assert check_env_file("CONNECTION_STRINGS=\nREASONING_API_KEY=nvapi-real\n").status == PASS


def test_frozen_models_blank_fails_and_names_role() -> None:
    frozen = {"reasoning": {"model": ""}, "embed": {"model": "e"}}
    check = check_frozen_models(frozen, {"reasoning": "r", "embed": "e"})
    assert check.status == FAIL
    assert "reasoning" in check.detail


def test_frozen_models_differing_from_running_fails() -> None:
    check = check_frozen_models({"embed": {"model": "a"}}, {"embed": "b"})
    assert check.status == FAIL
    assert "a" in check.detail and "b" in check.detail


def test_frozen_models_matching_passes() -> None:
    assert check_frozen_models({"embed": {"model": "a"}}, {"embed": "a"}).status == PASS


def test_embed_model_must_match_between_services() -> None:
    assert check_embed_match("a", "b").status == FAIL
    assert check_embed_match("a", "a").status == PASS


def test_next_run_inside_window_warns_because_it_would_mutate_mid_measurement() -> None:
    logs = (
        "INFO x: semantic: next run at 2026-11-07T14:00:00+00:00\n"
        "INFO x: ingest: next run at 2026-11-08T14:00:00+00:00\n"
    )
    check = check_next_runs(logs, NOW, window_hours=8)
    assert check.status == WARN
    assert "semantic" in check.detail
    assert "ingest" not in check.detail


def test_next_run_uses_latest_line_per_scheduler() -> None:
    logs = (
        "INFO x: semantic: next run at 2026-11-07T14:00:00+00:00\n"
        "INFO x: semantic: next run at 2026-11-09T14:00:00+00:00\n"
    )
    assert check_next_runs(logs, NOW, window_hours=8).status == PASS


def test_other_connections_warn_because_compile_passes_touch_all_of_them() -> None:
    check = check_other_connections(["hahlex_edu"])
    assert check.status == WARN
    assert "hahlex_edu" in check.detail
    assert check_other_connections([]).status == PASS


def test_token_role_expected_statuses() -> None:
    assert check_token_role("admin", 200).status == PASS
    assert check_token_role("viewer", 403).status == PASS
    # A viewer token that can read admin settings would invalidate the production-path measurement.
    assert check_token_role("viewer", 200).status == FAIL
    assert check_token_role("admin", 401).status == FAIL
    assert check_token_role("admin", None).status == FAIL


def test_exit_code_only_fails_on_fail() -> None:
    assert exit_code([Check("a", PASS, ""), Check("b", WARN, "")]) == 0
    assert exit_code([Check("a", PASS, ""), Check("b", FAIL, "")]) == 1
