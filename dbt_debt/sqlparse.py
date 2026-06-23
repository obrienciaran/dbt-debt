"""sqlglot helpers: resolve SQL column references against a warehouse schema.

The one fragile primitive both higher layers need is "resolve a column to the real relation and
column it reads". Two shapes are exposed:

- `columns_read` — every (relation, column) a query *touches* (SELECT, JOIN, WHERE, ...). Drives
  external column consumption. `SELECT *` is expanded against the schema, which is the
  conservative policy: a `*` counts all of a table's columns as used.
- `column_lineage_edges` — for each of a model's output columns, the upstream base columns that
  feed it, traced through CTEs and subqueries. Drives column-lineage propagation.

Resolved lineage sidesteps the naive-matching hazards (`SELECT *`, name collisions, multi-hop)
because dependencies are *resolved*, not string-matched. Anything sqlglot cannot parse raises
`SqlParseError`; callers decide whether to skip it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

# A sqlglot nested schema: {catalog: {database: {table: {column: type}}}}. Typed loosely because
# sqlglot's `qualify` takes `dict[str, object]` and dict is invariant in its value type.
Schema = dict[str, Any]
RelationColumns = Mapping[str, Iterable[str]]


class SqlParseError(RuntimeError):
    """A query could not be parsed or resolved against the schema."""


def build_schema(relation_columns: RelationColumns) -> Schema:
    """Nest canonical `project.dataset.table` relation keys into a sqlglot schema.

    Keys that are not three-part (the BigQuery shape) are skipped — they cannot be placed
    unambiguously, and a partial schema still resolves the relations it does know.
    """

    schema: Schema = {}
    for key, columns in relation_columns.items():
        parts = key.split(".")
        if len(parts) != 3:
            continue
        catalog, database, table = parts
        relation = {column.lower(): "UNKNOWN" for column in columns}
        schema.setdefault(catalog, {}).setdefault(database, {})[table] = relation
    return schema


def columns_read(sql: str, schema: Schema, dialect: str = "bigquery") -> set[tuple[str, str]]:
    """Every (relation_key, column) the query reads, with `*` expanded against the schema."""

    qualified = _qualify(sql, schema, dialect)
    reads: set[tuple[str, str]] = set()
    for scope in traverse_scope(qualified):
        for column in scope.columns:
            source = scope.sources.get(column.table)
            if isinstance(source, exp.Table):
                reads.add((_relation_key(source), column.name.lower()))
    return reads


def column_lineage_edges(
    sql: str, output_columns: Iterable[str], schema: Schema, dialect: str = "bigquery"
) -> list[tuple[str, str, str]]:
    """For each output column, the upstream (relation_key, column, output_column) it derives from.

    Output columns that cannot be traced are skipped individually so one odd column does not
    discard a whole model's lineage.
    """

    edges: list[tuple[str, str, str]] = []
    for output in output_columns:
        try:
            node = lineage(output, sql, schema=schema, dialect=dialect)
        except (SqlglotError, KeyError, ValueError):
            continue
        for leaf in node.walk():
            if isinstance(leaf.source, exp.Table):
                edges.append((_relation_key(leaf.source), _leaf_column(leaf.name), output.lower()))
    return edges


def referenced_relations(sql: str, dialect: str = "bigquery") -> set[str]:
    """Every fully-qualified `project.dataset.table` relation a query reads, at table grain.

    A lighter parse than `columns_read`: it needs no schema, only the table references, so it can
    run over model compiled SQL to recover the upstream relations a model depends on. Only
    three-part keys are kept — CTE references and bare table names are not warehouse relations —
    and the `INFORMATION_SCHEMA` views are skipped. Unparseable SQL raises `SqlParseError`.
    """

    try:
        expression = sqlglot.parse_one(sql, dialect=dialect)
    except SqlglotError as exc:
        raise SqlParseError(str(exc)) from exc
    keys: set[str] = set()
    for table in expression.find_all(exp.Table):
        key = _relation_key(table)
        if key.count(".") == 2 and "information_schema" not in key:
            keys.add(key)
    return keys


def _qualify(sql: str, schema: Schema, dialect: str) -> exp.Expression:
    # sqlglot types parse_one/qualify as the base `Expr`, but the runtime object is the
    # `Expression` that `traverse_scope` consumes.
    try:
        qualified = qualify(sqlglot.parse_one(sql, dialect=dialect), schema=schema, dialect=dialect)
    except SqlglotError as exc:
        raise SqlParseError(str(exc)) from exc
    return cast(exp.Expression, qualified)


def _relation_key(table: exp.Table) -> str:
    parts = [table.catalog, table.db, table.name]
    return ".".join(part for part in parts if part).lower()


def _leaf_column(name: str) -> str:
    """A lineage leaf is named `table.column` (or just `column`); keep the column."""

    return name.split(".")[-1].lower()
