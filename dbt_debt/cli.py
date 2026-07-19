"""Command-line entry point: argument parsing plus thin orchestration.

`scan` loads dbt's on-disk artifacts, connects to the warehouse, and prints the model-level
scorecard. The warehouse work goes through the `WarehouseClient` Protocol; `_scan` takes the
client as an argument so the orchestration is testable with a fake and no credentials. The
warehouse itself is auto-detected from the manifest's `adapter_type` (overridable with
`--warehouse`), and each adapter's SDK is imported only when that warehouse is scanned.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from dbt_debt.artifacts.catalog import Catalog, load_catalog
from dbt_debt.artifacts.errors import ArtifactError
from dbt_debt.artifacts.graph import Graph
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.config import DEFAULT_QUERY_COMMENT_PATTERN, SUPPORTED_WAREHOUSES, Config
from dbt_debt.consumption.client import (
    InvalidIdentifierError,
    MissingPermissionError,
    WarehouseClient,
    WarehouseError,
)
from dbt_debt.consumption.columns import consumed_model_columns
from dbt_debt.consumption.exclusion import validate_query_comment_pattern
from dbt_debt.domain import Manifest, TableHygiene, TableStorage, WarehouseRelation
from dbt_debt.lineage.sqlglot_source import SqlglotLineage
from dbt_debt.references import model_relation_references
from dbt_debt.report.render_json import render_json, render_orphans_json
from dbt_debt.report.render_text import render_orphans_text, render_text
from dbt_debt.report.scorecard import (
    ColumnReport,
    Scorecard,
    build_column_report,
    build_orphan_report,
    build_scorecard,
)
from dbt_debt.report.spinner import status
from dbt_debt.sqlparse import build_schema


def _scan(config: Config, client: WarehouseClient, manifest: Manifest | None = None) -> Scorecard:
    """Orchestrate a scan: preflight, load artifacts, fetch usage, assemble the scorecard.

    `manifest` may be passed in when the caller has already loaded it (to resolve the warehouse
    database); otherwise it is loaded here. Tests inject a fake client and let it load.
    """

    client.assert_usage_permission()
    if manifest is None:
        manifest = load_manifest(config.manifest_path)
    graph = Graph.from_manifest(manifest)
    with status("Querying job history"):
        usage = client.table_usage()
    catalog = _load_catalog(config)
    storage = _storage_bytes(catalog)
    # On Snowflake and Redshift the live storage metrics replace the catalog sizes (warehouse
    # truth, no `dbt docs generate` needed); Snowflake's additionally carry the
    # time-travel/fail-safe breakdown.
    table_storage: dict[str, TableStorage] = {}
    if config.warehouse in ("snowflake", "redshift"):
        with status("Reading storage metrics"):
            table_storage = client.table_storage()
        storage.update({key: s.active_bytes for key, s in table_storage.items()})
    # The table-hygiene check reads SVV_TABLE_INFO's maintenance columns, which only Redshift
    # exposes; the other warehouses maintain storage layout automatically.
    table_hygiene: dict[str, TableHygiene] = {}
    if config.warehouse == "redshift":
        with status("Reading table hygiene"):
            table_hygiene = client.table_hygiene()
    with status("Analysing column usage"):
        column_report = _column_report(config, client, manifest, catalog, storage)
    references = model_relation_references(manifest, dialect=config.dialect)
    with status("Listing warehouse relations"):
        existing = _existing_relations(client, manifest)
    orphan_report = build_orphan_report(manifest, existing, references, usage)
    first_seen: dict[str, datetime] = {}
    # Databricks always needs retained-lineage first-seen evidence: even when the caller
    # disables the recent-age threshold, a relation with no lineage date must be set aside.
    if config.min_age_days > 0 or config.warehouse == "databricks":
        with status("Checking relation ages"):
            first_seen = client.relation_first_seen()
    last_modified: dict[str, datetime] | None = None
    if config.stale_source_days > 0 and manifest.relations:
        if config.warehouse in ("redshift", "databricks"):
            reason = (
                "exposes no safe table last-modified metadata"
                if config.warehouse == "redshift"
                else "source freshness is deferred until safe semantics are validated"
            )
            print(
                f"{config.warehouse.capitalize()} {reason}; the stale-source check is skipped.",
                file=sys.stderr,
            )
        else:
            with status("Checking source freshness"):
                last_modified = _source_last_modified(client, manifest)
    catalog_columns = (
        {uid: node.columns for uid, node in catalog.nodes.items()} if catalog else None
    )
    return build_scorecard(
        manifest,
        graph,
        usage,
        storage,
        config,
        column_report,
        orphan_report,
        first_seen,
        catalog_columns=catalog_columns,
        last_modified=last_modified,
        table_storage=table_storage,
        table_hygiene=table_hygiene,
    )


def _load_catalog(config: Config) -> Catalog | None:
    """Load catalog.json when present and readable; None (with a warning) otherwise.

    A malformed catalog degrades exactly like a missing one (sizes go blank and the column
    stage is skipped) because the scan's core verdicts never depend on it.
    """

    if not config.catalog_path.exists():
        return None
    try:
        return load_catalog(config.catalog_path)
    except ArtifactError as exc:
        print(f"{exc} Continuing without catalog data.", file=sys.stderr)
        return None


def _storage_bytes(catalog: Catalog | None) -> dict[str, int]:
    """Per-relation logical bytes from catalog.json, for ranking the reclaimable dead assets.

    dbt's adapter records each relation's `num_bytes` during `dbt docs generate`, so sizes come
    from the catalog already on disk rather than a live `INFORMATION_SCHEMA.TABLE_STORAGE`
    query, which needs a stronger grant (`bigquery.tables.list`) that some projects cannot read
    even as Owner. Without a catalog the map is empty and dead assets rank by name.
    """

    if catalog is None:
        return {}
    return {node.relation_key: node.num_bytes for node in catalog.nodes.values()}


def _infer_database(manifest: Manifest) -> str | None:
    """The warehouse database the models live in, taken as the most common model `database`.

    On BigQuery this is the GCP project: `INFORMATION_SCHEMA.JOBS` is project-scoped, so the
    scan must run where the relations (and their queries) live. On Snowflake it names the
    database whose `INFORMATION_SCHEMA.TABLES` the orphan scan reads. On Databricks it is the
    Unity Catalog catalog used to qualify managed schemas for relation inventory. This lets the
    tool target the right namespace without a flag, the way dbt itself does.
    """

    databases = Counter(m.database for m in manifest.models.values() if m.database)
    return databases.most_common(1)[0][0] if databases else None


def _resolve_warehouse(flag: str | None, manifest: Manifest) -> str:
    """Pick the warehouse: an explicit `--warehouse` wins, else the manifest's `adapter_type`.

    A manifest without an `adapter_type` falls back to BigQuery (older artifacts predate the
    field, and the tool was BigQuery-only). A recognised-but-unsupported adapter raises
    `ValueError` so the CLI can exit 2 with the supported list rather than misreading another
    warehouse's identifiers.
    """

    if flag:
        return flag
    adapter = manifest.adapter_type
    if adapter is None:
        return "bigquery"
    if adapter in SUPPORTED_WAREHOUSES:
        return adapter
    raise ValueError(
        f"the manifest was built by the {adapter!r} adapter, which dbt-debt does not support "
        f"yet (supported: {', '.join(SUPPORTED_WAREHOUSES)}). Pass --warehouse to override."
    )


def _make_client(config: Config, database: str | None) -> WarehouseClient:
    """Build the live client for the resolved warehouse.

    Each adapter module lazily imports its own SDK, so scanning one warehouse never imports
    (or requires installing) the other's client library.
    """

    if config.warehouse == "snowflake":
        from dbt_debt.consumption.snowflake import RealSnowflakeClient

        return RealSnowflakeClient(config, database=database)
    if config.warehouse == "redshift":
        from dbt_debt.consumption.redshift import RealRedshiftClient

        return RealRedshiftClient(config, database=database)
    if config.warehouse == "databricks":
        from dbt_debt.consumption.databricks import RealDatabricksClient

        return RealDatabricksClient(config, database=database)
    from dbt_debt.consumption.bigquery import RealBigQueryClient

    return RealBigQueryClient(config, project=database)


def _column_report(
    config: Config,
    client: WarehouseClient,
    manifest: Manifest,
    catalog: Catalog | None,
    storage: dict[str, int],
) -> ColumnReport | None:
    """Run the column stage when `--columns` is set; None for model-level-only scans.

    Falls back to model-level (with a warning) when catalog.json is absent, since the column
    universe comes from `dbt docs generate`.
    """

    if not config.columns:
        return None
    if config.warehouse == "databricks":
        print(
            "Databricks column analysis is skipped: complete query-text or column-lineage "
            "coverage has not been proven, so unused-column verdicts would be unsafe. "
            "Reporting model-level only.",
            file=sys.stderr,
        )
        return None
    if catalog is None:
        print(
            f"catalog not found at {config.catalog_path}; skipping column analysis "
            "(run `dbt docs generate`). Reporting model-level only.",
            file=sys.stderr,
        )
        return None

    schema = build_schema(catalog.relation_columns())
    consumption = consumed_model_columns(
        client.query_texts(), schema, manifest.relation_to_id(), dialect=config.dialect
    )
    edges = SqlglotLineage(manifest, catalog, dialect=config.dialect, schema=schema).edges()
    return build_column_report(manifest, catalog, consumption, edges, storage)


def _existing_relations(
    client: WarehouseClient, manifest: Manifest
) -> list[WarehouseRelation] | None:
    """List warehouse relations in dbt-managed datasets, or None when they can't be read.

    Returns None (orphaned-relation discovery is skipped) when there are no managed datasets,
    when the caller lacks `bigquery.tables.list`, or when the metadata comes back without any of
    the model relations that must physically exist (a sign the listing was silently empty rather
    than truly empty). A warning is printed in the readable cases; undeclared sources are reported
    regardless, since they come from the manifest.
    """

    datasets = manifest.managed_datasets()
    if not datasets:
        return None
    try:
        existing = client.existing_relations(datasets)
    except (MissingPermissionError, InvalidIdentifierError) as exc:
        print(str(exc), file=sys.stderr)
        return None

    visible = {relation.relation_key for relation in existing}
    model_keys = {model.relation_key for model in manifest.models.values()}
    if not visible or (model_keys and model_keys.isdisjoint(visible)):
        print(
            "Could not read table metadata for the managed datasets; skipping "
            "orphaned-relation discovery. Undeclared sources are still reported.",
            file=sys.stderr,
        )
        return None
    return existing


def _source_last_modified(
    client: WarehouseClient, manifest: Manifest
) -> dict[str, datetime] | None:
    """Last-modified metadata for the source datasets, or None when it can't be read.

    Returns None (the stale-source check is skipped) when the caller lacks read access to
    the source datasets, with a warning; the rest of the scan is unaffected. Mirrors the
    orphan path's `_existing_relations` degradation.
    """

    datasets = manifest.source_datasets()
    if not datasets:
        return None
    try:
        return client.source_last_modified(datasets)
    except (MissingPermissionError, InvalidIdentifierError) as exc:
        print(str(exc), file=sys.stderr)
        return None


def _render(scorecard: Scorecard, config: Config, detail: bool) -> str:
    if config.output_format == "json":
        return render_json(scorecard)
    return render_text(scorecard, detail=detail, top_n=config.top_n)


def _render_orphans(scorecard: Scorecard, config: Config) -> str:
    """The focused `--orphans` report: just orphaned relations and undeclared sources."""

    if config.output_format == "json":
        return render_orphans_json(scorecard)
    return render_orphans_text(scorecard)


def _emit(text: str, output: str | None) -> int:
    """Write the rendered report to a file when `--output` is set, else to stdout.

    Returns the exit code: 0 on success, 2 when the output path cannot be written.
    """

    if output is None:
        print(text)
        return 0
    try:
        Path(output).write_text(text + "\n")
    except OSError as exc:
        print(f"cannot write report to {output}: {exc}", file=sys.stderr)
        return 2
    print(f"wrote report to {output}", file=sys.stderr)
    return 0


def _config_from_args(args: argparse.Namespace) -> Config:
    return Config(
        project_dir=Path(args.project_dir),
        target_path=Path(args.target_path),
        project=args.project,
        region=args.region or "US",
        warehouse=args.warehouse or "bigquery",
        connection=args.connection,
        lookback_days=args.lookback_days,
        query_comment_pattern=args.query_comment_pattern,
        columns=args.columns,
        min_age_days=args.min_age_days,
        rare_threshold=args.rare_threshold,
        stale_source_days=args.stale_source_days,
        output_format=args.format,
        top_n=args.top_n,
        cache=args.cache,
        cache_ttl_hours=args.cache_ttl,
    )


def _run_scan(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    try:
        validate_query_comment_pattern(config.query_comment_pattern)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if config.lookback_days < 1:
        print("--lookback-days must be at least 1.", file=sys.stderr)
        return 2
    if args.clear_cache:
        _clear_project_cache(config.project_dir)
    if not config.manifest_path.exists():
        print(
            f"manifest not found at {config.manifest_path} — run `dbt parse` "
            "(or `dbt docs generate`) first, or pass --target-path.",
            file=sys.stderr,
        )
        return 2

    try:
        manifest = load_manifest(config.manifest_path)
    except ArtifactError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not manifest.models:
        print(
            f"manifest at {config.manifest_path} has no models — is {config.project_dir} "
            "a dbt project? (run `dbt parse` inside it, or pass --project-dir).",
            file=sys.stderr,
        )
        return 2
    try:
        config = replace(config, warehouse=_resolve_warehouse(args.warehouse, manifest))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if config.warehouse == "databricks":
        from dbt_debt.consumption.databricks_queries import exclusion_clause

        try:
            exclusion_clause(config.query_comment_pattern)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.region and config.warehouse != "bigquery":
        print(
            f"--region only applies to BigQuery; ignoring for {config.warehouse}.", file=sys.stderr
        )
    database = config.project or _infer_database(manifest)
    try:
        client = _wrap_cache(_make_client(config, database), config, database)
        scorecard = _scan(config, client, manifest)
    except WarehouseError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.orphans:
        return _emit(_render_orphans(scorecard, config), args.output)
    if _should_view(config, args):
        from dbt_debt.report.viewer import run_viewer

        if run_viewer(scorecard, config):
            return 0
    return _emit(_render(scorecard, config, args.print_report), args.output)


def _clear_project_cache(project_dir: Path) -> None:
    """Delete one project's cached results: the first step of `scan --clear-cache` before it scans."""

    import shutil

    from dbt_debt.consumption.cache import cache_dir_for

    shutil.rmtree(cache_dir_for(project_dir), ignore_errors=True)
    print("cleared this project's scan cache.", file=sys.stderr)


def _clear_all_cache() -> None:
    """Delete every project's cached results: the top-level `dbt-debt --clear-cache`."""

    import shutil

    from dbt_debt.consumption.cache import cache_root

    shutil.rmtree(cache_root(), ignore_errors=True)
    print("cleared the dbt-debt cache.", file=sys.stderr)


def _wrap_cache(client: WarehouseClient, config: Config, project: str | None) -> WarehouseClient:
    """Wrap the client in the TTL disk cache unless `--no-cache` was passed.

    The key parts are exactly the warehouse query parameters; the manifest is intentionally not
    among them, since the cached results depend on the warehouse, not the local artifacts.
    """

    if not config.cache:
        return client
    from datetime import timedelta

    from dbt_debt.consumption.cache import CachingWarehouseClient, cache_dir_for

    # An explicit --cache-ttl governs this run outright; otherwise each entry keeps the TTL it
    # was written with, so `--cache-ttl 2` outlives the session that passed it.
    explicit = config.cache_ttl_hours is not None
    ttl_hours = (
        config.cache_ttl_hours
        if config.cache_ttl_hours is not None
        else Config.DEFAULT_CACHE_TTL_HOURS
    )
    endpoint = ""
    if config.warehouse == "redshift":
        endpoint = os.environ.get("REDSHIFT_HOST", "")
    elif config.warehouse == "databricks":
        from dbt_debt.consumption.databricks import endpoint_identity

        endpoint = endpoint_identity()
    key_parts = {
        "warehouse": config.warehouse,
        "connection": config.connection or "",
        # Redshift's connection is env-var based, so the endpoint joins the key here: two
        # workgroups sharing a database name must never serve each other's cached rows.
        "endpoint": endpoint,
        "project": project or "",
        "region": config.region,
        "lookback_days": str(config.lookback_days),
        "query_comment_pattern": config.query_comment_pattern,
    }
    return CachingWarehouseClient(
        client,
        cache_dir=cache_dir_for(config.project_dir),
        ttl=timedelta(hours=ttl_hours),
        key_parts=key_parts,
        honor_entry_ttl=not explicit,
    )


def _should_view(config: Config, args: argparse.Namespace) -> bool:
    """Open the interactive viewer only with no competing output intent, in a drivable terminal.

    Any explicit output flag (`--print`, `--format json`, `-o`, `--orphans`) means the caller
    wants plain output (for a pipe, a file, or a script), so the viewer stays out of the way.
    Otherwise a human at a real terminal gets the tabbed report by default.
    """

    if args.print_report or args.orphans or args.output is not None:
        return False
    if config.output_format != "text":
        return False
    from dbt_debt.report.viewer import interactive_supported

    return interactive_supported()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbt-debt",
        description="A technical-debt scorecard for dbt projects on BigQuery, Snowflake, "
        "Redshift, and Databricks.",
    )
    # A distinct dest from scan's own --clear-cache: argparse lets a subparser's defaults
    # overwrite values the top-level parser already set, so sharing one dest would make
    # `dbt-debt --clear-cache scan` silently drop the flag.
    parser.add_argument(
        "--clear-cache",
        dest="clear_all_cache",
        action="store_true",
        help="Delete all of dbt-debt's cached results (every project), then exit unless a "
        "command follows.",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    scan = subparsers.add_parser("scan", help="Scan the project and print the debt scorecard.")
    scan.add_argument("--project-dir", default=".", help="dbt project root (default: cwd).")
    scan.add_argument(
        "--target-path", default="target", help="Where manifest.json/catalog.json live."
    )
    scan.add_argument(
        "--warehouse",
        choices=list(SUPPORTED_WAREHOUSES),
        default=None,
        help="Warehouse to scan (default: the manifest's adapter_type).",
    )
    scan.add_argument(
        "--project",
        default=None,
        help="Warehouse database to query — the GCP project on BigQuery, the database on "
        "Snowflake and Redshift, or the catalog on Databricks (default: inferred).",
    )
    scan.add_argument(
        "--region", default=None, help="BigQuery region for INFORMATION_SCHEMA (default: US)."
    )
    scan.add_argument(
        "--connection",
        default=None,
        help="Named Snowflake connection from connections.toml (default: the connector's "
        "default connection).",
    )
    scan.add_argument(
        "--lookback-days", type=int, default=180, help="Usage window (JOBS retention is ~180)."
    )
    scan.add_argument(
        "--query-comment-pattern",
        default=DEFAULT_QUERY_COMMENT_PATTERN,
        help="Regex identifying dbt's own queries, excluded from usage.",
    )
    scan.add_argument(
        "--columns",
        action="store_true",
        help="Analyse columns too (which are unused), not just whole models.",
    )
    scan.add_argument(
        "--min-age-days",
        type=int,
        default=7,
        help="Relations first seen fewer than this many days ago are reported as too new to "
        "judge rather than unused (default: 7; 0 disables the guard).",
    )
    scan.add_argument(
        "--rare-threshold",
        type=int,
        default=5,
        help="Queried models with at most this many queries in the window are reported as "
        "rarely used (default: 5; 0 disables the band).",
    )
    scan.add_argument(
        "--stale-source-days",
        type=int,
        default=30,
        help="Declared sources whose table received no new data for more than this many days "
        "are reported as stale (default: 30; 0 disables the check).",
    )
    scan.add_argument("--format", choices=["text", "json"], default="text")
    scan.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="How many unused assets the summary list shows (default: 10).",
    )
    scan.add_argument(
        "--print",
        dest="print_report",
        action="store_true",
        help="Print the full plain-text report (every unused asset, grouped by model, with "
        "file paths) instead of opening the interactive viewer.",
    )
    scan.add_argument(
        "-o",
        "--output",
        default=None,
        help="Write the report to this file instead of stdout (e.g. --format json -o debt.json).",
    )
    scan.add_argument(
        "--orphans",
        action="store_true",
        help="Print only the orphaned-relation and undeclared-source report (non-interactive).",
    )
    scan.add_argument(
        "--no-cache",
        dest="cache",
        action="store_false",
        help="Always query the warehouse live, ignoring (and not writing) the scan cache.",
    )
    scan.add_argument(
        "--cache-ttl",
        type=float,
        default=None,
        help="Hours a cached scan stays valid before it is re-fetched (default: 1). Remembered "
        "per entry, so it persists across sessions; passing the flag overrides stored values.",
    )
    scan.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear this project's cache first, then run a fresh scan that rebuilds it.",
    )
    scan.set_defaults(func=_run_scan)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.clear_all_cache:
        _clear_all_cache()
        if args.command is None:
            return 0
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        # The spinner clears its own line and the viewer restores the terminal on unwind,
        # so a plain message and the shell's interrupt code are all that's left to do.
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
