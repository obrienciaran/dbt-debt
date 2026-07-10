"""Tests for orphan-report assembly and the CLI orchestration of orphan discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.cli import _scan
from dbt_debt.config import Config
from datetime import datetime, timezone

from dbt_debt.domain import UsageRow, WarehouseRelation
from dbt_debt.references import model_relation_references
from dbt_debt.report.scorecard import build_orphan_report
from tests.fakes import FakeWarehouseClient

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

STG_KEY = "my-gcp-project.jaffle_shop.stg_orders"
FCT_KEY = "my-gcp-project.jaffle_shop.fct_orders"
SEED_KEY = "my-gcp-project.jaffle_shop.country_codes"
ORPHAN_KEY = "my-gcp-project.jaffle_shop.tmp_backfill"


def _config() -> Config:
    return Config(project_dir=FIXTURE.parent.parent, target_path=FIXTURE.parent.name)


def _warehouse(*extra: WarehouseRelation) -> list[WarehouseRelation]:
    # The managed dataset physically holds every dbt relation, plus whatever extras a test adds.
    present = [WarehouseRelation(key, "BASE TABLE") for key in (STG_KEY, FCT_KEY, SEED_KEY)]
    return present + list(extra)


def test_build_orphan_report_flags_orphan_not_declared_relations() -> None:
    manifest = load_manifest(FIXTURE)
    references = model_relation_references(manifest)
    existing = _warehouse(WarehouseRelation(ORPHAN_KEY, "BASE TABLE"))
    report = build_orphan_report(manifest, existing, references)
    assert report.orphans_checked is True
    assert [r.relation_key for r in report.orphaned_relations] == [ORPHAN_KEY]
    # raw.orders is read by stg_orders but declared as a source -> not undeclared.
    assert report.undeclared_sources == ()


def test_build_orphan_report_flags_undeclared_source() -> None:
    manifest = load_manifest(FIXTURE)
    # A model reads a relation dbt has no node for -> undeclared source (independent of existence).
    report = build_orphan_report(manifest, [], {"my-gcp-project.raw.events"})
    assert report.undeclared_sources == ("my-gcp-project.raw.events",)
    assert report.orphaned_relations == ()


def test_build_orphan_report_skipped_without_metadata() -> None:
    manifest = load_manifest(FIXTURE)
    references = model_relation_references(manifest)
    report = build_orphan_report(manifest, None, references)
    assert report.orphans_checked is False
    assert report.orphaned_relations == ()


def test_build_orphan_report_attaches_query_evidence_and_ranks_queried_first() -> None:
    manifest = load_manifest(FIXTURE)
    references = model_relation_references(manifest)
    quiet_key = "my-gcp-project.jaffle_shop.aaa_quiet_backup"
    existing = _warehouse(
        WarehouseRelation(quiet_key, "BASE TABLE"),
        WarehouseRelation(ORPHAN_KEY, "BASE TABLE"),
    )
    when = datetime(2026, 7, 1, tzinfo=timezone.utc)
    usage = [
        UsageRow(ORPHAN_KEY, query_count=3, last_queried=when, bytes_scanned=2048),
        UsageRow(FCT_KEY, query_count=9),
    ]
    report = build_orphan_report(manifest, existing, references, usage)
    # The still-queried orphan ranks first despite sorting after the quiet one by name.
    assert [o.relation_key for o in report.orphaned_relations] == [ORPHAN_KEY, quiet_key]
    queried, quiet = report.orphaned_relations
    assert (queried.query_count, queried.bytes_scanned) == (3, 2048)
    assert queried.last_queried is not None and queried.last_queried.startswith("2026-07-01")
    assert (quiet.query_count, quiet.last_queried, quiet.bytes_scanned) == (0, None, 0)


def test_scan_attaches_orphan_query_evidence_from_the_usage_rows() -> None:
    client = FakeWarehouseClient(
        usage=[UsageRow(ORPHAN_KEY, query_count=2, bytes_scanned=1024)],
        existing=_warehouse(WarehouseRelation(ORPHAN_KEY, "BASE TABLE")),
    )
    card = _scan(_config(), client)
    assert card.orphans is not None
    orphan = card.orphans.orphaned_relations[0]
    assert (orphan.query_count, orphan.bytes_scanned) == (2, 1024)


def test_scan_reports_orphans_via_fake_client() -> None:
    client = FakeWarehouseClient(existing=_warehouse(WarehouseRelation(ORPHAN_KEY, "BASE TABLE")))
    card = _scan(_config(), client)
    assert card.orphans is not None
    assert card.orphans.orphans_checked is True
    assert [r.relation_key for r in card.orphans.orphaned_relations] == [ORPHAN_KEY]


def test_scan_warns_and_skips_orphans_without_permission(
    capsys: pytest.CaptureFixture[str],
) -> None:
    card = _scan(_config(), FakeWarehouseClient(orphans_permitted=False))
    assert card.orphans is not None
    assert card.orphans.orphans_checked is False
    assert "metadata" in capsys.readouterr().err
