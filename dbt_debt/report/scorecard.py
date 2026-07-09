"""Assemble the model-grain scorecard from a manifest, the DAG, and warehouse facts.

Given already-loaded inputs this is deterministic and warehouse-free, so the whole assembly is
testable with a fake client's canned data. Column-grain fields are absent here; the column stage
extends the structure without changing the model lines.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from dbt_debt.artifacts.catalog import Catalog
from dbt_debt.artifacts.graph import Graph
from dbt_debt.config import Config
from dbt_debt.consumption.usage import first_seen_model_ids, model_usage, queried_model_ids
from dbt_debt.domain import ColumnEdge, ColumnRef, Manifest, UsageRow, WarehouseRelation
from dbt_debt.verdict.blockers import analyze_columns
from dbt_debt.verdict.columns import dead_columns
from dbt_debt.verdict.coverage import Coverage, coverage
from dbt_debt.verdict.exposures import affected_exposures, unaffected_exposures
from dbt_debt.verdict.freshness import too_new_models
from dbt_debt.verdict.models import dead_models
from dbt_debt.verdict.orphans import orphaned_relations, undeclared_sources
from dbt_debt.verdict.partitioning import unpartitioned_large_tables
from dbt_debt.verdict.rarity import rarely_used_models
from dbt_debt.verdict.semantic import affected_semantic_consumers
from dbt_debt.verdict.tests import removable_tests


@dataclass(frozen=True)
class AffectedConsumer:
    """A declared consumer fed by a dead model, named for the report.

    Covers exposures and the semantic-layer kinds so the report can say *which* dashboard or
    metric is at risk, not just how many. `kind` is "exposure", "semantic_model", "metric",
    or "saved_query".
    """

    kind: str
    name: str
    unique_id: str


@dataclass(frozen=True)
class DeadModel:
    """A dead buildable node and the storage it would reclaim. `file_path` is the file to remove.

    `resource_type` tags what kind of node died — "model", "seed", or "snapshot" — so the
    renderers can label non-model entries without a second list.
    """

    unique_id: str
    name: str
    relation_key: str
    total_bytes: int
    file_path: str | None = None
    resource_type: str = "model"


@dataclass(frozen=True)
class RarelyUsedModel:
    """A queried node whose few queries put it in the review band, with the usage that dates it.

    `last_queried` is an ISO-8601 string (not a datetime) so the scorecard serializes to JSON
    unchanged; `total_bytes` sizes what a deprecation would reclaim. Never folded into the
    unused figures — observed use, however small, is still use.
    """

    unique_id: str
    name: str
    relation_key: str
    query_count: int
    last_queried: str | None
    total_bytes: int
    file_path: str | None = None
    resource_type: str = "model"


@dataclass(frozen=True)
class UnpartitionedTable:
    """A large BigQuery table built with neither `partition_by` nor `cluster_by` declared.

    Sized by *stored* bytes from the catalog (scan cost is not collected); `materialized` says
    whether it is a plain table or an incremental model.
    """

    unique_id: str
    name: str
    relation_key: str
    total_bytes: int
    materialized: str
    file_path: str | None = None


@dataclass(frozen=True)
class DeadColumn:
    """A dead column; `blocked` flags those not trivially removable, `file_path` its defining model.

    `unique_id` is the owning model's, so downstream verdicts (like removable tests) can rebuild
    the exact (model, column) ref without matching on the display name.
    """

    unique_id: str
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
    affected_exposures: tuple[AffectedConsumer, ...] = ()
    affected_semantic: tuple[AffectedConsumer, ...] = ()
    dead_models: tuple[DeadModel, ...] = field(default_factory=tuple)
    too_new_models: tuple[DeadModel, ...] = ()
    """Unqueried but first seen too recently to judge — a third bucket, not counted unused."""
    rarely_used: tuple[RarelyUsedModel, ...] = ()
    """Queried at most `rare_threshold` times — a review band, never counted unused."""
    rare_threshold: int = 0
    reclaimable_bytes: int = 0
    coverage: Coverage | None = None
    """Test/docs coverage counts; None only on handcrafted scorecards (assembly always sets it)."""
    unpartitioned_tables: tuple[UnpartitionedTable, ...] = ()
    """BigQuery only: large tables declaring neither partition_by nor cluster_by."""
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
    first_seen: Mapping[str, datetime] | None = None,
    now: datetime | None = None,
    catalog_columns: Mapping[str, Sequence[str]] | None = None,
) -> Scorecard:
    """Combine usage, DAG propagation, and manifest traversals into a `Scorecard`.

    `first_seen` (relation_key -> earliest job) drives the too-new guard: an unqueried node
    younger than `config.min_age_days` is set aside as "too new to judge" and excluded from
    every unused-derived figure — the count, the removable tests, the exposure and semantic
    impact, and the reclaimable bytes. Queried nodes with at most `config.rare_threshold`
    queries land in the separate rarely-used review band, which feeds none of those figures
    either. `now` is injectable for tests.
    """

    usage_rows = list(usage_rows)
    queried = queried_model_ids(manifest, usage_rows)
    unqueried = dead_models(manifest, graph, queried)
    first_seen_ids = first_seen_model_ids(manifest, first_seen or {})
    now_utc = now or datetime.now(timezone.utc)
    min_age = timedelta(days=config.min_age_days)
    too_new = too_new_models(unqueried, first_seen_ids, now_utc, min_age)
    dead = unqueried - too_new

    # The rarity band gets the same too-new protection as the dead set: a model created
    # mid-window has not had a full window to accumulate queries.
    usage_by_model = model_usage(manifest, usage_rows)
    rare = rarely_used_models(usage_by_model, config.rare_threshold)
    rare -= too_new_models(rare, first_seen_ids, now_utc, min_age)

    # When the column stage ran, tests guarding a dead column are removable too — rebuild the
    # (model, column) refs the tests verdict compares against from the ranked dead list.
    dead_column_refs: set[ColumnRef] = (
        {(c.unique_id, c.column) for c in column_report.dead_columns}
        if column_report is not None
        else set()
    )

    # Storage reclaimed by dropping the whole dead tables. Only whole dead models have a real
    # figure — BigQuery reports no per-column size, so dead columns are not summed here.
    reclaimable = sum(_model_bytes(manifest, storage_bytes, uid) for uid in dead)

    def rarely_used_entry(uid: str) -> RarelyUsedModel:
        row = usage_by_model[uid]
        return RarelyUsedModel(
            unique_id=uid,
            name=manifest.models[uid].name,
            relation_key=manifest.models[uid].relation_key,
            query_count=row.query_count,
            last_queried=row.last_queried.isoformat() if row.last_queried else None,
            total_bytes=_model_bytes(manifest, storage_bytes, uid),
            file_path=manifest.models[uid].original_file_path,
            resource_type=manifest.models[uid].resource_type,
        )

    def ranked_rarely_used(uids: Set[str]) -> tuple[RarelyUsedModel, ...]:
        ranked = sorted(uids, key=lambda uid: (-_model_bytes(manifest, storage_bytes, uid), uid))
        return tuple(rarely_used_entry(uid) for uid in ranked)

    # The partitioning check is BigQuery-specific: Snowflake micro-partitions automatically and
    # its explicit clustering keys are optional tuning, not debt.
    unpartitioned: tuple[UnpartitionedTable, ...] = ()
    if config.warehouse == "bigquery":
        unpartitioned = tuple(
            UnpartitionedTable(
                unique_id=uid,
                name=manifest.models[uid].name,
                relation_key=manifest.models[uid].relation_key,
                total_bytes=_model_bytes(manifest, storage_bytes, uid),
                materialized=manifest.models[uid].materialized or "table",
                file_path=manifest.models[uid].original_file_path,
            )
            for uid in unpartitioned_large_tables(manifest.models, storage_bytes)
        )

    def ranked_assets(uids: Set[str]) -> tuple[DeadModel, ...]:
        ranked = sorted(uids, key=lambda uid: (-_model_bytes(manifest, storage_bytes, uid), uid))
        return tuple(
            DeadModel(
                unique_id=uid,
                name=manifest.models[uid].name,
                relation_key=manifest.models[uid].relation_key,
                total_bytes=_model_bytes(manifest, storage_bytes, uid),
                file_path=manifest.models[uid].original_file_path,
                resource_type=manifest.models[uid].resource_type,
            )
            for uid in ranked
        )

    return Scorecard(
        project_name=manifest.project_name,
        lookback_days=config.lookback_days,
        active_models=len(manifest.models) - len(unqueried),
        unused_models=len(dead),
        removable_tests=tuple(
            t.unique_id for t in removable_tests(manifest, dead, dead_column_refs)
        ),
        unaffected_exposures=tuple(e.unique_id for e in unaffected_exposures(manifest, dead)),
        affected_exposures=tuple(
            AffectedConsumer(kind="exposure", name=e.name, unique_id=e.unique_id)
            for e in affected_exposures(manifest, dead)
        ),
        affected_semantic=tuple(
            AffectedConsumer(kind=c.kind, name=c.name, unique_id=c.unique_id)
            for c in affected_semantic_consumers(manifest, dead)
        ),
        dead_models=ranked_assets(dead),
        too_new_models=ranked_assets(too_new),
        rarely_used=ranked_rarely_used(rare),
        rare_threshold=config.rare_threshold,
        reclaimable_bytes=reclaimable,
        coverage=coverage(manifest, catalog_columns),
        unpartitioned_tables=unpartitioned,
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
            unique_id=unique_id,
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
