"""Large BigQuery tables built without partitioning or clustering — pure manifest working-out.

On BigQuery both must be declared explicitly in the dbt config (`partition_by` / `cluster_by`),
so a big table with neither is usually an oversight that makes every scan of it a full scan.
Only `table` and `incremental` materializations qualify (views cannot be partitioned), sizes
come from the catalog-derived bytes map, and a floor keeps small projects from being flagged
wholesale. Snowflake is skipped entirely by the caller: its micro-partitioning is automatic and
explicit clustering keys are an optional large-table tuning lever, not debt.

Ranked by *stored* bytes — what the catalog records — not bytes processed; scan cost is not
collected (see the backlog).
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
    floor_bytes: int = PARTITION_FLOOR_BYTES,
    max_flagged: int = MAX_FLAGGED,
) -> tuple[str, ...]:
    """Unique_ids of the largest partitionable models with neither `partition_by` nor `cluster_by`.

    Largest first, ties by unique_id, capped at `max_flagged`. Models without a known size are
    below any positive floor and so never flagged — no catalog, no verdict.
    """

    flagged = [
        (storage_bytes.get(model.relation_key, 0), unique_id)
        for unique_id, model in models.items()
        if model.resource_type == "model"
        and model.materialized in _PARTITIONABLE
        and not model.partitioned
        and not model.clustered
    ]
    ranked = sorted(
        ((size, uid) for size, uid in flagged if size >= floor_bytes),
        key=lambda item: (-item[0], item[1]),
    )
    return tuple(uid for _, uid in ranked[:max_flagged])
