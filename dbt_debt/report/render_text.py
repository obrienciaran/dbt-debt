"""Render a `Scorecard` as the terminal scorecard.

Mirrors the target mock-up at model grain: active/unused counts, the manifest-only savings
(removable tests), and the storage-ranked top dead assets. Column lines
appear once the column stage populates them. By default only the top few dead assets are shown;
`--detail` appends the complete list grouped by model, with each model's file path.
"""

from __future__ import annotations

from dbt_debt.report.scorecard import DeadColumn, DeadModel, OrphanReport, Scorecard

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
    "  (blocked = unused but still backed by a test or enforced contract; review before removing)"
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
        f"  ✗ {scorecard.unused_models} unused",
    ]
    if columns is not None:
        lines += [
            "Columns:",
            f"  ✓ {columns.active} active",
            f"  ✗ {columns.unused} unused",
        ]
    if scorecard.orphans is not None:
        lines += _orphan_summary_lines(scorecard.orphans)

    lines += [
        "",
        "Potential savings:",
    ]
    if columns is not None:
        lines.append(f"  - {_plural(columns.removable, 'column')} removable")
    lines.append(f"  - {_plural(len(scorecard.removable_tests), 'test')} removable")
    if scorecard.affected_exposures:
        lines.append(
            f"  ! {_plural(len(scorecard.affected_exposures), 'exposure')} affected "
            "(review before removing)"
        )
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

    if detail:
        lines += _detail_section(scorecard)

    return "\n".join(lines)


def _detail_section(scorecard: Scorecard) -> list[str]:
    """The full, un-truncated breakdown appended under `--detail`: dead tables, then dead columns.

    Whole unused models are always listed; the per-column breakdown is added when the column stage
    (`--columns`) ran, so column scans show both grains rather than columns alone. The orphan
    breakdown follows when orphan analysis ran.
    """

    lines = _detail_models(scorecard.dead_models)
    columns = scorecard.columns
    if columns is not None:
        lines += _detail_columns(columns.dead_columns)
    if scorecard.orphans is not None:
        lines += _detail_orphans(scorecard.orphans)
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
    """The full orphan breakdown under `--detail`: orphaned relations, then undeclared sources."""

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
        lines.append(f"  - {model.name}{size}{path}")
    return lines


def _format_model(model: DeadModel) -> str:
    if model.total_bytes > 0:
        return f"{model.name} ({humanize_bytes(model.total_bytes)})"
    return model.name


def _format_column(column: DeadColumn) -> str:
    suffix = " (blocked)" if column.blocked else ""
    return f"{column.model_name}.{column.column}{suffix}"
