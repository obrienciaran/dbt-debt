"""Tests for the "too new to judge" guard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dbt_debt.verdict.freshness import missing_first_seen_models, too_new_models

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
WEEK = timedelta(days=7)


def test_recently_first_seen_dead_node_is_too_new() -> None:
    first_seen = {"model.p.new": NOW - timedelta(days=2)}
    assert too_new_models({"model.p.new"}, first_seen, NOW, WEEK) == {"model.p.new"}


def test_old_first_seen_is_judged_normally() -> None:
    first_seen = {"model.p.old": NOW - timedelta(days=90)}
    assert too_new_models({"model.p.old"}, first_seen, NOW, WEEK) == set()


def test_never_seen_node_is_judged_normally() -> None:
    # No job in the whole window is the strongest "unused" signal there is.
    assert too_new_models({"model.p.ghost"}, {}, NOW, WEEK) == set()


def test_alive_nodes_are_never_flagged() -> None:
    # Only members of the dead set can be set aside.
    first_seen = {"model.p.alive": NOW - timedelta(days=1)}
    assert too_new_models(set(), first_seen, NOW, WEEK) == set()


def test_zero_min_age_disables_the_guard() -> None:
    first_seen = {"model.p.new": NOW - timedelta(hours=1)}
    assert too_new_models({"model.p.new"}, first_seen, NOW, timedelta(0)) == set()


def test_missing_first_seen_picks_the_dateless_dead_nodes() -> None:
    first_seen = {"model.p.dated": NOW - timedelta(days=90)}
    dead = {"model.p.dated", "model.p.ghost"}
    assert missing_first_seen_models(dead, first_seen) == {"model.p.ghost"}


def test_missing_first_seen_ignores_nodes_outside_the_dead_set() -> None:
    assert missing_first_seen_models(set(), {}) == set()
