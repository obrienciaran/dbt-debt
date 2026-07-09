"""Exclude dbt's own queries from the usage count.

User consumption is the `SELECT`s a human or BI tool ran — not dbt's builds and tests. dbt tags
every statement with a JSON query-comment, so a regex over `JOBS.query` removes them. This is
the second line of defence behind the `statement_type = 'SELECT'` filter, which alone would
keep dbt's data tests (they are `SELECT`s).
"""

from __future__ import annotations


def validate_query_comment_pattern(pattern: str) -> None:
    """Reject a pattern that cannot sit inside either warehouse's raw SQL string literal.

    A pattern containing `'''` (or ending in `'`) would terminate BigQuery's raw triple-quoted
    string early, and one containing `$$` would terminate Snowflake's dollar-quoted string, each
    producing a confusing warehouse syntax error — so we refuse both up front with a message
    that names the flag.
    """

    if "'''" in pattern or pattern.endswith("'") or "$$" in pattern:
        raise ValueError(
            "--query-comment-pattern must not contain ''' or $$ or end with a single quote; "
            "it is embedded in a raw SQL string (BigQuery r'''...''', Snowflake $$...$$)."
        )


def exclusion_clause(query_comment_pattern: str) -> str:
    """A SQL boolean that is true for rows whose query text is *not* a dbt query.

    Returned as a fragment so the caller composes it into the JOBS `WHERE`. The pattern is
    wrapped in a BigQuery raw triple-quoted string so its embedded quotes need no escaping.
    """

    validate_query_comment_pattern(query_comment_pattern)
    return f"NOT REGEXP_CONTAINS(query, r'''{query_comment_pattern}''')"
