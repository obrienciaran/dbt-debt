"""Stale-source verdict — a pure comparison of declared sources against last-modified dates.

A declared source whose table has received no new data for longer than the threshold means
the loader upstream of dbt has likely stopped. The last-modified dates come from warehouse
metadata (never from query history) and are fetched before this module runs; a source with
no entry is skipped, because absent metadata is not evidence of staleness. Like the other
review bands, this never feeds any unused figure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from dbt_debt.domain import Relation


def stale_sources(
    relations: Iterable[Relation],
    last_modified: Mapping[str, datetime],
    now: datetime,
    max_age: timedelta,
) -> list[tuple[Relation, datetime]]:
    """Sources whose table last changed more than `max_age` ago, stalest first."""

    stale = [
        (relation, modified)
        for relation in relations
        if (modified := last_modified.get(relation.relation_key)) is not None
        and now - modified > max_age
    ]
    return sorted(stale, key=lambda pair: (pair[1], pair[0].relation_key))
