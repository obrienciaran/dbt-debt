"""Exclude dbt's own queries from the usage count.

User consumption is the `SELECT`s a human or BI tool ran — not dbt's builds and tests. dbt tags
every statement with a JSON query-comment, so a regex over `JOBS.query` removes them. This is
the second line of defence behind the `statement_type = 'SELECT'` filter, which alone would
keep dbt's data tests (they are `SELECT`s).
"""

from __future__ import annotations


def validate_query_comment_pattern(pattern: str) -> None:
    """Reject a pattern that cannot sit inside the raw triple-quoted SQL string.

    A pattern containing `'''` (or ending in `'`) would terminate the string early and produce a
    confusing BigQuery syntax error, so we refuse it up front with a message that names the flag.
    """

    if "'''" in pattern or pattern.endswith("'"):
        raise ValueError(
            "--query-comment-pattern must not contain ''' or end with a single quote; "
            "it is embedded in a triple-quoted BigQuery string."
        )


def exclusion_clause(query_comment_pattern: str) -> str:
    """A SQL boolean that is true for rows whose query text is *not* a dbt query.

    Returned as a fragment so the caller composes it into the JOBS `WHERE`. The pattern is
    wrapped in a BigQuery raw triple-quoted string so its embedded quotes need no escaping.
    """

    validate_query_comment_pattern(query_comment_pattern)
    return f"NOT REGEXP_CONTAINS(query, r'''{query_comment_pattern}''')"
