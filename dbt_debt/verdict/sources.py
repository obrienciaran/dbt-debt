"""Unused declared sources, found by a pure manifest traversal with no warehouse needed.

A source is unused when nothing in the project depends on it: no model, seed, snapshot,
exposure, or semantic-layer consumer names it. Tests attached to the source do not count as
use (a test guards data, it does not consume it), so a source kept alive only by its own
tests is still reported.

This is the mirror image of the undeclared-source finding in `orphans.py`: there a model
reads a relation dbt has no record of; here dbt has a record nothing reads.
"""

from __future__ import annotations

from dbt_debt.domain import Manifest, Relation


def unused_sources(manifest: Manifest) -> list[Relation]:
    """Sources no model, exposure, or semantic-layer consumer depends on, sorted by name."""

    referenced: set[str] = set()
    for model in manifest.models.values():
        referenced.update(model.depends_on)
    for exposure in manifest.exposures.values():
        referenced.update(exposure.depends_on)
    for consumer in manifest.semantic_consumers.values():
        referenced.update(consumer.depends_on)
    unused = [
        relation
        for unique_id, relation in manifest.relations.items()
        if unique_id not in referenced
    ]
    return sorted(unused, key=lambda relation: (relation.name, relation.relation_key))
