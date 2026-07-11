"""The "rarely used" band of models queried within the window, but barely.

A third bucket between active and unused: these models have observed use, so none of the
unused-derived figures (removable tests, consumer impact, reclaimable bytes) ever include
them. The band is a review list, sized and dated so an owner can judge whether the few
remaining queries still earn the model's keep. Pure working-out over the usage map.
"""

from __future__ import annotations

from collections.abc import Mapping

from dbt_debt.domain import UsageRow


def rarely_used_models(usage: Mapping[str, UsageRow], threshold: int) -> set[str]:
    """Queried models with at most `threshold` queries in the window.

    A zero (or negative) `threshold` disables the band. Models with no usage at all are the
    dead set's business, not this one's, and the caller also subtracts the too-new set, since a
    model created mid-window has not had a full window to accumulate queries.
    """

    if threshold <= 0:
        return set()
    return {uid for uid, row in usage.items() if 0 < row.query_count <= threshold}
