"""Shared JSON-coercion helper for the artifact readers.

dbt artifacts sometimes carry explicit nulls where a nested object is expected. Coercing a
possibly-missing or null field to a dict keeps the manifest and catalog parsers free of
repetitive None-guards.
"""

from __future__ import annotations

from typing import Any


def as_dict(value: object) -> dict[str, Any]:
    """Coerce a possibly-missing or null artifact field to a dict."""

    return value if isinstance(value, dict) else {}
