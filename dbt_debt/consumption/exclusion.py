"""Exclude dbt's own queries from the usage count.

User consumption is the `SELECT`s a human or BI tool ran — not dbt's builds and tests. dbt tags
every statement with a JSON query-comment, so a regex over `JOBS.query` removes them. This is
the second line of defence behind the `statement_type = 'SELECT'` filter, which alone would
keep dbt's data tests (they are `SELECT`s).
"""

from __future__ import annotations


def exclusion_clause(query_comment_pattern: str, column: str = "query") -> str:
    """A SQL boolean that is true for rows whose query text is *not* a dbt query.

    Returned as a fragment so the caller composes it into the JOBS `WHERE`. The pattern is
    wrapped in a BigQuery raw triple-quoted string so its embedded quotes need no escaping.
    """

    return f"NOT REGEXP_CONTAINS({column}, r'''{query_comment_pattern}''')"
