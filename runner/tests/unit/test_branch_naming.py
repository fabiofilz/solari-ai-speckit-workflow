"""Unit tests for PascalCase derivation and branch/tag naming (T012)."""

from __future__ import annotations

import pytest

from solari_workflow.git.branch import (
    build_branch_name,
    build_checkpoint_tag_name,
    parse_branch_name,
    parse_checkpoint_tag_name,
    to_pascal_case,
    to_slug,
)

# (input, expected_pascal_case) — fallback_task_id is always "091" here.
PASCAL_CASE_TABLE = [
    ("validation pipeline", "ValidationPipeline"),
    ("Validation Pipeline", "ValidationPipeline"),
    ("validation-pipeline", "ValidationPipeline"),
    ("validation_pipeline", "ValidationPipeline"),
    ("validation!! pipeline??", "ValidationPipeline"),
    ("API integration", "APIIntegration"),
    ("CI setup", "CISetup"),
    ("café résumé", "CafeResume"),
    ("Über setup", "UberSetup"),
    ("  leading and trailing  ", "LeadingAndTrailing"),
    ("single", "Single"),
    ("a", "A"),
]


@pytest.mark.parametrize("raw_name,expected", PASCAL_CASE_TABLE)
def test_pascal_case_table(raw_name: str, expected: str) -> None:
    assert to_pascal_case(raw_name, fallback_task_id="091") == expected


def test_non_latin_script_falls_back_to_block_id() -> None:
    assert to_pascal_case("日本語のブロック", fallback_task_id="091") == "Block091"


def test_pure_emoji_falls_back_to_block_id() -> None:
    assert to_pascal_case("🎉🚀🔥", fallback_task_id="091") == "Block091"


def test_empty_string_falls_back_to_block_id() -> None:
    assert to_pascal_case("", fallback_task_id="091") == "Block091"


def test_only_punctuation_falls_back_to_block_id() -> None:
    assert to_pascal_case("!!!---???", fallback_task_id="091") == "Block091"


def test_fallback_uses_the_supplied_task_id_verbatim() -> None:
    assert to_pascal_case("日本語", fallback_task_id="004") == "Block004"


def test_a_result_starting_with_a_digit_is_not_a_valid_identifier_and_falls_back() -> None:
    # "2FA setup" -> "2FA" (preserved acronym, digits included) + "Setup"
    # = "2FASetup", which does NOT match ^[A-Za-z][A-Za-z0-9]*$ (research.md
    # §16 step 5) since it starts with a digit -> falls back to Block{id}.
    assert to_pascal_case("2FA setup", fallback_task_id="091") == "Block091"


def test_build_branch_name_matches_documented_examples() -> None:
    assert build_branch_name("T001", "Project Bootstrap") == "T001-ProjectBootstrap"
    assert build_branch_name("T091", "Validation Pipeline") == "T091-ValidationPipeline"


def test_build_branch_name_accepts_task_id_without_t_prefix() -> None:
    assert build_branch_name("091", "Validation Pipeline") == "T091-ValidationPipeline"


def test_build_branch_name_never_double_prefixes_t() -> None:
    name = build_branch_name("T091", "Validation Pipeline")
    assert not name.startswith("TT")


def test_build_branch_name_falls_back_cleanly_for_unrepresentable_block_name() -> None:
    assert build_branch_name("T091", "日本語") == "T091-Block091"


def test_branch_name_build_then_parse_round_trip() -> None:
    branch = build_branch_name("T091", "Validation Pipeline")
    parsed = parse_branch_name(branch)
    assert parsed == ("091", "ValidationPipeline")


def test_parse_branch_name_rejects_non_matching_strings() -> None:
    assert parse_branch_name("main") is None
    assert parse_branch_name("not-a-block-branch") is None
    assert parse_branch_name("T091ValidationPipeline") is None


def test_build_checkpoint_tag_name_matches_documented_pattern() -> None:
    assert build_checkpoint_tag_name("T091", "T099") == "checkpoint-T091-T099"


def test_checkpoint_tag_name_build_then_parse_round_trip() -> None:
    tag = build_checkpoint_tag_name("T091", "T099")
    assert parse_checkpoint_tag_name(tag) == ("091", "099")


def test_parse_checkpoint_tag_name_rejects_non_matching_strings() -> None:
    assert parse_checkpoint_tag_name("T091-T099") is None
    assert parse_checkpoint_tag_name("checkpoint-091-099") is None


def test_to_slug_shares_the_same_normalization_pipeline_as_pascal_case() -> None:
    assert to_slug("Validation Pipeline!!") == "validation-pipeline"
    assert to_slug("café résumé") == "cafe-resume"


def test_to_slug_of_non_representable_input_is_empty() -> None:
    assert to_slug("日本語") == ""
