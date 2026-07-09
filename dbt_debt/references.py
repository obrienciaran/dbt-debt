"""Which warehouse relations each model reads, recovered from compiled SQL.

A model's compiled SQL names its upstreams as fully-qualified relations (dbt renders `ref()` and
`source()` to `project.dataset.table`). Collecting those references lets us spot relations a model
depends on that dbt has no node for — an *undeclared source* — without any warehouse access. Pure:
manifest in, relation keys out.
"""

from __future__ import annotations

from dbt_debt.domain import Manifest
from dbt_debt.sqlparse import SqlParseError, referenced_relations


def model_relation_references(manifest: Manifest, dialect: str = "bigquery") -> set[str]:
    """Every `project.dataset.table` relation_key any model reads in its compiled SQL.

    Models with no compiled SQL, or SQL sqlglot cannot parse, are skipped so one odd model never
    sinks the whole pass.
    """

    references: set[str] = set()
    for model in manifest.models.values():
        sql = model.compiled_code
        if not sql:
            continue
        try:
            references |= referenced_relations(sql, dialect)
        except SqlParseError:
            continue
    return references
