"""Render a `Scorecard` as the terminal scorecard.

Mirrors the target mock-up at model grain: active/unused counts, the manifest-only savings
(removable tests), and the storage-ranked top dead assets. Column lines
appear once the column stage populates them. By default only the top few dead assets are shown;
`--print` (the renderer's `detail` flag) appends the complete list grouped by model, with each
model's file path.
"""

from __future__ import annotations

from dbt_debt.report.scorecard import (
    DeadColumn,
    DeadModel,
    OrphanReport,
    PhantomColumn,
    RarelyUsedModel,
    Scorecard,
    StaleSource,
    UnusedSource,
)
from dbt_debt.verdict.coverage import Coverage

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def humanize_bytes(num: int) -> str:
    """A compact human-readable size, e.g. 1536 -> "1.5 KB"."""

    size = float(num)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    # Unreachable: the loop always returns on the last unit. Kept because mypy cannot prove
    # _UNITS is non-empty, so without it the function reads as missing a return on some path.
    return f"{num} B"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


_BLOCKED_LEGEND = (
    "  (blocked = unused but still backed by a test, enforced contract, or semantic model; "
    "review before removing)"
)


def render_text(scorecard: Scorecard, *, detail: bool = False, top_n: int = 10) -> str:
    """Render the scorecard to a printable string.

    `top_n` caps the summary "top dead assets" list; `detail` appends the full per-model breakdown.
    """

    columns = scorecard.columns
    lines: list[str] = [
        f"dbt-debt scorecard — {scorecard.project_name}",
        f"Lookback: {scorecard.lookback_days} days",
        "",
        "Models:",
        f"  ✓ {scorecard.active_models} active",
        f"  ✗ {scorecard.unused_models} unused{_dead_kind_breakdown(scorecard.dead_models)}",
    ]
    if scorecard.rarely_used:
        lines.append(
            f"  ~ {len(scorecard.rarely_used)} rarely used "
            f"(at most {scorecard.rare_threshold} queries; not counted in 'unused')"
        )
    if scorecard.too_new_models:
        lines.append(
            f"  ? {len(scorecard.too_new_models)} too new to judge "
            "(first seen recently; not counted in 'unused')"
        )
    if scorecard.missing_first_seen:
        lines.append(
            f"  ? {len(scorecard.missing_first_seen)} missing a first-seen date "
            "(likely new tables; not counted in 'unused')"
        )
    if columns is not None:
        lines += [
            "Columns:",
            f"  ✓ {columns.active} active",
            f"  ✗ {columns.unused} unused",
        ]
        inspected = columns.parsed_queries + columns.unparseable_queries
        if inspected:
            noun = "query" if inspected == 1 else "queries"
            lines.append(
                f"  (column verdicts based on {_pct(columns.parsed_queries, inspected)} of "
                f"query text — {columns.parsed_queries} of {inspected} {noun} parsed; "
                "usage verdicts are unaffected)"
            )
    if scorecard.unused_sources or scorecard.stale_sources:
        lines.append("Sources:")
        if scorecard.unused_sources:
            lines.append(
                f"  ✗ {_plural(len(scorecard.unused_sources), 'declared source')} "
                "nothing in the project reads"
            )
        if scorecard.stale_sources:
            lines.append(
                f"  ! {_plural(len(scorecard.stale_sources), 'source')} stale "
                f"(no new data in {scorecard.stale_days}+ days)"
            )
    if scorecard.phantom_columns:
        count = len(scorecard.phantom_columns)
        verb = "exists" if count == 1 else "exist"
        lines += [
            "Docs drift:",
            f"  ! {_plural(count, 'documented column')} no longer {verb} in the table",
        ]
    if scorecard.orphans is not None:
        lines += _orphan_summary_lines(scorecard.orphans)
    if scorecard.coverage is not None:
        lines += _coverage_lines(scorecard.coverage)

    lines += [
        "",
        "Potential savings:",
    ]
    if columns is not None:
        lines.append(f"  - {_plural(columns.removable, 'column')} removable")
    lines.append(f"  - {_plural(len(scorecard.removable_tests), 'test')} removable")
    if scorecard.dead_exposures:
        lines.append(
            f"  ! {_plural(len(scorecard.dead_exposures), 'exposure')} fed only by unused "
            "models (likely dead)"
        )
        lines += [f"      - {e.name}" for e in scorecard.dead_exposures]
    if scorecard.affected_exposures:
        lines.append(
            f"  ! {_plural(len(scorecard.affected_exposures), 'exposure')} affected "
            "(review before removing)"
        )
        lines += [f"      - {e.name}" for e in scorecard.affected_exposures]
    if scorecard.affected_semantic:
        lines.append(
            f"  ! {_plural(len(scorecard.affected_semantic), 'semantic-layer consumer')} "
            "affected (review before removing)"
        )
        lines += [
            f"      - {c.name} ({_CONSUMER_LABELS.get(c.kind, c.kind)})"
            for c in scorecard.affected_semantic
        ]
    if scorecard.reclaimable_bytes > 0:
        lines.append(f"  - {humanize_bytes(scorecard.reclaimable_bytes)} reclaimable storage")

    # The summary list is columns when column analysis ran, else models.
    if columns is not None and columns.dead_columns:
        total = len(columns.dead_columns)
        shown = columns.dead_columns[:top_n]
        lines += [
            "",
            f"Top {len(shown)} of {total} unused columns "
            "(ranked by table bytes; BigQuery has no per-column sizes):",
        ]
        lines += [f"  {i}. {_format_column(c)}" for i, c in enumerate(shown, start=1)]
        if any(c.blocked for c in shown):
            lines += ["", _BLOCKED_LEGEND]
    elif scorecard.dead_models:
        total = len(scorecard.dead_models)
        shown_models = scorecard.dead_models[:top_n]
        lines += [
            "",
            f"Top {len(shown_models)} of {total} unused models (most reclaimable storage first):",
        ]
        lines += [f"  {i}. {_format_model(m)}" for i, m in enumerate(shown_models, start=1)]

    if scorecard.rarely_used:
        total = len(scorecard.rarely_used)
        shown_rare = scorecard.rarely_used[:top_n]
        lines += [
            "",
            f"Top {len(shown_rare)} of {total} rarely used models "
            f"(at most {scorecard.rare_threshold} queries in {scorecard.lookback_days} days; "
            "largest first):",
        ]
        lines += [f"  {i}. {_format_rare(m)}" for i, m in enumerate(shown_rare, start=1)]

    if scorecard.unpartitioned_tables:
        count = len(scorecard.unpartitioned_tables)
        lines += [
            "",
            f"Large tables with neither partition_by nor cluster_by ({count}; every query "
            "scans them in full):",
        ]
        for table in scorecard.unpartitioned_tables:
            size = humanize_bytes(table.total_bytes)
            lines.append(f"  - {table.name} ({size}, {table.materialized})")

    if detail:
        lines += _detail_section(scorecard)

    return "\n".join(lines)


def _detail_section(scorecard: Scorecard) -> list[str]:
    """The full, un-truncated breakdown of the Detail view and `--print`: tables, then columns.

    Whole unused models are always listed; the per-column breakdown is added when the column stage
    (`--columns`) ran, so column scans show both grains rather than columns alone. The remaining
    sections follow the summary's order — sources, docs drift, orphans, then the removable tests
    under "potential savings".
    """

    lines = _detail_models(scorecard.dead_models)
    if scorecard.rarely_used:
        lines += ["", f"Rarely used models ({len(scorecard.rarely_used)}):"]
        for rare in scorecard.rarely_used:
            path = f"  {rare.file_path}" if rare.file_path else ""
            lines.append(f"  - {_format_rare(rare)}{path}")
    if scorecard.too_new_models:
        lines += ["", f"Too new to judge ({len(scorecard.too_new_models)}):"]
        for model in scorecard.too_new_models:
            path = f"  {model.file_path}" if model.file_path else ""
            lines.append(f"  - {model.name}{_kind_tag(model)}{path}")
    if scorecard.missing_first_seen:
        lines += [
            "",
            f"Missing a first-seen date — likely new tables ({len(scorecard.missing_first_seen)}):",
        ]
        for model in scorecard.missing_first_seen:
            path = f"  {model.file_path}" if model.file_path else ""
            lines.append(f"  - {model.name}{_kind_tag(model)}{path}")
        lines.append(
            "  (Snowflake's ACCOUNT_USAGE.TABLES lags ~90 minutes behind reality; "
            "re-scan later to judge these)"
        )
    if scorecard.unpartitioned_tables:
        count = len(scorecard.unpartitioned_tables)
        lines += ["", f"Large tables with neither partition_by nor cluster_by ({count}):"]
        for table in scorecard.unpartitioned_tables:
            path = f"  {table.file_path}" if table.file_path else ""
            size = humanize_bytes(table.total_bytes)
            lines.append(f"  - {table.name} ({size}, {table.materialized}){path}")
    columns = scorecard.columns
    if columns is not None:
        lines += _detail_columns(columns.dead_columns)
    if scorecard.unused_sources:
        lines += _detail_unused_sources(scorecard.unused_sources)
    if scorecard.stale_sources:
        lines += _detail_stale_sources(scorecard.stale_sources, scorecard.stale_days)
    if scorecard.phantom_columns:
        lines += _detail_phantom_columns(scorecard.phantom_columns)
    if scorecard.orphans is not None:
        lines += _detail_orphans(scorecard.orphans)
    if scorecard.removable_tests:
        lines += _detail_removable_tests(scorecard.removable_tests)
    return lines


def _detail_removable_tests(test_ids: tuple[str, ...]) -> list[str]:
    """Each removable test by unique_id — removable only once the dead asset it guards goes."""

    lines = ["", f"Removable tests ({len(test_ids)}):"]
    lines += [f"  - {test_id}" for test_id in test_ids]
    lines.append("  (removable once the unused model or column each one guards is removed)")
    return lines


def _detail_stale_sources(sources: tuple[StaleSource, ...], stale_days: int) -> list[str]:
    """Each stale source with its last data change and file path, stalest first."""

    lines = ["", f"Stale sources (no new data in {stale_days}+ days; {len(sources)}):"]
    for source in sources:
        path = f"  {source.file_path}" if source.file_path else ""
        lines.append(f"  - {source.name}  (last data {source.last_modified[:10]}){path}")
    return lines


def _detail_phantom_columns(columns: tuple[PhantomColumn, ...]) -> list[str]:
    """Declared-but-missing columns grouped by model, with the YAML to fix.

    Compared against catalog.json, so a stale catalog can false-positive; the closing note
    says how to refresh it.
    """

    groups: dict[str, list[PhantomColumn]] = {}
    for column in columns:
        groups.setdefault(column.model_name, []).append(column)
    lines = ["", f"Documented columns missing from the table ({len(columns)}):"]
    for model_name, model_columns in groups.items():
        path = model_columns[0].file_path
        lines.append(f"  {model_name}" + (f"  {path}" if path else ""))
        lines += [f"    - {column.column}" for column in model_columns]
    lines.append("  (compared against catalog.json; run `dbt docs generate` if it is stale)")
    return lines


def _detail_unused_sources(sources: tuple[UnusedSource, ...]) -> list[str]:
    """Each unused source with its file path and any direct-query evidence.

    A queried entry means people read the raw table without going through dbt (consider
    modelling it); an unqueried one is a dead declaration in sources.yml.
    """

    lines = ["", f"Declared sources nothing in the project reads ({len(sources)}):"]
    for source in sources:
        if source.query_count:
            count = source.query_count
            evidence = [f"{count} query" if count == 1 else f"{count} queries"]
            if source.last_queried:
                evidence.append(f"last {source.last_queried[:10]}")
            usage = f"  (queried directly: {', '.join(evidence)})"
        else:
            usage = "  (no queries seen)"
        path = f"  {source.file_path}" if source.file_path else ""
        lines.append(f"  - {source.name}{usage}{path}")
    return lines


def _pct(count: int, total: int) -> str:
    return f"{100 * count / total:.0f}%" if total else "0%"


def _coverage_lines(cov: Coverage) -> list[str]:
    """The three one-sentence hygiene figures: tests, table-level docs, column-level docs."""

    lines = [
        "Coverage:",
        f"  - tests: {cov.tested_models} of {_plural(cov.total_models, 'model')} have at "
        f"least one test ({_pct(cov.tested_models, cov.total_models)})",
        f"  - model docs: {cov.documented_models} of {_plural(cov.total_models, 'model')} have a "
        f"description ({_pct(cov.documented_models, cov.total_models)})",
    ]
    if cov.total_columns:
        source = "catalog" if cov.column_source == "catalog" else "declared"
        lines.append(
            f"  - column docs: {cov.documented_columns} of {_plural(cov.total_columns, 'column')} "
            f"have a description ({_pct(cov.documented_columns, cov.total_columns)}, "
            f"{source} columns)"
        )
    return lines


def _orphan_summary_lines(orphans: OrphanReport) -> list[str]:
    """The summary `Orphans:` block — count of orphaned relations and undeclared sources."""

    lines = ["Orphans:"]
    if orphans.orphans_checked:
        count = len(orphans.orphaned_relations)
        lines.append(f"  ✗ {_plural(count, 'table')} in managed datasets with no dbt model")
    else:
        lines.append(
            "  ⚠ orphan check skipped — needs bigquery.tables.list (roles/bigquery.metadataViewer)"
        )
    if orphans.undeclared_sources:
        count = len(orphans.undeclared_sources)
        lines.append(f"  ! {_plural(count, 'source')} found but not declared in the manifest")
    return lines


def _detail_orphans(orphans: OrphanReport) -> list[str]:
    """The full orphan breakdown in the detail view: orphaned relations, then undeclared sources."""

    lines: list[str] = []
    if orphans.orphans_checked:
        relations = orphans.orphaned_relations
        lines += ["", f"Orphaned tables ({len(relations)}):"]
        for relation in relations:
            lines.append(f"  - {relation.relation_key}  ({relation.relation_type})")
    else:
        lines += ["", "Orphaned tables: skipped — needs bigquery.tables.list"]
    if orphans.undeclared_sources:
        sources = orphans.undeclared_sources
        lines += ["", f"Sources found but not declared in the manifest ({len(sources)}):"]
        for relation_key in sources:
            lines.append(f"  - {relation_key}")
    return lines


def render_orphans_text(scorecard: Scorecard) -> str:
    """The focused `--orphans` report: just the orphan summary and full breakdown."""

    lines = [f"dbt-debt orphans — {scorecard.project_name}", ""]
    orphans = scorecard.orphans
    if orphans is None:
        lines.append("Orphan analysis did not run (no dbt-managed datasets).")
        return "\n".join(lines)
    lines += _orphan_summary_lines(orphans)
    lines += _detail_orphans(orphans)
    return "\n".join(lines)


def _detail_columns(dead_columns: tuple[DeadColumn, ...]) -> list[str]:
    if not dead_columns:
        return []
    groups: dict[str, list[DeadColumn]] = {}
    for column in dead_columns:
        groups.setdefault(column.model_name, []).append(column)

    lines = ["", f"Unused columns ({len(dead_columns)}):"]
    for model_name, model_columns in groups.items():
        path = model_columns[0].file_path
        lines.append(f"  {model_name}" + (f"  {path}" if path else ""))
        for column in model_columns:
            suffix = "  (blocked)" if column.blocked else ""
            lines.append(f"    - {column.column}{suffix}")
    if any(column.blocked for column in dead_columns):
        lines += ["", _BLOCKED_LEGEND]
    return lines


def _detail_models(dead_models: tuple[DeadModel, ...]) -> list[str]:
    if not dead_models:
        return []
    lines = ["", f"Unused models ({len(dead_models)}):"]
    for model in dead_models:
        size = f"  {humanize_bytes(model.total_bytes)}" if model.total_bytes > 0 else ""
        path = f"  {model.file_path}" if model.file_path else ""
        lines.append(f"  - {model.name}{_kind_tag(model)}{size}{path}")
    return lines


def _kind_tag(model: DeadModel) -> str:
    """A ` (seed)` / ` (snapshot)` label; plain models carry no tag."""

    return f" ({model.resource_type})" if model.resource_type != "model" else ""


_CONSUMER_LABELS = {
    "semantic_model": "semantic model",
    "metric": "metric",
    "saved_query": "saved query",
}


def _dead_kind_breakdown(dead_models: tuple[DeadModel, ...]) -> str:
    """A ` (incl. 2 seeds, 1 snapshot)` suffix when non-model nodes are among the dead."""

    parts = [
        _plural(count, kind)
        for kind in ("seed", "snapshot")
        if (count := sum(1 for m in dead_models if m.resource_type == kind))
    ]
    return f" (incl. {', '.join(parts)})" if parts else ""


def _format_model(model: DeadModel) -> str:
    size = f" ({humanize_bytes(model.total_bytes)})" if model.total_bytes > 0 else ""
    return f"{model.name}{_kind_tag(model)}{size}"


def _format_rare(model: RarelyUsedModel) -> str:
    """`name (2 queries, last 2026-06-14, 1.2 GB)` — the evidence an owner needs to judge it."""

    count = model.query_count
    parts = [f"{count} query" if count == 1 else f"{count} queries"]
    if model.last_queried:
        parts.append(f"last {model.last_queried[:10]}")
    if model.total_bytes > 0:
        parts.append(humanize_bytes(model.total_bytes))
    kind = f" ({model.resource_type})" if model.resource_type != "model" else ""
    return f"{model.name}{kind} ({', '.join(parts)})"


def _format_column(column: DeadColumn) -> str:
    suffix = " (blocked)" if column.blocked else ""
    return f"{column.model_name}.{column.column}{suffix}"
