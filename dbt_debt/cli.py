"""Command-line entry point: argument parsing plus thin orchestration.

`scan` loads dbt's on-disk artifacts, connects to BigQuery, and prints the model-level
scorecard. The warehouse work goes through the `BigQueryClient` Protocol; `_scan` takes the
client as an argument so the orchestration is testable with a fake and no credentials.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from dbt_debt.artifacts.catalog import Catalog, load_catalog
from dbt_debt.artifacts.graph import Graph
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.config import DEFAULT_QUERY_COMMENT_PATTERN, Config
from dbt_debt.consumption.client import (
    BigQueryClient,
    MissingCredentialsError,
    MissingPermissionError,
)
from dbt_debt.consumption.columns import consumed_model_columns
from dbt_debt.consumption.exclusion import validate_query_comment_pattern
from dbt_debt.domain import Manifest, WarehouseRelation
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


def _scan(config: Config, client: BigQueryClient, manifest: Manifest | None = None) -> Scorecard:
    """Orchestrate a scan: preflight, load artifacts, fetch usage, assemble the scorecard.

    `manifest` may be passed in when the caller has already loaded it (to resolve the BigQuery
    project); otherwise it is loaded here. Tests inject a fake client and let it load.
    """

    client.assert_usage_permission()
    if manifest is None:
        manifest = load_manifest(config.manifest_path)
    graph = Graph.from_manifest(manifest)
    with status("Querying job history"):
        usage = client.table_usage()
    catalog = load_catalog(config.catalog_path) if config.catalog_path.exists() else None
    storage = _storage_bytes(catalog)
    with status("Analysing column usage"):
        column_report = _column_report(config, client, manifest, catalog, storage)
    references = model_relation_references(manifest)
    with status("Listing warehouse relations"):
        existing = _existing_relations(client, manifest)
    orphan_report = build_orphan_report(manifest, existing, references)
    return build_scorecard(manifest, graph, usage, storage, config, column_report, orphan_report)


def _storage_bytes(catalog: Catalog | None) -> dict[str, int]:
    """Per-relation logical bytes from catalog.json, for ranking the reclaimable dead assets.

    dbt's adapter records each relation's `num_bytes` during `dbt docs generate`, so sizes come
    from the catalog already on disk rather than a live `INFORMATION_SCHEMA.TABLE_STORAGE` query
    — which needs a stronger grant (`bigquery.tables.list`) that some projects cannot read even
    as Owner. Without a catalog the map is empty and dead assets rank by name.
    """

    if catalog is None:
        return {}
    return {node.relation_key: node.num_bytes for node in catalog.nodes.values()}


def _infer_project(manifest: Manifest) -> str | None:
    """The GCP project the models live in, taken as the most common model `database`.

    `INFORMATION_SCHEMA.JOBS` is project-scoped, so the scan must run in the project where the
    relations (and their queries) live — which is exactly each model's BigQuery `database`. This
    lets the tool target the right project without a flag, the way dbt itself does.
    """

    databases = Counter(m.database for m in manifest.models.values() if m.database)
    return databases.most_common(1)[0][0] if databases else None


def _column_report(
    config: Config,
    client: BigQueryClient,
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
    if catalog is None:
        print(
            f"catalog not found at {config.catalog_path}; skipping column analysis "
            "(run `dbt docs generate`). Reporting model-level only.",
            file=sys.stderr,
        )
        return None

    schema = build_schema(catalog.relation_columns())
    consumed = consumed_model_columns(client.query_texts(), schema, manifest.relation_to_id())
    edges = SqlglotLineage(manifest, catalog, schema=schema).edges()
    return build_column_report(manifest, catalog, consumed, edges, storage)


def _existing_relations(
    client: BigQueryClient, manifest: Manifest
) -> list[WarehouseRelation] | None:
    """List warehouse relations in dbt-managed datasets, or None when they can't be read.

    Returns None — orphaned-relation discovery is skipped — when there are no managed datasets,
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
    except MissingPermissionError as exc:
        print(str(exc), file=sys.stderr)
        return None

    visible = {relation.relation_key for relation in existing}
    model_keys = {model.relation_key for model in manifest.models.values()}
    if not visible or (model_keys and model_keys.isdisjoint(visible)):
        print(
            "Could not read table metadata for the managed datasets (need read access, e.g. "
            "roles/bigquery.metadataViewer or dataViewer); skipping orphaned-relation discovery. "
            "Undeclared sources are still reported.",
            file=sys.stderr,
        )
        return None
    return existing


def _render(scorecard: Scorecard, config: Config, detail: bool) -> str:
    if config.output_format == "json":
        return render_json(scorecard)
    return render_text(scorecard, detail=detail, top_n=config.top_n)


def _render_orphans(scorecard: Scorecard, config: Config) -> str:
    """The focused `--orphans` report: just orphaned relations and undeclared sources."""

    if config.output_format == "json":
        return render_orphans_json(scorecard)
    return render_orphans_text(scorecard)


def _emit(text: str, output: str | None) -> None:
    """Write the rendered report to a file when `--output` is set, else to stdout."""

    if output is None:
        print(text)
        return
    Path(output).write_text(text + "\n")
    print(f"wrote report to {output}", file=sys.stderr)


def _config_from_args(args: argparse.Namespace) -> Config:
    return Config(
        project_dir=Path(args.project_dir),
        target_path=Path(args.target_path),
        project=args.project,
        region=args.region,
        lookback_days=args.lookback_days,
        query_comment_pattern=args.query_comment_pattern,
        columns=args.columns,
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
    if args.clear_cache:
        _clear_project_cache(config.project_dir)
    if not config.manifest_path.exists():
        print(
            f"manifest not found at {config.manifest_path} — run `dbt parse` "
            "(or `dbt docs generate`) first, or pass --target-path.",
            file=sys.stderr,
        )
        return 2

    from dbt_debt.consumption.bigquery import RealBigQueryClient

    manifest = load_manifest(config.manifest_path)
    project = config.project or _infer_project(manifest)
    try:
        client: BigQueryClient = RealBigQueryClient(config, project=project)
        client = _wrap_cache(client, config, project)
        scorecard = _scan(config, client, manifest)
    except (MissingCredentialsError, MissingPermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.orphans:
        _emit(_render_orphans(scorecard, config), args.output)
        return 0
    if _should_view(config, args):
        from dbt_debt.report.viewer import run_viewer

        if run_viewer(scorecard, config):
            return 0
    _emit(_render(scorecard, config, args.detail), args.output)
    return 0


def _clear_project_cache(project_dir: Path) -> None:
    """Delete one project's cached results — the first step of `scan --clear-cache` before it scans."""

    import shutil

    from dbt_debt.consumption.cache import cache_dir_for

    shutil.rmtree(cache_dir_for(project_dir), ignore_errors=True)
    print("cleared this project's scan cache.", file=sys.stderr)


def _clear_all_cache() -> None:
    """Delete every project's cached results — the top-level `dbt-debt --clear-cache`."""

    import shutil

    from dbt_debt.consumption.cache import cache_root

    shutil.rmtree(cache_root(), ignore_errors=True)
    print("cleared the dbt-debt cache.", file=sys.stderr)


def _wrap_cache(client: BigQueryClient, config: Config, project: str | None) -> BigQueryClient:
    """Wrap the client in the TTL disk cache unless `--no-cache` was passed.

    The key parts are exactly the warehouse query parameters; the manifest is intentionally not
    among them, since the cached results depend on the warehouse, not the local artifacts.
    """

    if not config.cache:
        return client
    from datetime import timedelta

    from dbt_debt.consumption.cache import CachingBigQueryClient, cache_dir_for

    key_parts = {
        "project": project or "",
        "region": config.region,
        "lookback_days": str(config.lookback_days),
        "query_comment_pattern": config.query_comment_pattern,
    }
    return CachingBigQueryClient(
        client,
        cache_dir=cache_dir_for(config.project_dir),
        ttl=timedelta(hours=config.cache_ttl_hours),
        key_parts=key_parts,
    )


def _should_view(config: Config, args: argparse.Namespace) -> bool:
    """Open the interactive viewer only with no competing output intent, in a drivable terminal.

    Any explicit output flag (`--detail`, `--format json`, `-o`, `--no-interactive`) means the
    caller wants plain output — for a pipe, a file, or a script — so the viewer stays out of the
    way. Otherwise a human at a real terminal gets the tabbed report by default.
    """

    if args.no_interactive or args.detail or args.orphans or args.output is not None:
        return False
    if config.output_format != "text":
        return False
    from dbt_debt.report.viewer import interactive_supported

    return interactive_supported()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbt-debt",
        description="A technical-debt scorecard for dbt projects on BigQuery.",
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
        "--project",
        default=None,
        help="GCP project to query (default: inferred from the models' database).",
    )
    scan.add_argument("--region", default="US", help="BigQuery region for INFORMATION_SCHEMA.")
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
    scan.add_argument("--format", choices=["text", "json"], default="text")
    scan.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="How many unused assets the summary list shows (default: 10).",
    )
    scan.add_argument(
        "--detail",
        action="store_true",
        help="List every unused asset (grouped by model, with file paths), not just the top few.",
    )
    scan.add_argument(
        "-o",
        "--output",
        default=None,
        help="Write the report to this file instead of stdout (e.g. --format json -o debt.json).",
    )
    scan.add_argument(
        "--no-interactive",
        action="store_true",
        help="Print the report instead of opening the interactive viewer (for scripts/non-TTY).",
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
        help="Always query BigQuery live, ignoring (and not writing) the scan cache.",
    )
    scan.add_argument(
        "--cache-ttl",
        type=float,
        default=1.0,
        help="Hours a cached scan stays valid before it is re-fetched (default: 1).",
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
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
