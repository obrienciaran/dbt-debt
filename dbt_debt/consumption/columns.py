"""Recover external column consumption by parsing user-query text.

BigQuery exposes no column-level access logs, so the only way to learn which columns a human or
BI tool actually read is to parse the query SQL. Each query's resolved (relation, column) reads
are mapped to the owning model; columns that resolve to non-model relations (raw sources) are
dropped. Unparseable queries are skipped so one bad query never sinks the scan, but they are
counted, so the report can say how much of the query text the column verdicts actually saw.
Usage verdicts never depend on this parsing; only column-level findings do.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from dbt_debt.domain import ColumnRef
from dbt_debt.sqlparse import Schema, SqlParseError, columns_read


@dataclass(frozen=True)
class ColumnConsumption:
    """The consumed column set plus how much of the query text it is based on.

    `parsed` + `unparseable` is the number of query texts inspected; a high unparseable share
    means the column verdicts saw less evidence than the usage verdicts did. `unparseable`
    covers every query no reads could be extracted from, both broken SQL and SQL that would not
    qualify against the catalog schema alike.
    """

    consumed: set[ColumnRef] = field(default_factory=set)
    parsed: int = 0
    unparseable: int = 0


def consumed_model_columns(
    query_texts: Iterable[str],
    schema: Schema,
    relation_to_id: Mapping[str, str],
    dialect: str = "bigquery",
) -> ColumnConsumption:
    """Model columns (unique_id, column) read by at least one external query, with parse counts."""

    consumed: set[ColumnRef] = set()
    parsed = 0
    unparseable = 0
    for sql in query_texts:
        try:
            reads = columns_read(sql, schema, dialect)
        except SqlParseError:
            unparseable += 1
            continue
        parsed += 1
        for relation_key, column in reads:
            unique_id = relation_to_id.get(relation_key)
            if unique_id is not None:
                consumed.add((unique_id, column))
    return ColumnConsumption(consumed=consumed, parsed=parsed, unparseable=unparseable)
