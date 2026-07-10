"""Core domain objects for the dbt-debt scorecard.

These are plain value objects loaded from dbt artifacts. They carry no I/O and no warehouse
knowledge, so the verdict layer can be exercised with in-memory fixtures and stays pure.

Terminology is load-bearing: a *model* is the `.sql` definition; a *relation* is the
materialized table/view in the warehouse. `Model.relation_key` is the bridge between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

ColumnRef = tuple[str, str]
"""A column identified by (model unique_id, column name)."""


def relation_key(database: str | None, schema: str | None, identifier: str | None) -> str:
    """Canonical `project.dataset.table` join key for a warehouse relation.

    Both sides of the usage join produce this from their own components — a model from its
    database/schema/alias, a BigQuery `referenced_tables` entry from its project/dataset/table
    — so the key matches without parsing dbt's quoted `relation_name`. Backticks and quotes are
    stripped and the whole key is lowercased so the two sides compare equal regardless of how
    each source quoted or cased the identifier.
    """

    parts = (database, schema, identifier)
    return ".".join(part.strip(' `"').lower() for part in parts if part)


@dataclass
class Model:
    """A buildable dbt node — a model, seed, or snapshot — and its declared metadata.

    All three kinds share the same usage question ("did anything query what this builds?"),
    so they share one type, told apart by `resource_type`; a seed simply has no SQL and no
    dependencies. The *model* terminology stays load-bearing at the report surface, where
    non-model entries are tagged with their kind.

    The `relation_key` property is the join key against BigQuery's `referenced_tables` in the
    consumption layer; it is derived from `database`/`schema`/`alias`, so we never parse dbt's
    quoted `relation_name`.

    `columns` holds the names documented in YAML, lowercased at parse time — the same
    normalization `relation_key` applies to relations, so column names compare equal across the
    manifest, the catalog, and parsed query text. The full physical column list comes from
    catalog.json.
    """

    unique_id: str
    name: str
    database: str | None = None
    schema: str | None = None
    alias: str | None = None
    original_file_path: str | None = None
    depends_on: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    documented_columns: tuple[str, ...] = ()
    """The subset of `columns` carrying a non-empty description, for docs coverage."""
    contract_enforced: bool = False
    compiled_code: str | None = None
    resource_type: str = "model"
    has_description: bool = False
    materialized: str | None = None
    partitioned: bool = False
    """True when the node declares `partition_by` (BigQuery) in its config."""
    clustered: bool = False
    """True when the node declares `cluster_by` in its config."""

    @property
    def relation_key(self) -> str:
        """Join key against warehouse query logs; uses `alias` when set, else the model name."""

        return relation_key(self.database, self.schema, self.alias or self.name)


@dataclass(frozen=True)
class UsageRow:
    """One relation observed in the BigQuery query logs, restricted to user queries.

    Produced by the consumption layer (dbt's own queries already excluded). `relation_key` is
    the canonical join key (see `relation_key`); `query_count` and `last_queried` quantify and
    date the consumption that keeps a model alive. `bytes_scanned` sums what those queries
    read (BigQuery `total_bytes_processed`, Snowflake `bytes_scanned`) — a cost signal for the
    review lists only, never an input to any usage verdict. A query touching several tables
    attributes its whole figure to each, so sums across tables overlap.
    """

    relation_key: str
    query_count: int
    last_queried: datetime | None = None
    bytes_scanned: int = 0


@dataclass(frozen=True)
class WarehouseRelation:
    """A relation physically present in the warehouse, as reported by INFORMATION_SCHEMA.TABLES.

    The inventory side of the orphan check: a table or view that exists in a dbt-managed dataset.
    `relation_key` is the canonical join key against the dbt relation set; `relation_type` is the
    warehouse's own label (`BASE TABLE`, `VIEW`, `MATERIALIZED VIEW`, ...).
    """

    relation_key: str
    relation_type: str


@dataclass(frozen=True)
class ColumnEdge:
    """A column-lineage edge: `upstream` feeds `downstream` (data flows upstream → downstream).

    Both ends are `(model unique_id, column)` refs. Used to keep an unqueried column alive when
    a downstream column built from it is consumed — the column-grain analogue of the model DAG.
    """

    upstream: ColumnRef
    downstream: ColumnRef


@dataclass(frozen=True)
class Test:
    """A dbt test node and what it guards.

    `attached_node` / `column_name` let us mark a test removable when the model or column it
    guards is dead — a pure manifest traversal, no warehouse needed. `column_name` is lowercased
    at parse time so it compares equal to the normalized dead-column refs.
    """

    # Opt out of pytest collection: the class name starts with "Test" but it is a domain
    # object, not a test case. Unannotated, so the dataclass does not treat it as a field.
    __test__ = False

    unique_id: str
    name: str
    depends_on: tuple[str, ...] = ()
    attached_node: str | None = None
    column_name: str | None = None


@dataclass(frozen=True)
class Exposure:
    """A declared external consumer of one or more models.

    Exposures depend on models, not columns, and only capture *declared* uses — so they
    undercount real consumption, which the query-log layer fills in. The two are complementary.
    """

    unique_id: str
    name: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticConsumer:
    """A semantic-layer node — a semantic model, metric, or saved query — that consumes models.

    Like exposures these capture *declared* use: a dead model feeding one is flagged for review
    rather than removable, and a column a semantic model names is blocked rather than consumed.
    `depends_on` holds the raw manifest deps (model, semantic-model, or metric unique_ids);
    `column_refs` is resolved to (model unique_id, column) pairs for semantic models only —
    metrics and saved queries reference columns indirectly through their semantic models.
    """

    unique_id: str
    name: str
    kind: str
    """One of "semantic_model", "metric", or "saved_query"."""
    depends_on: tuple[str, ...] = ()
    column_refs: tuple[ColumnRef, ...] = ()


@dataclass(frozen=True)
class Relation:
    """A dbt source: a warehouse relation dbt reads but does not build.

    Carries the `relation_key` the orphan check subtracts from the warehouse inventory, the
    owning `schema`, and the display `name` / `original_file_path` the unused-source report
    shows. Sources are only read, so their datasets are external inputs and never searched for
    orphans. (Seeds and snapshots, which dbt *builds*, live in `Manifest.models` with their
    `resource_type` tag.)
    """

    unique_id: str
    relation_key: str
    schema: str | None
    name: str = ""
    """Display name, `source_name.table` as written in sources.yml."""
    original_file_path: str | None = None
    database: str | None = None
    """The database (GCP project / Snowflake database) the source table lives in."""


@dataclass
class Manifest:
    """A parsed, trimmed view of dbt's manifest.json."""

    project_name: str
    dbt_schema_version: str
    dbt_version: str | None
    adapter_type: str | None = None
    """The dbt adapter that produced the artifacts (e.g. "bigquery", "snowflake")."""
    models: dict[str, Model] = field(default_factory=dict)
    tests: dict[str, Test] = field(default_factory=dict)
    exposures: dict[str, Exposure] = field(default_factory=dict)
    relations: dict[str, Relation] = field(default_factory=dict)
    """Sources only; every buildable node (model, seed, snapshot) lives in `models`."""
    semantic_consumers: dict[str, SemanticConsumer] = field(default_factory=dict)

    def relation_to_id(self) -> dict[str, str]:
        """Reverse map from each model's warehouse `relation_key` to its `unique_id`.

        The join the consumption and lineage layers use to turn a queried relation back into a
        dbt model; built here once so callers don't each reconstruct it.
        """

        return {model.relation_key: unique_id for unique_id, model in self.models.items()}

    def dbt_relation_keys(self) -> set[str]:
        """Every warehouse relation dbt defines: models, seeds, snapshots, and sources.

        The subtraction set for orphan discovery — a physical table whose key is in here is
        accounted for by dbt and is never an orphan or an undeclared source.
        """

        keys = {model.relation_key for model in self.models.values()}
        keys |= {relation.relation_key for relation in self.relations.values()}
        return keys

    def managed_datasets(self) -> set[str]:
        """Dataset (schema) names dbt materializes into — where orphans are looked for.

        Drawn from the buildable nodes (models, seeds, snapshots); source datasets are excluded
        because dbt only reads them, so unmanaged tables there are not orphans. Quotes are
        stripped but case is preserved: these name a dataset-qualified `INFORMATION_SCHEMA.TABLES`
        view, and BigQuery dataset ids are case-sensitive.
        """

        schemas = {model.schema for model in self.models.values()}
        return {schema.strip(' `"') for schema in schemas if schema}

    def source_datasets(self) -> set[str]:
        """`database.schema` keys for the declared sources — where staleness metadata is read.

        Quotes are stripped but case is preserved, matching `managed_datasets`: on BigQuery
        these name a dataset-qualified metadata table and dataset ids are case-sensitive.
        Sources missing a database or schema are skipped (their staleness cannot be checked).
        """

        quotes = ' `"'
        pairs = {
            (relation.database.strip(quotes), relation.schema.strip(quotes))
            for relation in self.relations.values()
            if relation.database and relation.schema
        }
        return {f"{database}.{schema}" for database, schema in pairs}
