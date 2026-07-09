"""Tests for the unused-declared-source verdict."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.manifest import load_manifest, parse_manifest
from dbt_debt.verdict.sources import unused_sources

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

SOURCE = "source.jaffle_shop.raw.orders"


def _manifest_data(model_depends_on: list[str]) -> dict[str, object]:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "p",
        },
        "nodes": {
            "model.p.stg": {
                "resource_type": "model",
                "name": "stg",
                "depends_on": {"nodes": model_depends_on},
            },
        },
        "sources": {
            "source.p.raw.events": {
                "resource_type": "source",
                "source_name": "raw",
                "name": "events",
                "identifier": "events",
                "database": "db",
                "schema": "raw",
                "original_file_path": "models/staging/sources.yml",
            },
        },
    }


def test_fixture_source_is_unused() -> None:
    # Nothing in the fixture depends on the source, so it is reported.
    manifest = load_manifest(FIXTURE)
    assert [r.unique_id for r in unused_sources(manifest)] == [SOURCE]


def test_source_read_by_a_model_is_used() -> None:
    manifest = parse_manifest(_manifest_data(["source.p.raw.events"]))
    assert unused_sources(manifest) == []


def test_source_read_by_nothing_is_unused() -> None:
    manifest = parse_manifest(_manifest_data([]))
    [relation] = unused_sources(manifest)
    assert relation.unique_id == "source.p.raw.events"
    assert relation.name == "raw.events"
    assert relation.original_file_path == "models/staging/sources.yml"


def test_a_test_on_the_source_does_not_count_as_use() -> None:
    # A test guards data; it does not consume it. A source kept alive only by its own
    # tests is still reported.
    data = _manifest_data([])
    nodes = data["nodes"]
    assert isinstance(nodes, dict)
    nodes["test.p.not_null_raw_events_id"] = {
        "resource_type": "test",
        "name": "not_null_raw_events_id",
        "depends_on": {"nodes": ["source.p.raw.events"]},
        "attached_node": "source.p.raw.events",
    }
    manifest = parse_manifest(data)
    assert [r.unique_id for r in unused_sources(manifest)] == ["source.p.raw.events"]


def test_an_exposure_on_the_source_counts_as_use() -> None:
    data = _manifest_data([])
    data["exposures"] = {
        "exposure.p.raw_dashboard": {
            "name": "raw_dashboard",
            "depends_on": {"nodes": ["source.p.raw.events"]},
        }
    }
    manifest = parse_manifest(data)
    assert unused_sources(manifest) == []
