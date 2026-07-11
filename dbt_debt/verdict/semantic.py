"""Semantic-layer impact verdict. A pure manifest traversal, no warehouse needed.

The exposure check's semantic-layer sibling, with one extra hop: metrics depend on semantic
models (and sometimes other metrics), and saved queries depend on metrics, so impact has to
flow through the consumer graph. A consumer is *affected* when a dead model feeds it directly
or through other consumers; affected consumers need review before any removal. Each result
carries `via`, the dependency that made it affected, so the report can say *why* a consumer
is at risk, not just that it is.

Like exposures, semantic consumers never feed the aliveness verdict itself: they are declared
use, not observed use (real semantic-layer queries land in the job history and count there).
"""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass

from dbt_debt.domain import Manifest, SemanticConsumer


@dataclass(frozen=True)
class AffectedSemanticConsumer:
    """An affected consumer and the dependency that condemned it.

    `via` is a unique_id: a dead model for consumers sitting directly on one, or an
    already-affected consumer for the transitive hops. A dead-model dependency always wins
    over a consumer one, so a consumer touching a dead model directly is reported as such
    even when an affected consumer also feeds it.
    """

    consumer: SemanticConsumer
    via: str


def affected_semantic_consumers(
    manifest: Manifest, dead_models: Set[str]
) -> list[AffectedSemanticConsumer]:
    """Semantic consumers fed, directly or transitively, by at least one dead model.

    Resolved by fixpoint over the consumer dependency graph so a saved query over a metric
    over a semantic model over a dead model is flagged too; cycles between consumers simply
    stop adding members and terminate. Results come out in discovery order (the consumers on
    dead models first, then each transitive hop), so the chain reads top-down in the report.
    """

    affected: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for consumer in manifest.semantic_consumers.values():
            if consumer.unique_id in affected:
                continue
            via = next((dep for dep in consumer.depends_on if dep in dead_models), None)
            if via is None:
                via = next((dep for dep in consumer.depends_on if dep in affected), None)
            if via is not None:
                affected[consumer.unique_id] = via
                changed = True
    return [
        AffectedSemanticConsumer(consumer=manifest.semantic_consumers[uid], via=via)
        for uid, via in affected.items()
    ]
