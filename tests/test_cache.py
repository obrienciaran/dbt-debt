"""Tests for the TTL disk cache that fronts the BigQuery client.

A counting `FakeWarehouseClient` stands in for the warehouse: a hit serves the second call from
disk without touching the inner client, a key change or an expired entry forces a re-fetch, and
the prune-on-construction sweep is the teardown that keeps the cache from persisting forever.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dbt_debt.consumption.cache import (
    CachingWarehouseClient,
    _relations_from_json,
    _relations_to_json,
    _usage_from_json,
    _usage_to_json,
    cache_dir_for,
)
from dbt_debt.domain import UsageRow, WarehouseRelation
from tests.fakes import FakeWarehouseClient

_KEY = {"project": "p", "region": "US", "lookback_days": "180", "query_comment_pattern": "x"}
_DAY = timedelta(hours=24)


def _client(
    inner: FakeWarehouseClient, cache_dir: Path, ttl: timedelta = _DAY
) -> CachingWarehouseClient:
    return CachingWarehouseClient(inner, cache_dir=cache_dir, ttl=ttl, key_parts=_KEY)


def test_second_call_is_served_from_disk(tmp_path: Path) -> None:
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    client = _client(inner, tmp_path)

    first = client.table_usage()
    second = client.table_usage()

    assert first == second == [UsageRow("a.b.c", 3)]
    assert inner.calls["table_usage"] == 1


def test_a_fresh_caching_client_reuses_the_file_on_disk(tmp_path: Path) -> None:
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    _client(inner, tmp_path).table_usage()
    # A brand-new wrapper over a fresh inner client still hits the cache file from the first run.
    other = FakeWarehouseClient(usage=[UsageRow("a.b.c", 99)])
    assert _client(other, tmp_path).table_usage() == [UsageRow("a.b.c", 3)]
    assert other.calls["table_usage"] == 0


def test_changing_a_key_part_misses(tmp_path: Path) -> None:
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    _client(inner, tmp_path).table_usage()

    other_key = dict(_KEY, region="EU")
    CachingWarehouseClient(inner, cache_dir=tmp_path, ttl=_DAY, key_parts=other_key).table_usage()

    assert inner.calls["table_usage"] == 2


def test_permission_preflight_is_never_cached(tmp_path: Path) -> None:
    inner = FakeWarehouseClient()
    client = _client(inner, tmp_path)
    client.assert_usage_permission()
    client.assert_usage_permission()
    assert inner.calls["assert_usage_permission"] == 2


def test_existing_relations_keys_on_the_datasets(tmp_path: Path) -> None:
    rel = WarehouseRelation("p.staging.t", "BASE TABLE")
    inner = FakeWarehouseClient(existing=[rel])
    client = _client(inner, tmp_path)

    client.existing_relations({"staging"})
    client.existing_relations({"staging"})
    assert inner.calls["existing_relations"] == 1
    # A different dataset set is a different key, so it re-fetches.
    client.existing_relations({"marts"})
    assert inner.calls["existing_relations"] == 2


def test_expired_entry_is_refetched_and_removed(tmp_path: Path) -> None:
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    # A one-hour TTL with a two-hour-old entry: stale.
    client = CachingWarehouseClient(
        inner, cache_dir=tmp_path, ttl=timedelta(hours=1), key_parts=_KEY
    )
    client.table_usage()
    (path,) = list(tmp_path.glob("*.json"))
    _backdate(path, hours=2)

    client.table_usage()
    assert inner.calls["table_usage"] == 2


def test_entry_ttl_outlives_the_session_that_set_it(tmp_path: Path) -> None:
    # Written under a 2h TTL, an entry stays valid for 2h even when a later run (a fresh
    # client with the 1h default) would have expired it — the TTL is a property of the entry.
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    _client(inner, tmp_path, ttl=timedelta(hours=2)).table_usage()
    (path,) = list(tmp_path.glob("*.json"))
    _backdate(path, hours=1.5)

    later = FakeWarehouseClient(usage=[UsageRow("a.b.c", 99)])
    default_run = _client(later, tmp_path, ttl=timedelta(hours=1))
    assert default_run.table_usage() == [UsageRow("a.b.c", 3)]
    assert later.calls["table_usage"] == 0


def test_explicit_ttl_overrides_the_stored_one(tmp_path: Path) -> None:
    # honor_entry_ttl=False is the explicit --cache-ttl path: this run's value governs, so the
    # same 1.5h-old entry written under 2h is expired by an explicit 1h.
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    _client(inner, tmp_path, ttl=timedelta(hours=2)).table_usage()
    (path,) = list(tmp_path.glob("*.json"))
    _backdate(path, hours=1.5)

    later = FakeWarehouseClient(usage=[UsageRow("a.b.c", 99)])
    explicit_run = CachingWarehouseClient(
        later, cache_dir=tmp_path, ttl=timedelta(hours=1), key_parts=_KEY, honor_entry_ttl=False
    )
    assert explicit_run.table_usage() == [UsageRow("a.b.c", 99)]
    assert later.calls["table_usage"] == 1


def test_entry_without_a_stored_ttl_uses_the_run_ttl(tmp_path: Path) -> None:
    # An entry from before TTLs were stored per entry falls back to this run's TTL.
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    _client(inner, tmp_path, ttl=timedelta(hours=1)).table_usage()
    (path,) = list(tmp_path.glob("*.json"))
    entry = json.loads(path.read_text())
    del entry["ttl_hours"]
    path.write_text(json.dumps(entry))
    _backdate(path, hours=2)

    later = FakeWarehouseClient(usage=[UsageRow("a.b.c", 99)])
    assert _client(later, tmp_path, ttl=timedelta(hours=1)).table_usage() == [UsageRow("a.b.c", 99)]
    assert later.calls["table_usage"] == 1


def test_prune_on_construction_removes_old_files(tmp_path: Path) -> None:
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    _client(inner, tmp_path).table_usage()
    (path,) = list(tmp_path.glob("*.json"))
    _backdate(path, hours=48)

    # Constructing a new client (TTL 24h) prunes the 48-hour-old file.
    _client(inner, tmp_path)
    assert list(tmp_path.glob("*.json")) == []


def test_corrupt_entry_is_treated_as_a_miss(tmp_path: Path) -> None:
    inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
    client = _client(inner, tmp_path)
    (tmp_path / "garbage.json").write_text("not json")
    client.table_usage()  # prune already ran on construction; a fetch still works
    assert inner.calls["table_usage"] == 1


def test_valid_json_of_the_wrong_shape_is_also_a_miss(tmp_path: Path) -> None:
    # Regression: a cache file holding a JSON list (not the expected dict) used to raise an
    # uncaught TypeError from the prune sweep and kill the scan.
    inner = FakeWarehouseClient(query_texts=["q"])
    _client(inner, tmp_path).query_texts()
    for path in tmp_path.glob("*.json"):
        path.write_text("[1, 2]")

    fresh_inner = FakeWarehouseClient(query_texts=["q"])
    assert _client(fresh_inner, tmp_path).query_texts() == ["q"]
    assert fresh_inner.calls["query_texts"] == 1


def test_cache_dir_for_is_under_temp_and_project_specific(tmp_path: Path) -> None:
    a = cache_dir_for(tmp_path / "project_a")
    b = cache_dir_for(tmp_path / "project_b")
    assert a != b
    assert a.parent.name == "dbt-debt-cache"


def test_scan_clear_cache_clears_the_project_dir_before_scanning(tmp_path: Path) -> None:
    from dbt_debt.cli import _build_parser, _run_scan

    cache_dir = cache_dir_for(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "something.json").write_text("{}")

    args = _build_parser().parse_args(["scan", "--project-dir", str(tmp_path), "--clear-cache"])
    # No manifest in tmp_path, so the scan itself stops at exit 2 — but the cache is cleared first.
    assert _run_scan(args) == 2
    assert not cache_dir.exists()


def test_bare_clear_cache_wipes_everything_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dbt_debt import cli

    root = tmp_path / "dbt-debt-cache"
    (root / "proj_a").mkdir(parents=True)
    (root / "proj_a" / "x.json").write_text("{}")
    monkeypatch.setattr("dbt_debt.consumption.cache.cache_root", lambda: root)

    assert cli.main(["--clear-cache"]) == 0
    assert not root.exists()


def test_no_command_without_clear_cache_returns_two() -> None:
    from dbt_debt import cli

    assert cli.main([]) == 2


def test_first_seen_round_trips_through_the_cache(tmp_path: Path) -> None:
    when = datetime(2026, 6, 1, tzinfo=timezone.utc)
    inner = FakeWarehouseClient(first_seen={"p.d.t": when})
    client = _client(inner, tmp_path)
    assert client.relation_first_seen() == {"p.d.t": when}
    # Second call is served from disk with the datetime intact.
    assert client.relation_first_seen() == {"p.d.t": when}
    assert inner.calls["relation_first_seen"] == 1


def test_unwritable_cache_dir_fails_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A cache that cannot be created must never kill the scan: it disables itself with a
    # warning and every call goes straight to the inner client.
    parent = tmp_path / "readonly"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        inner = FakeWarehouseClient(usage=[UsageRow("a.b.c", 3)])
        client = _client(inner, parent / "cache")
        assert client.table_usage() == [UsageRow("a.b.c", 3)]
        assert client.table_usage() == [UsageRow("a.b.c", 3)]
        assert inner.calls["table_usage"] == 2
        assert "scan cache disabled" in capsys.readouterr().err
    finally:
        parent.chmod(0o700)


def test_usage_round_trip_preserves_last_queried() -> None:
    when = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc)
    rows = [UsageRow("a.b.c", 3, when), UsageRow("d.e.f", 1, None)]
    assert _usage_from_json(_usage_to_json(rows)) == rows


def test_relations_round_trip() -> None:
    rows = [WarehouseRelation("p.s.t", "VIEW")]
    assert _relations_from_json(_relations_to_json(rows)) == rows


def _backdate(path: Path, *, hours: float) -> None:
    """Rewrite a cache entry's `created` timestamp to `hours` in the past."""

    entry = json.loads(path.read_text())
    entry["created"] = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    path.write_text(json.dumps(entry))


def test_source_last_modified_is_cached_and_keyed_on_the_datasets(tmp_path: Path) -> None:
    when = datetime(2026, 6, 1, tzinfo=timezone.utc)
    inner = FakeWarehouseClient(last_modified={"db.raw.events": when})
    client = _client(inner, tmp_path)

    first = client.source_last_modified({"db.raw"})
    second = client.source_last_modified({"db.raw"})
    assert first == second == {"db.raw.events": when}
    assert inner.calls["source_last_modified"] == 1
    # A different dataset set is a different key, so it re-fetches.
    client.source_last_modified({"db.other"})
    assert inner.calls["source_last_modified"] == 2
