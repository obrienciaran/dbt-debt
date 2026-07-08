"""Shared JSON-coercion helper for the artifact readers.

dbt artifacts sometimes carry explicit nulls where a nested object is expected. Coercing a
possibly-missing or null field to a dict keeps the manifest and catalog parsers free of
repetitive None-guards.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dbt_debt.artifacts.errors import ArtifactError


def as_dict(value: object) -> dict[str, Any]:
    """Coerce a possibly-missing or null artifact field to a dict."""

    return value if isinstance(value, dict) else {}


def load_artifact(path: str | Path) -> dict[str, Any]:
    """Read one artifact file as a JSON object, raising `ArtifactError` on anything else.

    Both loaders funnel through here so a truncated, hand-edited, or wrong-format file fails
    with the path in the message rather than a bare `JSONDecodeError` traceback.
    """

    try:
        data = json.loads(Path(path).read_text())
    except OSError as exc:
        raise ArtifactError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ArtifactError(
            f"{path} is not valid JSON ({exc}) — re-run dbt to rebuild it."
        ) from exc
    if not isinstance(data, dict):
        raise ArtifactError(f"{path} is not a dbt artifact (expected a JSON object).")
    return data
