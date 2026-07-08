"""Argument-parsing and CLI-wiring tests that need no BigQuery and no dbt project."""

from __future__ import annotations

import pytest

from dbt_debt.cli import _build_parser, _config_from_args, main


def test_top_level_clear_cache_survives_the_scan_subcommand() -> None:
    # Regression: the two --clear-cache flags used to share one dest, and argparse lets a
    # subparser's default overwrite a value the top-level parser already set — so
    # `dbt-debt --clear-cache scan` silently dropped the flag.
    args = _build_parser().parse_args(["--clear-cache", "scan"])
    assert args.clear_all_cache is True
    assert args.clear_cache is False


def test_scan_clear_cache_is_a_separate_flag() -> None:
    args = _build_parser().parse_args(["scan", "--clear-cache"])
    assert args.clear_cache is True
    assert args.clear_all_cache is False


def test_top_n_reaches_the_config() -> None:
    args = _build_parser().parse_args(["scan", "--top-n", "3"])
    assert _config_from_args(args).top_n == 3


def test_invalid_query_comment_pattern_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Validated before any BigQuery or file access, so the run stops with a clear message
    # instead of a confusing BigQuery syntax error mid-scan.
    assert main(["scan", "--query-comment-pattern", "bad'''pattern"]) == 2
    assert "query-comment-pattern" in capsys.readouterr().err
