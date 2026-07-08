"""Baseline column lineage: reconstruct edges from each model's compiled SQL with sqlglot.

Works on every dbt version with no platform login — the compiled SQL is already in the manifest
(`compiled_code`) and the column universe in the catalog. For each model we trace every output
column back to the base columns that feed it, then keep only edges between known models (a
column fed by a source needs no propagation, since sources are not reported as dead).
"""

from __future__ import annotations

from dbt_debt.artifacts.catalog import Catalog
from dbt_debt.domain import ColumnEdge, Manifest
from dbt_debt.sqlparse import Schema, build_schema, column_lineage_edges


class SqlglotLineage:
    """`LineageSource` that reconstructs column edges from compiled model SQL.

    `schema` lets a caller that already built the sqlglot schema from the catalog (the column
    stage does) share it instead of building it twice; left None, it is built here.
    """

    def __init__(
        self,
        manifest: Manifest,
        catalog: Catalog,
        dialect: str = "bigquery",
        schema: Schema | None = None,
    ) -> None:
        self._manifest = manifest
        self._catalog = catalog
        self._dialect = dialect
        self._schema = schema

    def edges(self) -> list[ColumnEdge]:
        schema = self._schema or build_schema(self._catalog.relation_columns())
        relation_to_id = self._manifest.relation_to_id()

        edges: list[ColumnEdge] = []
        for unique_id, model in self._manifest.models.items():
            sql = model.compiled_code
            if not sql:
                continue
            output_columns = self._catalog.model_columns(unique_id) or tuple(model.columns)
            for upstream_rel, upstream_col, output_col in column_lineage_edges(
                sql, output_columns, schema, self._dialect
            ):
                upstream_id = relation_to_id.get(upstream_rel)
                if upstream_id is None or upstream_id == unique_id:
                    continue
                edges.append(
                    ColumnEdge(
                        upstream=(upstream_id, upstream_col),
                        downstream=(unique_id, output_col),
                    )
                )
        return edges
