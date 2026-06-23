"""Recover external column consumption by parsing user-query text.

BigQuery exposes no column-level access logs, so the only way to learn which columns a human or
BI tool actually read is to parse the query SQL. Each query's resolved (relation, column) reads
are mapped to the owning model; columns that resolve to non-model relations (raw sources) are
dropped. Unparseable queries are skipped so one bad query never sinks the scan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from dbt_debt.domain import ColumnRef
from dbt_debt.sqlparse import Schema, SqlParseError, columns_read


def consumed_model_columns(
    query_texts: Iterable[str],
    schema: Schema,
    relation_to_id: Mapping[str, str],
    dialect: str = "bigquery",
) -> set[ColumnRef]:
    """Model columns (unique_id, column) read by at least one external query."""

    consumed: set[ColumnRef] = set()
    for sql in query_texts:
        try:
            reads = columns_read(sql, schema, dialect)
        except SqlParseError:
            continue
        for relation_key, column in reads:
            unique_id = relation_to_id.get(relation_key)
            if unique_id is not None:
                consumed.add((unique_id, column))
    return consumed
