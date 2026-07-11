"""Exposure-impact verdict. A pure manifest traversal, no warehouse needed.

Exposures depend on models, not columns, so impact is computed at model grain. An exposure
is *unaffected* when every upstream model is still active, *affected* when some but not all
upstream models are dead (review before removing), and *dead* when every model it depends on
is dead, meaning nothing queried anything the dashboard reads, so the dashboard itself is likely
dead. The three sets are mutually exclusive. Non-model dependencies (e.g. sources) are
ignored for the all-dead rule, and because the dead set already excludes too-new and
rarely-used nodes, an exposure over those is never flagged.
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
    """Exposures with some, but not all, upstream models dead."""

    return [
        exposure
        for exposure in manifest.exposures.values()
        if _has_dead_upstream(exposure, dead_models)
        and not _all_model_deps_dead(exposure, manifest, dead_models)
    ]


def dead_exposures(manifest: Manifest, dead_models: Set[str]) -> list[Exposure]:
    """Exposures whose every model dependency is dead, so the consumer itself is likely dead."""

    return [
        exposure
        for exposure in manifest.exposures.values()
        if _all_model_deps_dead(exposure, manifest, dead_models)
    ]


def _has_dead_upstream(exposure: Exposure, dead_models: Set[str]) -> bool:
    return any(dep in dead_models for dep in exposure.depends_on)


def _all_model_deps_dead(exposure: Exposure, manifest: Manifest, dead_models: Set[str]) -> bool:
    model_deps = [dep for dep in exposure.depends_on if dep in manifest.models]
    return bool(model_deps) and all(dep in dead_models for dep in model_deps)
