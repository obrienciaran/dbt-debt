"""Resolves ignore-list model names against the manifest. Pure, no I/O.

Kept separate from `ignore_config` (which only reads the file) because resolving names
needs the manifest, and a name that matches nothing in it almost always means a typo or
a renamed/removed model — worth failing loudly rather than the entry silently doing
nothing, especially since an ignored model is otherwise invisible in the report.
"""

from __future__ import annotations

from collections.abc import Mapping

from dbt_debt.domain import Manifest


class UnknownIgnoredModelError(ValueError):
    """An ignore-list entry names a model that does not exist in the manifest."""


def ignored_model_ids(manifest: Manifest, reasons: Mapping[str, str]) -> set[str]:
    """Unique_ids for every ignore-list model name, or raise if any name is unknown."""

    name_to_id = {model.name: unique_id for unique_id, model in manifest.models.items()}
    unknown = sorted(set(reasons) - set(name_to_id))
    if unknown:
        raise UnknownIgnoredModelError(
            "ignore file names models not found in the manifest: " + ", ".join(unknown)
        )
    return {name_to_id[name] for name in reasons}
