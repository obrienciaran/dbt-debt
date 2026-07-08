"""Semantic-layer impact verdict — a pure manifest traversal, no warehouse needed.

The exposure check's semantic-layer sibling, with one extra hop: metrics depend on semantic
models (and sometimes other metrics), and saved queries depend on metrics, so impact has to
flow through the consumer graph. A consumer is *affected* when a dead model feeds it directly
or through other consumers; affected consumers need review before any removal.

Like exposures, semantic consumers never feed the aliveness verdict itself: they are declared
use, not observed use (real semantic-layer queries land in the job history and count there).
"""

from __future__ import annotations

from collections.abc import Set

from dbt_debt.domain import Manifest, SemanticConsumer


def affected_semantic_consumers(
    manifest: Manifest, dead_models: Set[str]
) -> list[SemanticConsumer]:
    """Semantic consumers fed — directly or transitively — by at least one dead model.

    Resolved by fixpoint over the consumer dependency graph so a saved query over a metric
    over a semantic model over a dead model is flagged too; cycles between consumers simply
    stop adding members and terminate.
    """

    affected: set[str] = set()
    changed = True
    while changed:
        changed = False
        for consumer in manifest.semantic_consumers.values():
            if consumer.unique_id in affected:
                continue
            if any(dep in dead_models or dep in affected for dep in consumer.depends_on):
                affected.add(consumer.unique_id)
                changed = True
    return [c for uid, c in sorted(manifest.semantic_consumers.items()) if uid in affected]
