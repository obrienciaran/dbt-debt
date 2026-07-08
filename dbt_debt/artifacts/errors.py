"""Shared error type for the artifact readers.

dbt's artifacts are read as plain JSON, so a truncated or hand-edited file surfaces here as a
parse failure. The loaders raise `ArtifactError` with the offending path in the message; the CLI
turns it into a friendly exit instead of a traceback.
"""

from __future__ import annotations


class ArtifactError(ValueError):
    """Raised when a dbt artifact on disk cannot be read or is not valid artifact JSON."""
