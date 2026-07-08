"""Exposure-impact verdict — a pure manifest traversal, no warehouse needed.

Exposures depend on models, not columns, so impact is computed at model grain. An exposure
is *unaffected* when every upstream model is still active, and *affected* when at least one
upstream model is dead (those need review before any removal).
"""

from __future__ import annotations

from collections.abc import Set

from dbt_debt.domain import Exposure, Manifest


def unaffected_exposures(manifest: Manifest, dead_models: Set[str]) -> list[Exposure]:
    """Exposures whose every upstream model is still active."""

    return [
        exposure
        for exposure in manifest.exposures.values()
        if not _has_dead_upstream(exposure, dead_models)
    ]


def affected_exposures(manifest: Manifest, dead_models: Set[str]) -> list[Exposure]:
    """Exposures with at least one dead upstream model."""

    return [
        exposure
        for exposure in manifest.exposures.values()
        if _has_dead_upstream(exposure, dead_models)
    ]


def _has_dead_upstream(exposure: Exposure, dead_models: Set[str]) -> bool:
    return any(dep in dead_models for dep in exposure.depends_on)
