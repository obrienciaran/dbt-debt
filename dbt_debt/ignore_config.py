"""Loads the operator-maintained ignore list.

Some real consumers cannot be seen in the warehouse's query log at all — a downstream
system fed by a bulk export, a model built ahead of a use that hasn't landed yet. dbt's
own `exposures:` block covers the declared-consumer case (see `verdict.exposures`), but
it only *flags* an unused model feeding one for review; it does not stop the model from
being called unused. This is the harder-to-verify manual override for everything else:
name a model and say why, and it is treated as fully active, exactly like a model the
warehouse evidence shows in real use.

The file lives with the dbt project being scanned (not this tool's own repo), since
which models are known exceptions is project data, not tool configuration. JSON only,
matching how this tool already reads `manifest.json` and `catalog.json`, so reading it
needs no new dependency.
"""

from __future__ import annotations

import json
from pathlib import Path


class IgnoreConfigError(ValueError):
    """The ignore file exists but is not shaped as expected."""


def load_ignored_models(path: Path) -> dict[str, str]:
    """Model name -> reason, read from `path`; `{}` when the file does not exist.

    Every entry must carry a non-empty ``reason``: an ignore with no stated reason
    defeats the point of naming *why* a model is excluded, not just that it is.
    """

    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise IgnoreConfigError(f"{path} is not valid JSON: {exc}") from exc
    entries = raw.get("ignored_models") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise IgnoreConfigError(f'{path} must have a top-level "ignored_models" list.')
    result: dict[str, str] = {}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if not name or not reason:
            raise IgnoreConfigError(
                f'{path}: each ignored_models entry needs a non-empty "name" and "reason".'
            )
        result[str(name)] = str(reason)
    return result
