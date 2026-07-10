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
from dbt_debt.consumption.columns import ColumnConsumption
from dbt_debt.consumption.usage import first_seen_model_ids, model_usage, queried_model_ids
from dbt_debt.domain import (
    ColumnEdge,
    ColumnRef,
    Manifest,
    Relation,
    TableStorage,
    UsageRow,
    WarehouseRelation,
)
from dbt_debt.verdict.blockers import analyze_columns
from dbt_debt.verdict.columns import dead_columns
from dbt_debt.verdict.coverage import Coverage, coverage
from dbt_debt.verdict.drift import phantom_columns
from dbt_debt.verdict.exposures import affected_exposures, dead_exposures, unaffected_exposures
from dbt_debt.verdict.freshness import missing_first_seen_models, too_new_models
from dbt_debt.verdict.models import dead_models
from dbt_debt.verdict.orphans import orphaned_relations, undeclared_sources
from dbt_debt.verdict.partitioning import unpartitioned_large_tables
from dbt_debt.verdict.rarity import rarely_used_models
from dbt_debt.verdict.semantic import AffectedSemanticConsumer, affected_semantic_consumers
from dbt_debt.verdict.sources import unused_sources
from dbt_debt.verdict.staleness import stale_sources
from dbt_debt.verdict.tests import removable_tests


@dataclass(frozen=True)
class AffectedConsumer:
    """A declared consumer fed by a dead model, named for the report.

    Covers exposures and the semantic-layer kinds so the report can say *which* dashboard or
    metric is at risk, not just how many. `kind` is "exposure", "semantic_model", "metric",
    or "saved_query". `via_name` / `via_kind` name what makes it affected: the unused model
    (kind "model", "seed", or "snapshot") for consumers sitting directly on one, or the
    affected consumer in between for transitive hops. Both are None when the cause is not
    resolved (exposures today).
    """

    kind: str
    name: str
    unique_id: str
    via_name: str | None = None
    via_kind: str | None = None


@dataclass(frozen=True)
class DeadModel:
    """A dead buildable node and the storage it would reclaim. `file_path` is the file to remove.

    `resource_type` tags what kind of node died — "model", "seed", or "snapshot" — so the
    renderers can label non-model entries without a second list. On Snowflake,
    `time_travel_bytes` and `failsafe_bytes` are the retained copies the account still pays
    for on top of `total_bytes` (live data); both are 0 on BigQuery, which has no equivalent.
    """

    unique_id: str
    name: str
    relation_key: str
    total_bytes: int
    file_path: str | None = None
    resource_type: str = "model"
    time_travel_bytes: int = 0
    failsafe_bytes: int = 0


@dataclass(frozen=True)
class RarelyUsedModel:
    """A queried node whose few queries put it in the review band, with the usage that dates it.

    `last_queried` is an ISO-8601 string (not a datetime) so the scorecard serializes to JSON
    unchanged; `total_bytes` sizes what a deprecation would reclaim, and `bytes_scanned` is
    what the few queries read over the window — high scanned bytes on a rarely used model is
    the "expensive but rarely used" deprecation argument. Never folded into the unused
    figures — observed use, however small, is still use.
    """

    unique_id: str
    name: str
    relation_key: str
    query_count: int
    last_queried: str | None
    total_bytes: int
    bytes_scanned: int = 0
    file_path: str | None = None
    resource_type: str = "model"


@dataclass(frozen=True)
class UnusedSource:
    """A declared source nothing in the project reads, with any direct-query evidence.

    `query_count` / `last_queried` / `bytes_scanned` come from the same usage rows the model
    verdicts join: a non-zero count means people query the raw table directly (worth
    modelling, not just deleting the declaration), zero means the declaration is dead weight
    in sources.yml, and the scanned bytes size how much those direct reads cost.
    `last_queried` is an ISO-8601 string so the scorecard serializes to JSON unchanged.
    """

    unique_id: str
    name: str
    relation_key: str
    query_count: int
    last_queried: str | None = None
    bytes_scanned: int = 0
    file_path: str | None = None


@dataclass(frozen=True)
class StaleSource:
    """A declared source whose table has received no new data past the staleness threshold.

    `last_modified` is an ISO-8601 string (like `RarelyUsedModel.last_queried`) so the
    scorecard serializes to JSON unchanged. A review list: the loader upstream of dbt has
    likely stopped, which no unused figure captures.
    """

    unique_id: str
    name: str
    relation_key: str
    last_modified: str
    file_path: str | None = None


@dataclass(frozen=True)
class PhantomColumn:
    """A column declared in a model's YAML that no longer exists in the built relation.

    Stale documentation to delete; compared against catalog.json, so a stale catalog can
    false-positive (regenerate docs first).
    """

    unique_id: str
    model_name: str
    column: str
    file_path: str | None = None


@dataclass(frozen=True)
class UnpartitionedTable:
    """A large BigQuery table built with neither `partition_by` nor `cluster_by` declared.

    `total_bytes` is the *stored* size from the catalog; `bytes_scanned` is what user queries
    read from it over the window (every one a full scan, since nothing prunes) — the list is
    ranked by it, so the top entry is the partitioning fix that saves the most. `materialized`
    says whether it is a plain table or an incremental model.
    """

    unique_id: str
    name: str
    relation_key: str
    total_bytes: int
    materialized: str
    bytes_scanned: int = 0
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
    few by default and the whole thing in the detail view, while JSON always carries all of it.
    """

    active: int
    unused: int
    removable: int
    dead_columns: tuple[DeadColumn, ...] = field(default_factory=tuple)
    parsed_queries: int = 0
    """How many query texts sqlglot parsed — with `unparseable_queries`, the confidence figure."""
    unparseable_queries: int = 0
    """Query texts sqlglot could not parse; their column reads are invisible to the verdicts.
    Never affects usage verdicts, which do not come from query text."""


@dataclass(frozen=True)
class OrphanedRelation:
    """A table in a dbt-managed dataset with no dbt node, plus any direct-query evidence.

    `query_count` / `last_queried` / `bytes_scanned` come from the same usage rows the model
    verdicts join: a non-zero count means people still query the orphan directly — the
    dangerous-to-drop ones — while zero means nothing read it all window. `last_queried` is an
    ISO-8601 string so the scorecard serializes to JSON unchanged.
    """

    relation_key: str
    relation_type: str
    query_count: int = 0
    last_queried: str | None = None
    bytes_scanned: int = 0


@dataclass(frozen=True)
class OrphanReport:
    """The orphan-grain section: warehouse relations dbt does not account for.

    `orphaned_relations` are tables in dbt-managed datasets with no dbt node that no model reads;
    `undeclared_sources` are relations a model reads that have no dbt node (declare them as
    sources). `orphans_checked` is False when the warehouse table metadata could not be listed
    (missing `bigquery.tables.list`) — undeclared sources are still reported, since they come from
    the manifest, but the orphan list is then empty and not trustworthy.
    """

    orphaned_relations: tuple[OrphanedRelation, ...] = ()
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
    dead_exposures: tuple[AffectedConsumer, ...] = ()
    """Exposures whose every model dependency is dead — the consumer itself is likely dead."""
    affected_semantic: tuple[AffectedConsumer, ...] = ()
    dead_models: tuple[DeadModel, ...] = field(default_factory=tuple)
    too_new_models: tuple[DeadModel, ...] = ()
    """Unqueried but first seen too recently to judge — a third bucket, not counted unused."""
    missing_first_seen: tuple[DeadModel, ...] = ()
    """Snowflake only: unqueried nodes with no first-seen date yet (ACCOUNT_USAGE.TABLES lags,
    so these are likely new tables) — set aside like the too-new bucket, not counted unused."""
    rarely_used: tuple[RarelyUsedModel, ...] = ()
    """Queried at most `rare_threshold` times — a review band, never counted unused."""
    rare_threshold: int = 0
    reclaimable_bytes: int = 0
    coverage: Coverage | None = None
    """Test/docs coverage counts; None only on handcrafted scorecards (assembly always sets it)."""
    unpartitioned_tables: tuple[UnpartitionedTable, ...] = ()
    """BigQuery only: large tables declaring neither partition_by nor cluster_by."""
    unused_sources: tuple[UnusedSource, ...] = ()
    """Declared sources nothing in the project reads; a review list, never counted unused."""
    stale_sources: tuple[StaleSource, ...] = ()
    """Declared sources with no new data past the threshold; a review list."""
    stale_days: int = 0
    stale_checked: bool = False
    """False when the check is disabled or the source metadata could not be read."""
    phantom_columns: tuple[PhantomColumn, ...] = ()
    """YAML-declared columns missing from the built relation, per catalog.json."""
    columns: ColumnReport | None = None
    orphans: OrphanReport | None = None


def _affected_semantic_entry(
    manifest: Manifest, verdict: AffectedSemanticConsumer
) -> AffectedConsumer:
    """Resolve the verdict's `via` unique_id to the name and kind the renderers print."""

    if verdict.via in manifest.models:
        cause = manifest.models[verdict.via]
        via_name, via_kind = cause.name, cause.resource_type
    else:
        upstream = manifest.semantic_consumers[verdict.via]
        via_name, via_kind = upstream.name, upstream.kind
    consumer = verdict.consumer
    return AffectedConsumer(
        kind=consumer.kind,
        name=consumer.name,
        unique_id=consumer.unique_id,
        via_name=via_name,
        via_kind=via_kind,
    )


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
    last_modified: Mapping[str, datetime] | None = None,
    table_storage: Mapping[str, TableStorage] | None = None,
) -> Scorecard:
    """Combine usage, DAG propagation, and manifest traversals into a `Scorecard`.

    `first_seen` (relation_key -> earliest job) drives the too-new guard: an unqueried node
    younger than `config.min_age_days` is set aside as "too new to judge" and excluded from
    every unused-derived figure — the count, the removable tests, the exposure and semantic
    impact, and the reclaimable bytes. On Snowflake an unqueried node with no first-seen date
    at all is set aside the same way (as "missing a first-seen date, likely a new table"), since
    ACCOUNT_USAGE.TABLES lags behind reality. Queried nodes with at most `config.rare_threshold`
    queries land in the separate rarely-used review band, which feeds none of those figures
    either. `table_storage` (Snowflake only) adds the time-travel/fail-safe breakdown to the
    dead-asset lists; the totals and ranking come from `storage_bytes` as ever. `now` is
    injectable for tests.
    """

    usage_rows = list(usage_rows)
    queried = queried_model_ids(manifest, usage_rows)
    unqueried = dead_models(manifest, graph, queried)
    first_seen_ids = first_seen_model_ids(manifest, first_seen or {})
    now_utc = now or datetime.now(timezone.utc)
    min_age = timedelta(days=config.min_age_days)
    too_new = too_new_models(unqueried, first_seen_ids, now_utc, min_age)

    # On Snowflake, first-seen comes from ACCOUNT_USAGE.TABLES, which lags (~90 minutes): a
    # dead node with no row yet cannot prove its age, so it is set aside as a likely new table
    # rather than judged. On BigQuery a missing first-seen means zero jobs all window — the
    # strongest unused signal — so those are judged normally.
    missing: set[str] = set()
    if config.warehouse == "snowflake" and min_age > timedelta(0):
        missing = missing_first_seen_models(unqueried, first_seen_ids)
    dead = unqueried - too_new - missing

    # The rarity band gets the same too-new protection as the dead set: a model created
    # mid-window has not had a full window to accumulate queries.
    usage_by_model = model_usage(manifest, usage_rows)
    rare = rarely_used_models(usage_by_model, config.rare_threshold)
    rare -= too_new_models(rare, first_seen_ids, now_utc, min_age)
    if config.warehouse == "snowflake" and min_age > timedelta(0):
        rare -= missing_first_seen_models(rare, first_seen_ids)

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
            bytes_scanned=row.bytes_scanned,
            file_path=manifest.models[uid].original_file_path,
            resource_type=manifest.models[uid].resource_type,
        )

    def ranked_rarely_used(uids: Set[str]) -> tuple[RarelyUsedModel, ...]:
        # Most scanned bytes first — the "expensive but rarely used" candidates on top — then
        # stored size, so the ranking still works when the warehouse reports no byte figures.
        ranked = sorted(
            uids,
            key=lambda uid: (
                -usage_by_model[uid].bytes_scanned,
                -_model_bytes(manifest, storage_bytes, uid),
                uid,
            ),
        )
        return tuple(rarely_used_entry(uid) for uid in ranked)

    # Unused declared sources are a manifest verdict; the usage rows (already fetched for the
    # model verdicts) attach any direct-query evidence so the report can tell "dead weight in
    # sources.yml" apart from "raw table people query directly".
    usage_by_key = {row.relation_key: row for row in usage_rows}

    def unused_source_entry(relation: Relation) -> UnusedSource:
        row = usage_by_key.get(relation.relation_key)
        return UnusedSource(
            unique_id=relation.unique_id,
            name=relation.name,
            relation_key=relation.relation_key,
            query_count=row.query_count if row else 0,
            last_queried=row.last_queried.isoformat() if row and row.last_queried else None,
            bytes_scanned=row.bytes_scanned if row else 0,
            file_path=relation.original_file_path,
        )

    unused_source_entries = tuple(unused_source_entry(r) for r in unused_sources(manifest))

    # The stale-source check runs only when it is enabled and the metadata was readable;
    # `last_modified` is None in every other case (disabled, no sources, missing grant).
    stale_entries: tuple[StaleSource, ...] = ()
    stale_checked = False
    if last_modified is not None and config.stale_source_days > 0:
        stale_checked = True
        stale_entries = tuple(
            StaleSource(
                unique_id=relation.unique_id,
                name=relation.name,
                relation_key=relation.relation_key,
                last_modified=modified.isoformat(),
                file_path=relation.original_file_path,
            )
            for relation, modified in stale_sources(
                manifest.relations.values(),
                last_modified,
                now_utc,
                timedelta(days=config.stale_source_days),
            )
        )

    # Documentation drift is catalog-only: without catalog columns nothing can be compared.
    phantom_entries = tuple(
        PhantomColumn(
            unique_id=uid,
            model_name=manifest.models[uid].name,
            column=column,
            file_path=manifest.models[uid].original_file_path,
        )
        for uid, column in phantom_columns(manifest.models, catalog_columns or {})
    )

    # The partitioning check is BigQuery-specific: Snowflake micro-partitions automatically and
    # its explicit clustering keys are optional tuning, not debt. Ranked by the bytes user
    # queries scanned (from the usage rows already fetched), so the costliest offender is first.
    unpartitioned: tuple[UnpartitionedTable, ...] = ()
    if config.warehouse == "bigquery":
        scanned_by_key = {row.relation_key: row.bytes_scanned for row in usage_rows}
        unpartitioned = tuple(
            UnpartitionedTable(
                unique_id=uid,
                name=manifest.models[uid].name,
                relation_key=manifest.models[uid].relation_key,
                total_bytes=_model_bytes(manifest, storage_bytes, uid),
                materialized=manifest.models[uid].materialized or "table",
                bytes_scanned=scanned_by_key.get(manifest.models[uid].relation_key, 0),
                file_path=manifest.models[uid].original_file_path,
            )
            for uid in unpartitioned_large_tables(
                manifest.models, storage_bytes, scanned_bytes=scanned_by_key
            )
        )

    storage_by_key = dict(table_storage or {})

    def ranked_assets(uids: Set[str]) -> tuple[DeadModel, ...]:
        ranked = sorted(uids, key=lambda uid: (-_model_bytes(manifest, storage_bytes, uid), uid))

        def entry(uid: str) -> DeadModel:
            storage = storage_by_key.get(manifest.models[uid].relation_key)
            return DeadModel(
                unique_id=uid,
                name=manifest.models[uid].name,
                relation_key=manifest.models[uid].relation_key,
                total_bytes=_model_bytes(manifest, storage_bytes, uid),
                file_path=manifest.models[uid].original_file_path,
                resource_type=manifest.models[uid].resource_type,
                time_travel_bytes=storage.time_travel_bytes if storage else 0,
                failsafe_bytes=storage.failsafe_bytes if storage else 0,
            )

        return tuple(entry(uid) for uid in ranked)

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
        dead_exposures=tuple(
            AffectedConsumer(kind="exposure", name=e.name, unique_id=e.unique_id)
            for e in dead_exposures(manifest, dead)
        ),
        affected_semantic=tuple(
            _affected_semantic_entry(manifest, a)
            for a in affected_semantic_consumers(manifest, dead)
        ),
        dead_models=ranked_assets(dead),
        too_new_models=ranked_assets(too_new),
        missing_first_seen=ranked_assets(missing),
        rarely_used=ranked_rarely_used(rare),
        rare_threshold=config.rare_threshold,
        reclaimable_bytes=reclaimable,
        coverage=coverage(manifest, catalog_columns),
        unpartitioned_tables=unpartitioned,
        unused_sources=unused_source_entries,
        stale_sources=stale_entries,
        stale_days=config.stale_source_days,
        stale_checked=stale_checked,
        phantom_columns=phantom_entries,
        columns=column_report,
        orphans=orphan_report,
    )


def build_column_report(
    manifest: Manifest,
    catalog: Catalog,
    consumption: ColumnConsumption,
    edges: Iterable[ColumnEdge],
    storage_bytes: Mapping[str, int],
) -> ColumnReport:
    """Assemble the column-grain section: dead vs active, removable, and the full ranked dead list.

    `removable` softens "dead" with the manifest blocker check — a dead column backing a test or
    bound by an enforced contract is *not* trivially removable. Dead columns are ranked by their
    owning model's storage, the best available proxy (BigQuery has no per-column bytes). The whole
    ranked list is kept; the renderer decides how much to show. `consumption` carries the parse
    counts alongside the consumed set, so the report can state how much query text the verdicts saw.
    """

    all_columns = {
        (unique_id, column)
        for unique_id in manifest.models
        for column in catalog.model_columns(unique_id)
    }
    dead = dead_columns(all_columns, consumption.consumed, edges)
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
        parsed_queries=consumption.parsed,
        unparseable_queries=consumption.unparseable,
    )


def build_orphan_report(
    manifest: Manifest,
    existing: Iterable[WarehouseRelation] | None,
    references: Set[str],
    usage_rows: Iterable[UsageRow] = (),
) -> OrphanReport:
    """Assemble the orphan section: undeclared sources (manifest-only) plus orphaned relations.

    `existing` is None when the warehouse table metadata could not be listed (missing
    `bigquery.tables.list`); then `orphans_checked` is False and the orphan list is empty, but
    undeclared sources are still reported because they are recovered from the manifest.
    `usage_rows` (already fetched for the model verdicts) attach direct-query evidence to each
    orphan and rank the still-queried ones first — those are dangerous to drop.
    """

    dbt_keys = manifest.dbt_relation_keys()
    undeclared = undeclared_sources(references, dbt_keys)
    if existing is None:
        return OrphanReport(undeclared_sources=undeclared, orphans_checked=False)

    usage_by_key = {row.relation_key: row for row in usage_rows}

    def orphan_entry(relation: WarehouseRelation) -> OrphanedRelation:
        row = usage_by_key.get(relation.relation_key)
        return OrphanedRelation(
            relation_key=relation.relation_key,
            relation_type=relation.relation_type,
            query_count=row.query_count if row else 0,
            last_queried=row.last_queried.isoformat() if row and row.last_queried else None,
            bytes_scanned=row.bytes_scanned if row else 0,
        )

    entries = [orphan_entry(r) for r in orphaned_relations(existing, references, dbt_keys)]
    entries.sort(key=lambda o: (-o.bytes_scanned, -o.query_count, o.relation_key))
    return OrphanReport(
        orphaned_relations=tuple(entries),
        undeclared_sources=undeclared,
        orphans_checked=True,
    )
