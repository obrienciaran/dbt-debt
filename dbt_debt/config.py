"""Run configuration for a scan.

A single immutable value object so the layers never reach for argparse or environment state
directly. The consumption layer reads region/lookback/exclusion from here; the CLI is the only
place that builds it from arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_QUERY_COMMENT_PATTERN = r'"app":\s*"dbt"'
"""Regex matched against the logged query text to drop dbt's own queries.

dbt tags every statement it issues with a leading JSON query-comment containing `"app": "dbt"`.
Matching that, rather than the statement type, excludes dbt-issued `SELECT`s (its data tests)
which the statement-type filter alone would keep. Configurable for non-default comments.
"""

SUPPORTED_WAREHOUSES = ("bigquery", "snowflake", "redshift", "databricks")
"""The warehouses a scan can target, in the order everything else lists them.

This order is the style for every warehouse enumeration, in code and in docs. It is the
order support was added and the order of completeness, BigQuery being the first
implementation and Databricks the newest. argparse shows it to users in `--warehouse` help, so
a list that disagrees reads as a different set.
"""

WAREHOUSE_DIALECTS = {
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "redshift": "redshift",
    "databricks": "databricks",
}
"""The sqlglot dialect every SQL parse uses, keyed by warehouse."""

WAREHOUSE_RETENTION_DAYS = {
    "bigquery": 180,
    "snowflake": 365,
    "redshift": 7,
    "databricks": 365,
}
"""How much query history each warehouse keeps, keyed by warehouse.

Snowflake's ACCOUNT_USAGE and Databricks' query-history and lineage system tables both document
a year. BigQuery's INFORMATION_SCHEMA.JOBS is documented at 180 days, which is also the default
window, so it only ever bites on an explicit over-ask. Redshift is the odd one: AWS documents
seven days for the older STL views and states no retention at all for the SYS views, so seven is
a conservative floor rather than a measurement of what any account holds. `next_steps.md` tracks
the experiment measuring it.

This bounds what the report *claims* to have seen, so a model queried less often than the real
window is not silently called unused. It deliberately does not bound what the adapters ask the
warehouse for, and it does not need to: a warehouse cannot return history it no longer holds, so
asking for 400 days returns exactly what asking for the retained window would. On Redshift that
distinction is load-bearing, because AWS states no SYS retention and an account may keep more
than the floor, so clamping the queries could discard evidence we would otherwise have. See the
comment above `RealRedshiftClient.table_usage`.
"""


@dataclass(frozen=True)
class Config:
    """Everything a scan needs, resolved once from CLI arguments."""

    project_dir: Path = Path(".")
    target_path: Path = Path("target")
    project: str | None = None
    region: str = "US"
    warehouse: str = "bigquery"
    """Resolved before the client is built: `--warehouse`, else the manifest's adapter_type."""
    connection: str | None = None
    """Named Snowflake connection (connections.toml); the connector's default when None."""
    lookback_days: int = 180
    query_comment_pattern: str = DEFAULT_QUERY_COMMENT_PATTERN
    columns: bool = False
    min_age_days: int = 7
    rare_threshold: int = 5
    """Queried models with at most this many queries in the window are "rarely used"; 0 disables."""
    stale_source_days: int = 30
    """Declared sources with no new data for more than this many days are stale; 0 disables."""
    output_format: str = "text"
    top_n: int = 10
    cache: bool = True
    cache_ttl_hours: float | None = None
    """An explicit `--cache-ttl`; None means unspecified, so new entries get the 1h default and
    existing entries keep the TTL they were written with."""

    DEFAULT_CACHE_TTL_HOURS = 1.0

    @property
    def dialect(self) -> str:
        """The sqlglot dialect matching the warehouse the scan targets."""

        return WAREHOUSE_DIALECTS.get(self.warehouse, self.warehouse)

    @property
    def effective_lookback_days(self) -> int:
        """`lookback_days` bounded by what the warehouse's query history is known to retain.

        Read this only once `warehouse` is resolved (the CLI replaces the field after reading
        the manifest); before that it reports the BigQuery default's window.
        """

        cap = WAREHOUSE_RETENTION_DAYS.get(self.warehouse)
        return min(self.lookback_days, cap) if cap is not None else self.lookback_days

    @property
    def manifest_path(self) -> Path:
        """Location of `manifest.json` (an absolute `target_path` overrides `project_dir`)."""

        return self.project_dir / self.target_path / "manifest.json"

    @property
    def catalog_path(self) -> Path:
        """Location of `catalog.json` (consumed by the column stage)."""

        return self.project_dir / self.target_path / "catalog.json"
