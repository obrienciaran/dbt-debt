"""Bridge warehouse usage to manifest model ids.

The consumption layer speaks in relation keys; the verdict layer speaks in model unique_ids.
This pure function is the join between them, so the verdict layer never has to know about
warehouse identifiers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from dbt_debt.domain import Manifest, UsageRow


def queried_model_ids(manifest: Manifest, usage_rows: Iterable[UsageRow]) -> set[str]:
    """Unique_ids of models whose relation was directly referenced by a user query."""

    relation_to_id = manifest.relation_to_id()
    return {
        relation_to_id[row.relation_key]
        for row in usage_rows
        if row.query_count > 0 and row.relation_key in relation_to_id
    }


def first_seen_model_ids(
    manifest: Manifest, first_seen: Mapping[str, datetime]
) -> dict[str, datetime]:
    """Rekey the relation-level first-seen map by model unique_id, for the too-new guard."""

    relation_to_id = manifest.relation_to_id()
    return {relation_to_id[key]: seen for key, seen in first_seen.items() if key in relation_to_id}
