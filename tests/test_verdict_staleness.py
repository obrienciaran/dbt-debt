"""Tests for the stale-source verdict."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dbt_debt.domain import Relation
from dbt_debt.verdict.staleness import stale_sources

NOW = datetime(2026, 7, 9, tzinfo=timezone.utc)
MAX_AGE = timedelta(days=30)


def _relation(table: str) -> Relation:
    return Relation(
        unique_id=f"source.p.raw.{table}",
        relation_key=f"db.raw.{table}",
        schema="raw",
        name=f"raw.{table}",
    )


def test_source_older_than_the_threshold_is_stale() -> None:
    modified = NOW - timedelta(days=31)
    [(relation, date)] = stale_sources(
        [_relation("events")], {"db.raw.events": modified}, NOW, MAX_AGE
    )
    assert relation.name == "raw.events"
    assert date == modified


def test_source_within_the_threshold_is_fresh() -> None:
    last_modified = {"db.raw.events": NOW - timedelta(days=29)}
    assert stale_sources([_relation("events")], last_modified, NOW, MAX_AGE) == []


def test_exactly_at_the_threshold_is_fresh() -> None:
    # The comparison is strictly older-than, so a table touched exactly 30 days ago is not
    # flagged.
    last_modified = {"db.raw.events": NOW - MAX_AGE}
    assert stale_sources([_relation("events")], last_modified, NOW, MAX_AGE) == []


def test_source_without_metadata_is_skipped() -> None:
    # Absent metadata is not evidence of staleness.
    assert stale_sources([_relation("events")], {}, NOW, MAX_AGE) == []


def test_stalest_first() -> None:
    last_modified = {
        "db.raw.events": NOW - timedelta(days=40),
        "db.raw.legacy": NOW - timedelta(days=400),
    }
    relations = [_relation("events"), _relation("legacy")]
    stale = stale_sources(relations, last_modified, NOW, MAX_AGE)
    assert [relation.name for relation, _ in stale] == ["raw.legacy", "raw.events"]
