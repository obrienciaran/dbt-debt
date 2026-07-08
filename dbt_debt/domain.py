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
    """A dbt model: its `.sql` definition and declared metadata.

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
    contract_enforced: bool = False
    compiled_code: str | None = None

    @property
    def relation_key(self) -> str:
        """Join key against warehouse query logs; uses `alias` when set, else the model name."""

        return relation_key(self.database, self.schema, self.alias or self.name)


@dataclass(frozen=True)
class UsageRow:
    """One relation observed in the BigQuery query logs, restricted to user queries.

    Produced by the consumption layer (dbt's own queries already excluded). `relation_key` is
    the canonical join key (see `relation_key`); `query_count` and `last_queried` quantify and
    date the consumption that keeps a model alive.
    """

    relation_key: str
    query_count: int
    last_queried: datetime | None = None


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
class Relation:
    """A dbt-defined warehouse relation that is not a model: a seed, snapshot, or source.

    Carries only what the orphan check needs — the `relation_key` to subtract from the warehouse
    inventory, the owning `schema`, and whether dbt *materializes* it. Seeds and snapshots are
    materialized (dbt writes them), so their dataset is dbt-managed; a source is only read, so its
    dataset is an external input and not searched for orphans.
    """

    unique_id: str
    relation_key: str
    schema: str | None
    materialized: bool


@dataclass
class Manifest:
    """A parsed, trimmed view of dbt's manifest.json."""

    project_name: str
    dbt_schema_version: str
    dbt_version: str | None
    models: dict[str, Model] = field(default_factory=dict)
    tests: dict[str, Test] = field(default_factory=dict)
    exposures: dict[str, Exposure] = field(default_factory=dict)
    relations: dict[str, Relation] = field(default_factory=dict)

    def relation_to_id(self) -> dict[str, str]:
        """Reverse map from each model's warehouse `relation_key` to its `unique_id`.

        The join the consumption and lineage layers use to turn a queried relation back into a
        dbt model; built here once so callers don't each reconstruct it.
        """

        return {model.relation_key: unique_id for unique_id, model in self.models.items()}

    def dbt_relation_keys(self) -> set[str]:
        """Every warehouse relation dbt defines: models plus seeds, snapshots, and sources.

        The subtraction set for orphan discovery — a physical table whose key is in here is
        accounted for by dbt and is never an orphan or an undeclared source.
        """

        keys = {model.relation_key for model in self.models.values()}
        keys |= {relation.relation_key for relation in self.relations.values()}
        return keys

    def managed_datasets(self) -> set[str]:
        """Dataset (schema) names dbt materializes into — where orphans are looked for.

        Drawn from models plus materialized relations (seeds, snapshots); source datasets are
        excluded because dbt only reads them, so unmanaged tables there are not orphans. Quotes are
        stripped but case is preserved: these name a dataset-qualified `INFORMATION_SCHEMA.TABLES`
        view, and BigQuery dataset ids are case-sensitive.
        """

        schemas = {model.schema for model in self.models.values()}
        schemas |= {r.schema for r in self.relations.values() if r.materialized}
        return {schema.strip(' `"') for schema in schemas if schema}
