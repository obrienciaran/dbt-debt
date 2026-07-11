"""Large BigQuery tables built without partitioning or clustering. Pure manifest working-out.

On BigQuery both must be declared explicitly in the dbt config (`partition_by` / `cluster_by`),
so a big table with neither is usually an oversight that makes every scan of it a full scan.
Only `table` and `incremental` materializations qualify (views cannot be partitioned), sizes
come from the catalog-derived bytes map, and a floor keeps small projects from being flagged
wholesale. Snowflake is skipped entirely by the caller: its micro-partitioning is automatic and
explicit clustering keys are an optional large-table tuning lever, not debt.

Flagging is by *stored* bytes (what the catalog records), but ranking puts the tables user
queries actually scanned the most first (from the usage rows' bytes), falling back to stored
size: an unpartitioned table only costs money when queried, so the top of the list is the
best partitioning candidate, not just the biggest table.
"""

from __future__ import annotations

from collections.abc import Mapping

from dbt_debt.domain import Model

PARTITION_FLOOR_BYTES = 1024**3
"""Tables below 1 GiB are never flagged; at that size a full scan is cheap anyway."""

MAX_FLAGGED = 20
"""The report names at most this many offenders, largest first."""

_PARTITIONABLE = ("table", "incremental")


def unpartitioned_large_tables(
    models: Mapping[str, Model],
    storage_bytes: Mapping[str, int],
    *,
    scanned_bytes: Mapping[str, int] | None = None,
    floor_bytes: int = PARTITION_FLOOR_BYTES,
    max_flagged: int = MAX_FLAGGED,
) -> tuple[str, ...]:
    """Unique_ids of the largest partitionable models with neither `partition_by` nor `cluster_by`.

    The floor is on stored size; the ranking is by `scanned_bytes` (what user queries read over
    the window, keyed by relation_key) first, stored size second, ties by unique_id, capped at
    `max_flagged`. Models without a known size are below any positive floor and so never
    flagged, so no catalog means no verdict.
    """

    scanned = scanned_bytes or {}
    flagged = [
        (
            scanned.get(model.relation_key, 0),
            storage_bytes.get(model.relation_key, 0),
            unique_id,
        )
        for unique_id, model in models.items()
        if model.resource_type == "model"
        and model.materialized in _PARTITIONABLE
        and not model.partitioned
        and not model.clustered
    ]
    ranked = sorted(
        (entry for entry in flagged if entry[1] >= floor_bytes),
        key=lambda item: (-item[0], -item[1], item[2]),
    )
    return tuple(uid for _, _, uid in ranked[:max_flagged])
