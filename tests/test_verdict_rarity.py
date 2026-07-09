"""Tests for the pure rarely-used verdict."""

from __future__ import annotations

from dbt_debt.domain import UsageRow
from dbt_debt.verdict.rarity import rarely_used_models


def _usage(counts: dict[str, int]) -> dict[str, UsageRow]:
    return {uid: UsageRow(relation_key=f"p.d.{uid}", query_count=n) for uid, n in counts.items()}


def test_at_or_below_the_threshold_is_rare_above_is_not() -> None:
    usage = _usage({"m.low": 1, "m.edge": 5, "m.busy": 6})
    assert rarely_used_models(usage, 5) == {"m.low", "m.edge"}


def test_zero_threshold_disables_the_band() -> None:
    assert rarely_used_models(_usage({"m.low": 1}), 0) == set()
    assert rarely_used_models(_usage({"m.low": 1}), -1) == set()


def test_zero_count_rows_belong_to_the_dead_set_not_the_band() -> None:
    assert rarely_used_models(_usage({"m.none": 0}), 5) == set()
