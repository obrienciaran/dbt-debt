"""Run configuration for a scan.

A single immutable value object so the layers never reach for argparse or environment state
directly. The consumption layer reads region/lookback/exclusion from here; the CLI is the only
place that builds it from arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_QUERY_COMMENT_PATTERN = r'"app":\s*"dbt"'
"""Regex matched against `JOBS.query` to drop dbt's own queries.

dbt tags every statement it issues with a leading JSON query-comment containing `"app": "dbt"`.
Matching that, rather than the statement type, excludes dbt-issued `SELECT`s (its data tests)
which the `statement_type` filter alone would keep. Configurable for non-default comments.
"""


@dataclass(frozen=True)
class Config:
    """Everything a scan needs, resolved once from CLI arguments."""

    project_dir: Path = Path(".")
    target_path: Path = Path("target")
    project: str | None = None
    region: str = "US"
    lookback_days: int = 180
    query_comment_pattern: str = DEFAULT_QUERY_COMMENT_PATTERN
    columns: bool = False
    min_age_days: int = 7
    output_format: str = "text"
    top_n: int = 10
    cache: bool = True
    cache_ttl_hours: float = 1.0

    @property
    def manifest_path(self) -> Path:
        """Location of `manifest.json` (an absolute `target_path` overrides `project_dir`)."""

        return self.project_dir / self.target_path / "manifest.json"

    @property
    def catalog_path(self) -> Path:
        """Location of `catalog.json` (consumed by the column stage)."""

        return self.project_dir / self.target_path / "catalog.json"
