"""Resolves ignore-list model names against the manifest. Pure, no I/O.

Kept separate from `ignore_config` (which only reads the file) because resolving names
needs the manifest, and a name that matches nothing in it almost always means a typo or
a renamed/removed model — worth failing loudly rather than the entry silently doing
nothing, especially since an ignored model is otherwise invisible in the report.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from dbt_debt.domain import Manifest

logger = logging.getLogger(__name__)


class UnknownIgnoredModelError(ValueError):
    """An ignore-list entry names a model that does not exist in the manifest."""


def ignored_model_ids(manifest: Manifest, reasons: Mapping[str, str]) -> dict[str, str]:
    """Resolve ignore-list entries by unique_id, returning unique_id -> reason.

    `reasons` maps from manifest unique_id (preferred) or display name (legacy fallback) to
    the stated reason. Duplicate display names across packages are resolved by unique_id, and
    unknown or disabled entries are warned about and skipped rather than killing the scan.
    """

    id_to_reason: dict[str, str] = {}
    name_to_id: dict[str, str] = {}
    duplicates: set[str] = set()
    for unique_id, model in manifest.models.items():
        if model.name in name_to_id:
            duplicates.add(model.name)
        else:
            name_to_id[model.name] = unique_id
    # Prefer unique_id matches; any remaining keys try display-name lookup.
    unresolved: list[str] = []
    for key, reason in reasons.items():
        if key in manifest.models:
            id_to_reason[key] = reason
            continue
        if key in manifest.disabled_models:
            logger.warning("ignore entry %r points to a disabled model; skipping", key)
            continue
        if key in duplicates:
            logger.warning(
                "ignore entry %r matches multiple models by name; resolve by unique_id instead", key
            )
            unresolved.append(key)
            continue
        if key in name_to_id:
            id_to_reason[name_to_id[key]] = reason
        else:
            unresolved.append(key)
    if unresolved:
        raise UnknownIgnoredModelError(
            "ignore file names models not found in the manifest: " + ", ".join(sorted(unresolved))
        )
    return id_to_reason
