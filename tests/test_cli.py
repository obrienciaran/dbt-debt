"""Argument-parsing and CLI-wiring tests that need no BigQuery and no dbt project."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from dataclasses import replace
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


def test_every_warehouse_has_a_specific_orphan_skip_message() -> None:
    # The generic fallback names no grant and gives the reader nothing to act on, so it must
    # only ever fire for a warehouse we don't know about.
    from dbt_debt.config import SUPPORTED_WAREHOUSES

    for warehouse in SUPPORTED_WAREHOUSES:
        message = cli._ORPHAN_INVENTORY_SKIP_MESSAGES[warehouse]
        assert "need " in message
        assert "Undeclared sources are still reported." in message


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


def test_top_n_cannot_be_negative(capsys: pytest.CaptureFixture[str]) -> None:
    # A negative slice bound would silently drop entries from the end of every summary list
    # under a "Top N of M" label that presents the cut as intentional.
    assert main(["scan", "--top-n", "-1"]) == 2
    assert "--top-n" in capsys.readouterr().err


def test_redshift_caps_the_lookback_window_at_its_retention() -> None:
    # Redshift's SYS views keep far less than the 180-day default, so the report must not claim
    # a window the warehouse cannot answer for.
    config = _config_from_args(_build_parser().parse_args(["scan"]))
    assert replace(config, warehouse="redshift").effective_lookback_days == 7


def test_the_default_window_is_untouched_off_redshift() -> None:
    # Every other warehouse retains at least the 180-day default, so the common case must not
    # warn or shrink: Redshift is the only one where a default run is capped.
    config = _config_from_args(_build_parser().parse_args(["scan"]))
    for warehouse in ("bigquery", "snowflake", "databricks"):
        assert replace(config, warehouse=warehouse).effective_lookback_days == 180


def test_an_over_ask_falls_back_to_each_warehouse_maximum() -> None:
    config = _config_from_args(_build_parser().parse_args(["scan", "--lookback-days", "400"]))
    caps = {"bigquery": 180, "snowflake": 365, "redshift": 7, "databricks": 365}
    for warehouse, cap in caps.items():
        assert replace(config, warehouse=warehouse).effective_lookback_days == cap


def test_the_cap_never_raises_a_shorter_request() -> None:
    # It is a ceiling, not a floor: asking for less than retention still asks for less.
    config = _config_from_args(_build_parser().parse_args(["scan", "--lookback-days", "3"]))
    assert replace(config, warehouse="redshift").effective_lookback_days == 3


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


def test_broken_pipe_is_a_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    # `dbt-debt scan --print | head` closes stdout early; the scan finished, so the run ends
    # cleanly instead of with a traceback.
    def _broken(args: object) -> int:
        raise BrokenPipeError

    monkeypatch.setattr(cli, "_run_scan", _broken)
    # A StringIO stands in for the broken stream: its fileno() refusal keeps the handler's
    # devnull redirect away from the test harness's real stdout.
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert main(["scan"]) == 0


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
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    without_connector: Callable[[str], None],
) -> None:
    # With the optional extra missing, a Snowflake scan must stop at exit 3 with the install
    # hint — proof the SDK is only reached for the selected warehouse.
    without_connector("snowflake.connector")
    manifest = {
        "metadata": {**_METADATA, "adapter_type": "snowflake"},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 3
    assert "dbt-debt[snowflake]" in capsys.readouterr().err


def test_bigquery_scan_without_the_sdk_exits_three(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    without_package: Callable[[str], None],
) -> None:
    # Same isolation proof for the BigQuery extra: the SDK is only reached for the selected
    # warehouse, and its absence is a readable exit 3 with the install hint.
    without_package("google")
    manifest = {
        "metadata": {**_METADATA, "adapter_type": "bigquery"},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 3
    assert "dbt-debt[bigquery]" in capsys.readouterr().err


def test_redshift_scan_without_the_connector_exits_three(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    without_connector: Callable[[str], None],
) -> None:
    # Same isolation proof for the Redshift extra: the SDK is only reached for the selected
    # warehouse, and its absence is a readable exit 3.
    without_connector("redshift_connector")
    manifest = {
        "metadata": {**_METADATA, "adapter_type": "redshift"},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 3
    assert "dbt-debt[redshift]" in capsys.readouterr().err


def test_databricks_scan_without_the_connector_exits_three(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    without_connector: Callable[[str], None],
) -> None:
    # And the third optional extra, so every warehouse behind one carries the same contract.
    without_connector("databricks")
    manifest = {
        "metadata": {**_METADATA, "adapter_type": "databricks"},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 3
    assert "dbt-debt[databricks]" in capsys.readouterr().err


def _scan_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], adapter_type: str, *argv: str
) -> str:
    manifest = {
        "metadata": {**_METADATA, "adapter_type": adapter_type},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    main(["scan", "--project-dir", str(tmp_path), "--no-cache", *argv])
    return capsys.readouterr().err


def test_redshift_warns_on_stderr_that_the_window_was_capped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Announced before the scan runs, so a run piped to a file still reports the short window.
    err = _scan_stderr(tmp_path, capsys, "redshift")
    assert (
        "Only 7 days lookback displayed (180 requested but Redshift SYS views retain only 7)" in err
    )


def test_no_cap_warning_when_the_request_fits_retention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "lookback displayed" not in _scan_stderr(
        tmp_path, capsys, "redshift", "--lookback-days", "3"
    )


def test_no_cap_warning_off_redshift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    without_connector: Callable[[str], None],
) -> None:
    # The cap warning is written before the client is built, so hiding the connector keeps
    # the scan from reaching a live account without touching the assertion.
    without_connector("snowflake.connector")
    assert "lookback displayed" not in _scan_stderr(tmp_path, capsys, "snowflake")


def test_an_over_ask_warns_on_stderr_off_redshift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    without_connector: Callable[[str], None],
) -> None:
    # Asking Snowflake for more than the year it keeps is the only way a non-Redshift scan
    # reaches the cap, and it must say so rather than reporting the request back unchanged.
    without_connector("snowflake.connector")
    err = _scan_stderr(tmp_path, capsys, "snowflake", "--lookback-days", "400")
    assert "Only 365 days lookback displayed" in err
    assert "Snowflake ACCOUNT_USAGE retains only 365" in err


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


def _write_scannable_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-model manifest plus a faked BigQuery client, for full end-to-end scans."""

    from tests.fakes import FakeWarehouseClient

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
    monkeypatch.setattr(
        "dbt_debt.consumption.bigquery.RealBigQueryClient",
        lambda config, project=None: FakeWarehouseClient(),
    )


def test_scan_prints_the_text_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scannable_project(tmp_path, monkeypatch)
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache", "--print"]) == 0
    assert "dbt-debt scorecard" in capsys.readouterr().out


def test_scan_json_format_emits_valid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scannable_project(tmp_path, monkeypatch)
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["project_name"] == "p"


def test_scan_orphans_report_in_both_formats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scannable_project(tmp_path, monkeypatch)
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache", "--orphans"]) == 0
    assert "dbt-debt orphans" in capsys.readouterr().out
    assert (
        main(
            [
                "scan",
                "--project-dir",
                str(tmp_path),
                "--no-cache",
                "--orphans",
                "--format",
                "json",
            ]
        )
        == 0
    )
    json.loads(capsys.readouterr().out)


def test_scan_writes_the_report_to_an_output_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scannable_project(tmp_path, monkeypatch)
    out_file = tmp_path / "debt.json"
    argv = ["scan", "--project-dir", str(tmp_path), "--no-cache", "--format", "json"]
    assert main([*argv, "-o", str(out_file)]) == 0
    json.loads(out_file.read_text())
    assert "wrote report" in capsys.readouterr().err


def test_databricks_rejects_a_quoted_comment_pattern(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Valid as a regex, but Databricks SQL cannot carry the quote safely, so the scan must
    # stop before any connection with a clear message.
    manifest = {
        "metadata": {**_METADATA, "adapter_type": "databricks"},
        "nodes": {"model.p.m": {"resource_type": "model", "name": "m", "schema": "s"}},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    argv = ["scan", "--project-dir", str(tmp_path), "--no-cache"]
    assert main([*argv, "--query-comment-pattern", "d'bt"]) == 2
    assert "single quote" in capsys.readouterr().err


def test_region_flag_warns_off_bigquery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    without_connector: Callable[[str], None],
) -> None:
    # The warning is written before the client is built, so hiding the connector keeps the
    # scan from reaching a live account without touching the assertion.
    without_connector("snowflake.connector")
    err = _scan_stderr(tmp_path, capsys, "snowflake", "--region", "EU")
    assert "--region only applies to BigQuery" in err


def test_load_catalog_returns_none_when_absent(tmp_path: Path) -> None:
    from dbt_debt.cli import _load_catalog
    from dbt_debt.config import Config

    assert _load_catalog(Config(project_dir=tmp_path)) is None


def test_storage_bytes_without_a_catalog_is_empty() -> None:
    from dbt_debt.cli import _storage_bytes

    assert _storage_bytes(None) == {}


def test_columns_without_a_catalog_fall_back_to_model_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dbt_debt.cli import _column_report
    from dbt_debt.config import Config
    from tests.fakes import FakeWarehouseClient

    report = _column_report(
        Config(columns=True), FakeWarehouseClient(), _manifest_for("bigquery"), None, {}
    )
    assert report is None
    assert "dbt docs generate" in capsys.readouterr().err


def test_metadata_helpers_step_aside_without_datasets() -> None:
    # A manifest with no models has no managed or source datasets, so both optional metadata
    # reads skip without touching the client.
    from dbt_debt.cli import _existing_relations, _source_last_modified
    from tests.fakes import FakeWarehouseClient

    client = FakeWarehouseClient()
    assert _existing_relations(client, _manifest_for("bigquery"), "bigquery") is None
    assert _source_last_modified(client, _manifest_for("bigquery")) is None
    assert client.calls["existing_relations"] == 0
    assert client.calls["source_last_modified"] == 0


def test_scan_opens_the_viewer_when_the_terminal_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no competing output intent and a drivable terminal, the viewer handles the report
    # and the scan exits 0 without printing it again.
    _write_scannable_project(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_should_view", lambda config, args: True)
    shown: list[bool] = []
    monkeypatch.setattr(
        "dbt_debt.report.viewer.run_viewer",
        lambda scorecard, config: shown.append(True) or True,
    )
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 0
    assert shown == [True]


def test_scan_falls_back_to_plain_output_when_the_viewer_declines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # run_viewer returns False when the terminal cannot be set up; the report must still
    # reach stdout so every environment gets it.
    _write_scannable_project(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_should_view", lambda config, args: True)
    monkeypatch.setattr("dbt_debt.report.viewer.run_viewer", lambda scorecard, config: False)
    assert main(["scan", "--project-dir", str(tmp_path), "--no-cache"]) == 0
    assert "dbt-debt scorecard" in capsys.readouterr().out


def test_top_level_clear_cache_continues_into_the_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # With a command following, --clear-cache clears and then runs it (here failing on the
    # missing manifest) instead of exiting early.
    cleared: list[bool] = []
    monkeypatch.setattr(cli, "_clear_all_cache", lambda: cleared.append(True))
    assert main(["--clear-cache", "scan", "--project-dir", str(tmp_path)]) == 2
    assert cleared == [True]
    assert "manifest not found" in capsys.readouterr().err
