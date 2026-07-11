"""Large Redshift tables whose maintenance has fallen behind — pure working-out.

Flags tables with a big unsorted region (scans stop pruning until VACUUM runs), stale planner
statistics (ANALYZE resets `stats_off`), or heavy slice skew (one slice becomes the bottleneck),
from the `SVV_TABLE_INFO` hygiene rows. On Serverless and modern provisioned clusters automatic
vacuum and analyze usually keep every figure near zero, so an empty result is the healthy state;
NULL columns parse as 0 and never trip a threshold. BigQuery and Snowflake are skipped entirely
by the caller: both manage storage layout automatically and expose no equivalent columns.

Flagging is by the *stored* size carried on the hygiene rows, but ranking puts the tables user
queries actually scanned the most first (from the usage rows' bytes), falling back to stored
size — a neglected table only costs query time when queried, so the top of the list is the best
maintenance candidate, not just the messiest table.
"""

from __future__ import annotations

from collections.abc import Mapping

from dbt_debt.domain import Model, TableHygiene

HYGIENE_FLOOR_BYTES = 1024**3
"""Tables below 1 GiB are never flagged; at that size a fresh VACUUM or ANALYZE is cheap anyway."""

UNSORTED_THRESHOLD = 20.0
"""Percent of rows outside the sort order; AWS's guidance treats ~20% as the needs-VACUUM line."""

STATS_OFF_THRESHOLD = 10.0
"""Statistics staleness percent; AWS's ANALYZE guidance treats above 10 as stale statistics."""

SKEW_THRESHOLD = 4.0
"""Largest-to-smallest slice row ratio; AWS's tuning guidance treats around 4:1 as problem skew."""

MAX_FLAGGED = 20
"""The report names at most this many offenders, most scanned first."""


def unhealthy_tables(
    models: Mapping[str, Model],
    hygiene: Mapping[str, TableHygiene],
    *,
    scanned_bytes: Mapping[str, int] | None = None,
    floor_bytes: int = HYGIENE_FLOOR_BYTES,
    max_flagged: int = MAX_FLAGGED,
    unsorted_threshold: float = UNSORTED_THRESHOLD,
    stats_off_threshold: float = STATS_OFF_THRESHOLD,
    skew_threshold: float = SKEW_THRESHOLD,
) -> tuple[str, ...]:
    """Unique_ids of the largest nodes whose hygiene row trips any maintenance threshold.

    Every buildable node with a hygiene row qualifies — models, seeds, and snapshots are all
    physical tables here, and views have no SVV_TABLE_INFO row, so keying on the hygiene map
    filters them naturally. The floor is on the row's own stored size; the ranking is by
    `scanned_bytes` (what user queries read over the window, keyed by relation_key) first,
    stored size second, ties by unique_id, capped at `max_flagged`.
    """

    scanned = scanned_bytes or {}
    flagged = []
    for unique_id, model in models.items():
        row = hygiene.get(model.relation_key)
        if row is None or row.active_bytes < floor_bytes:
            continue
        if (
            row.unsorted_percent >= unsorted_threshold
            or row.stats_off_percent >= stats_off_threshold
            or row.skew_rows >= skew_threshold
        ):
            flagged.append((scanned.get(model.relation_key, 0), row.active_bytes, unique_id))
    ranked = sorted(flagged, key=lambda item: (-item[0], -item[1], item[2]))
    return tuple(uid for _, _, uid in ranked[:max_flagged])
