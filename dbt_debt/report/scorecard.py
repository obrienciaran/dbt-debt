"""Assemble the model-grain scorecard from a manifest, the DAG, and warehouse facts.

Given already-loaded inputs this is deterministic and warehouse-free, so the whole assembly is
testable with a fake client's canned data. Column-grain fields are absent here; the column stage
extends the structure without changing the model lines.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass, field

from dbt_debt.artifacts.catalog import Catalog
from dbt_debt.artifacts.graph import Graph
from dbt_debt.config import Config
from dbt_debt.consumption.usage import queried_model_ids
from dbt_debt.domain import ColumnEdge, ColumnRef, Manifest, UsageRow, WarehouseRelation
from dbt_debt.verdict.blockers import analyze_columns
from dbt_debt.verdict.columns import dead_columns
from dbt_debt.verdict.exposures import affected_exposures, unaffected_exposures
from dbt_debt.verdict.models import dead_models
from dbt_debt.verdict.orphans import orphaned_relations, undeclared_sources
from dbt_debt.verdict.tests import removable_tests


@dataclass(frozen=True)
class DeadModel:
    """A dead model and the storage it would reclaim. `file_path` points at the `.sql` to remove."""

    unique_id: str
    name: str
    relation_key: str
    total_bytes: int
    file_path: str | None = None


@dataclass(frozen=True)
class DeadColumn:
    """A dead column; `blocked` flags those not trivially removable, `file_path` its defining model."""

    model_name: str
    column: str
    blocked: bool
    file_path: str | None = None


@dataclass(frozen=True)
class ColumnReport:
    """The column-grain section, present only when column analysis (`--columns`) ran.

    `dead_columns` is the complete ranked list (not truncated); the text renderer shows the top
    few by default and the whole thing under `--detail`, while JSON always carries all of it.
    """

    active: int
    unused: int
    removable: int
    dead_columns: tuple[DeadColumn, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OrphanReport:
    """The orphan-grain section: warehouse relations dbt does not account for.

    `orphaned_relations` are tables in dbt-managed datasets with no dbt node that nothing reads;
    `undeclared_sources` are relations a model reads that have no dbt node (declare them as
    sources). `orphans_checked` is False when the warehouse table metadata could not be listed
    (missing `bigquery.tables.list`) — undeclared sources are still reported, since they come from
    the manifest, but the orphan list is then empty and not trustworthy.
    """

    orphaned_relations: tuple[WarehouseRelation, ...] = ()
    undeclared_sources: tuple[str, ...] = ()
    orphans_checked: bool = False


@dataclass(frozen=True)
class Scorecard:
    """The assembled result, ready to render. `columns` is set only if the column stage ran.

    `dead_models` is the complete ranked list of dead models (not truncated); display limiting is
    a renderer concern so nothing actionable is discarded at assembly.
    """

    project_name: str
    lookback_days: int
    active_models: int
    unused_models: int
    removable_tests: tuple[str, ...] = ()
    unaffected_exposures: tuple[str, ...] = ()
    affected_exposures: tuple[str, ...] = ()
    dead_models: tuple[DeadModel, ...] = field(default_factory=tuple)
    reclaimable_bytes: int = 0
    columns: ColumnReport | None = None
    orphans: OrphanReport | None = None


def _model_bytes(manifest: Manifest, storage_bytes: Mapping[str, int], unique_id: str) -> int:
    """Logical bytes of a model's relation from the catalog-derived map (0 when unknown)."""

    return storage_bytes.get(manifest.models[unique_id].relation_key, 0)


def build_scorecard(
    manifest: Manifest,
    graph: Graph,
    usage_rows: Iterable[UsageRow],
    storage_bytes: Mapping[str, int],
    config: Config,
    column_report: ColumnReport | None = None,
    orphan_report: OrphanReport | None = None,
) -> Scorecard:
    """Combine usage, DAG propagation, and manifest traversals into a `Scorecard`."""

    queried = queried_model_ids(manifest, usage_rows)
    dead = dead_models(manifest, graph, queried)

    # Storage reclaimed by dropping the whole dead tables. Only whole dead models have a real
    # figure — BigQuery reports no per-column size, so dead columns are not summed here.
    reclaimable = sum(_model_bytes(manifest, storage_bytes, uid) for uid in dead)

    ranked = sorted(dead, key=lambda uid: (-_model_bytes(manifest, storage_bytes, uid), uid))
    dead_assets = tuple(
        DeadModel(
            unique_id=uid,
            name=manifest.models[uid].name,
            relation_key=manifest.models[uid].relation_key,
            total_bytes=_model_bytes(manifest, storage_bytes, uid),
            file_path=manifest.models[uid].original_file_path,
        )
        for uid in ranked
    )

    return Scorecard(
        project_name=manifest.project_name,
        lookback_days=config.lookback_days,
        active_models=len(manifest.models) - len(dead),
        unused_models=len(dead),
        removable_tests=tuple(t.unique_id for t in removable_tests(manifest, dead)),
        unaffected_exposures=tuple(e.unique_id for e in unaffected_exposures(manifest, dead)),
        affected_exposures=tuple(e.unique_id for e in affected_exposures(manifest, dead)),
        dead_models=dead_assets,
        reclaimable_bytes=reclaimable,
        columns=column_report,
        orphans=orphan_report,
    )


def build_column_report(
    manifest: Manifest,
    catalog: Catalog,
    consumed: Set[ColumnRef],
    edges: Iterable[ColumnEdge],
    storage_bytes: Mapping[str, int],
) -> ColumnReport:
    """Assemble the column-grain section: dead vs active, removable, and the full ranked dead list.

    `removable` softens "dead" with the manifest blocker check — a dead column backing a test or
    bound by an enforced contract is *not* trivially removable. Dead columns are ranked by their
    owning model's storage, the best available proxy (BigQuery has no per-column bytes). The whole
    ranked list is kept; the renderer decides how much to show.
    """

    all_columns = {
        (unique_id, column)
        for unique_id in manifest.models
        for column in catalog.model_columns(unique_id)
    }
    dead = dead_columns(all_columns, consumed, edges)
    blockers = {(b.model_unique_id, b.column_name): b for b in analyze_columns(manifest, dead)}
    removable = sum(1 for ref in dead if not blockers[ref].is_blocked)

    def rank_key(ref: ColumnRef) -> tuple[int, str, str]:
        unique_id, column = ref
        return (-_model_bytes(manifest, storage_bytes, unique_id), unique_id, column)

    ranked_dead = tuple(
        DeadColumn(
            model_name=manifest.models[unique_id].name,
            column=column,
            blocked=blockers[(unique_id, column)].is_blocked,
            file_path=manifest.models[unique_id].original_file_path,
        )
        for unique_id, column in sorted(dead, key=rank_key)
    )

    return ColumnReport(
        active=len(all_columns) - len(dead),
        unused=len(dead),
        removable=removable,
        dead_columns=ranked_dead,
    )


def build_orphan_report(
    manifest: Manifest,
    existing: Iterable[WarehouseRelation] | None,
    references: Set[str],
) -> OrphanReport:
    """Assemble the orphan section: undeclared sources (manifest-only) plus orphaned relations.

    `existing` is None when the warehouse table metadata could not be listed (missing
    `bigquery.tables.list`); then `orphans_checked` is False and the orphan list is empty, but
    undeclared sources are still reported because they are recovered from the manifest.
    """

    dbt_keys = manifest.dbt_relation_keys()
    undeclared = undeclared_sources(references, dbt_keys)
    if existing is None:
        return OrphanReport(undeclared_sources=undeclared, orphans_checked=False)
    return OrphanReport(
        orphaned_relations=orphaned_relations(existing, references, dbt_keys),
        undeclared_sources=undeclared,
        orphans_checked=True,
    )
