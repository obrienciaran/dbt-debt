"""Argument-parsing and CLI-wiring tests that need no BigQuery and no dbt project."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dbt_debt import cli
from dbt_debt.cli import _build_parser, _config_from_args, _emit, main
from dbt_debt.consumption.client import WarehouseError

_METADATA = {
    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
    "project_name": "p",
}


def _write_manifest(tmp_path: Path, nodes: dict[str, Any]) -> None:
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    (target / "manifest.json").write_text(json.dumps({"metadata": _METADATA, "nodes": nodes}))


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


def test_rare_threshold_reaches_the_config() -> None:
    args = _build_parser().parse_args(["scan", "--rare-threshold", "3"])
    assert _config_from_args(args).rare_threshold == 3
    assert _config_from_args(_build_parser().parse_args(["scan"])).rare_threshold == 5


def test_min_age_days_reaches_the_config() -> None:
    args = _build_parser().parse_args(["scan", "--min-age-days", "14"])
    assert _config_from_args(args).min_age_days == 14
    assert _config_from_args(_build_parser().parse_args(["scan"])).min_age_days == 7


def test_stale_source_days_reaches_the_config() -> None:
    args = _build_parser().parse_args(["scan", "--stale-source-days", "90"])
    assert _config_from_args(args).stale_source_days == 90
    assert _config_from_args(_build_parser().parse_args(["scan"])).stale_source_days == 30


def test_min_age_zero_skips_the_first_seen_call() -> None:
    from dbt_debt.cli import _scan
    from dbt_debt.config import Config
    from tests.fakes import FakeWarehouseClient

    fixture_dir = Path(__file__).parent / "fixtures"
    client = FakeWarehouseClient()
    config = Config(
        project_dir=fixture_dir.parent, target_path=Path(fixture_dir.name), min_age_days=0
    )
    _scan(config, client)
    assert client.calls["relation_first_seen"] == 0

    with_guard = Config(project_dir=fixture_dir.parent, target_path=Path(fixture_dir.name))
    _scan(with_guard, client)
    assert client.calls["relation_first_seen"] == 1

    databricks = FakeWarehouseClient()
    databricks_config = Config(
        project_dir=fixture_dir.parent,
        target_path=Path(fixture_dir.name),
        warehouse="databricks",
        min_age_days=0,
    )
    _scan(databricks_config, databricks)
    assert databricks.calls["relation_first_seen"] == 1


def test_invalid_query_comment_pattern_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Validated before any BigQuery or file access, so the run stops with a clear message
    # instead of a confusing BigQuery syntax error mid-scan.
    assert main(["scan", "--query-comment-pattern", "bad'''pattern"]) == 2
    assert "query-comment-pattern" in capsys.readouterr().err


def test_lookback_days_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "--lookback-days", "0"]) == 2
    assert "--lookback-days" in capsys.readouterr().err


def test_malformed_manifest_exits_two_with_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{ truncated")
    assert main(["scan", "--project-dir", str(tmp_path)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_manifest_without_models_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An empty scorecard is nearly always a wrong --project-dir, so say so instead of
    # printing all zeros.
    _write_manifest(tmp_path, nodes={})
    assert main(["scan", "--project-dir", str(tmp_path)]) == 2
    assert "has no models" in capsys.readouterr().err


def test_warehouse_error_mid_scan_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(
        tmp_path,
        nodes={
            "model.p.m": {
                "resource_type": "model",
                "name": "m",
                "database": "proj",
                "schema": "mart",
                "alias": "m",
            }
        },
    )

    class _MidScanFailure:
        def __init__(self, config: Any, project: str | None = None) -> None:
            pass

        def assert_usage_permission(self) -> None:
            raise WarehouseError("BigQuery query for job history failed: boom")

    monkeypatch.setattr("dbt_debt.consumption.bigquery.RealBigQueryClient", _MidScanFailure)
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 3
    assert "job history" in capsys.readouterr().err


def test_keyboard_interrupt_exits_130(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _interrupted(args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run_scan", _interrupted)
    assert main(["scan"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_malformed_catalog_degrades_to_no_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The catalog only feeds sizes and the column stage, so a damaged one degrades exactly
    # like a missing one instead of failing the scan.
    from dbt_debt.cli import _load_catalog
    from dbt_debt.config import Config

    target = tmp_path / "target"
    target.mkdir()
    (target / "catalog.json").write_text("garbage")
    assert _load_catalog(Config(project_dir=tmp_path)) is None
    assert "Continuing without catalog" in capsys.readouterr().err


def test_emit_reports_unwritable_output_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Writing to a directory fails with OSError; the CLI must turn that into exit 2.
    assert _emit("report", str(tmp_path)) == 2
    assert "cannot write report" in capsys.readouterr().err


def _manifest_for(adapter_type: str | None) -> Any:
    from dbt_debt.domain import Manifest

    return Manifest(
        project_name="p", dbt_schema_version="", dbt_version=None, adapter_type=adapter_type
    )


def test_warehouse_flag_overrides_the_manifest_adapter() -> None:
    from dbt_debt.cli import _resolve_warehouse

    assert _resolve_warehouse("snowflake", _manifest_for("bigquery")) == "snowflake"


def test_warehouse_auto_detects_from_the_manifest_adapter() -> None:
    from dbt_debt.cli import _resolve_warehouse

    assert _resolve_warehouse(None, _manifest_for("snowflake")) == "snowflake"
    assert _resolve_warehouse(None, _manifest_for("redshift")) == "redshift"
    assert _resolve_warehouse(None, _manifest_for("databricks")) == "databricks"


def test_missing_adapter_type_falls_back_to_bigquery() -> None:
    # Older artifacts predate adapter_type, and the tool was BigQuery-only.
    from dbt_debt.cli import _resolve_warehouse

    assert _resolve_warehouse(None, _manifest_for(None)) == "bigquery"


def test_unsupported_adapter_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "target"
    target.mkdir()
    manifest = {
        "metadata": {**_METADATA, "adapter_type": "duckdb"},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert main(["scan", "--project-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "duckdb" in err
    assert "--warehouse" in err


def test_snowflake_scan_without_the_connector_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The optional extra is absent from the dev environment, so a Snowflake scan must stop at
    # exit 3 with the install hint — proof the SDK is only reached for the selected warehouse.
    manifest = {
        "metadata": {**_METADATA, "adapter_type": "snowflake"},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 3
    assert "dbt-debt[snowflake]" in capsys.readouterr().err


def test_redshift_scan_without_the_connector_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same isolation proof for the Redshift extra: the SDK is only reached for the selected
    # warehouse, and its absence is a readable exit 3.
    manifest = {
        "metadata": {**_METADATA, "adapter_type": "redshift"},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 3
    assert "dbt-debt[redshift]" in capsys.readouterr().err


def test_cache_key_carries_the_warehouse(tmp_path: Path) -> None:
    # Two warehouses' results must never collide in the cache, whatever else matches.
    from dbt_debt.cli import _wrap_cache
    from dbt_debt.config import Config
    from tests.fakes import FakeWarehouseClient

    config = Config(project_dir=tmp_path, warehouse="snowflake", connection="team")
    wrapped: Any = _wrap_cache(FakeWarehouseClient(), config, "db")
    assert wrapped._key_parts["warehouse"] == "snowflake"
    assert wrapped._key_parts["connection"] == "team"


def test_cache_key_carries_the_redshift_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Redshift's connection is env-var based, so the endpoint joins the key: two workgroups
    # sharing a database name must never serve each other's cached rows. Off Redshift the
    # env var is ignored.
    from dbt_debt.cli import _wrap_cache
    from dbt_debt.config import Config
    from tests.fakes import FakeWarehouseClient

    monkeypatch.setenv("REDSHIFT_HOST", "wg-a.eu-west-1.redshift-serverless.amazonaws.com")
    config = Config(project_dir=tmp_path, warehouse="redshift")
    wrapped: Any = _wrap_cache(FakeWarehouseClient(), config, "db")
    assert wrapped._key_parts["endpoint"] == "wg-a.eu-west-1.redshift-serverless.amazonaws.com"

    bigquery: Any = _wrap_cache(FakeWarehouseClient(), Config(project_dir=tmp_path), "db")
    assert bigquery._key_parts["endpoint"] == ""


def test_cache_key_carries_the_databricks_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dbt_debt.cli import _wrap_cache
    from dbt_debt.config import Config
    from tests.fakes import FakeWarehouseClient

    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.example.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME", raising=False)
    config = Config(project_dir=tmp_path, warehouse="databricks")
    wrapped: Any = _wrap_cache(FakeWarehouseClient(), config, "main")
    assert wrapped._key_parts["endpoint"] == ("workspace.example.com|/sql/1.0/warehouses/abc")


def test_databricks_columns_are_skipped_before_query_text_is_requested(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dbt_debt.cli import _column_report
    from dbt_debt.config import Config
    from tests.fakes import FakeWarehouseClient

    client = FakeWarehouseClient(query_texts=["SELECT secret FROM main.marts.model"])
    report = _column_report(
        Config(warehouse="databricks", columns=True),
        client,
        _manifest_for("databricks"),
        None,
        {},
    )
    assert report is None
    assert client.calls["query_texts"] == 0
    assert "would be unsafe" in capsys.readouterr().err


def test_databricks_factory_is_lazy_and_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dbt_debt.cli import _make_client
    from dbt_debt.config import Config
    from tests.fakes import FakeWarehouseClient

    expected = FakeWarehouseClient()
    monkeypatch.setattr(
        "dbt_debt.consumption.databricks.RealDatabricksClient",
        lambda config, database=None: expected,
    )
    assert _make_client(Config(warehouse="databricks"), "main") is expected
