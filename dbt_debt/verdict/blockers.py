"""The "unused != removable" blocker check — a pure manifest traversal.

A column can be dead-by-usage yet load-bearing: it may back a data test or be bound by an
enforced model contract. The design rule (see README, "Unused ≠ removable") is:

    removable = dead AND zero blockers

We surface the blockers per column rather than emit a flat "removable" count, so the advice
is defensible rather than dangerous. Macro references and `SELECT *` reliance are further
blockers but are not manifest-only (they need lineage), so they are out of scope here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from dbt_debt.domain import ColumnRef, Manifest


@dataclass(frozen=True)
class ColumnBlockers:
    """The manifest-discoverable reasons a dead column may not be safe to drop."""

    model_unique_id: str
    column_name: str
    backed_by_tests: tuple[str, ...]
    contract_enforced: bool
    backed_by_semantic_models: tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return (
            bool(self.backed_by_tests)
            or self.contract_enforced
            or bool(self.backed_by_semantic_models)
        )


def column_blockers(manifest: Manifest, model_unique_id: str, column_name: str) -> ColumnBlockers:
    """Compute the blocker analysis for a single column.

    A semantic model naming the column blocks it the same way a test does: declared use, not
    observed use — folding it into the consumed set would silently mark the column active,
    whereas "unused but blocked" is the honest verdict.
    """

    model = manifest.models.get(model_unique_id)
    contract_enforced = bool(
        model is not None and model.contract_enforced and column_name in model.columns
    )
    backed_by_tests = tuple(
        test.unique_id
        for test in manifest.tests.values()
        if test.attached_node == model_unique_id and test.column_name == column_name
    )
    backed_by_semantic_models = tuple(
        consumer.unique_id
        for consumer in manifest.semantic_consumers.values()
        if (model_unique_id, column_name) in consumer.column_refs
    )
    return ColumnBlockers(
        model_unique_id=model_unique_id,
        column_name=column_name,
        backed_by_tests=backed_by_tests,
        contract_enforced=contract_enforced,
        backed_by_semantic_models=backed_by_semantic_models,
    )


def analyze_columns(manifest: Manifest, dead_columns: Iterable[ColumnRef]) -> list[ColumnBlockers]:
    """Blocker analysis for each dead column, ordered deterministically.

    Filter the result by `.is_blocked` to split clean-removable columns from those needing
    review.
    """

    return [
        column_blockers(manifest, model_id, column_name)
        for model_id, column_name in sorted(dead_columns)
    ]
