# Changelog

All notable changes to this project are recorded here. Versions follow
[semantic versioning](https://semver.org/), and the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Databricks support.** A fourth warehouse adapter, installed with the `[databricks]` extra
  and connected through `DATABRICKS_*` environment variables. Usage combines
  `system.access.table_lineage` with `system.query.history`, following
  `cache_origin_statement_id` so result-cache repeats still count as use. Orphan discovery
  reads `system.information_schema.tables`. First-seen comes from retained lineage, and a
  relation absent from it is always set aside as unproven rather than judged, even when
  `--min-age-days` is zero.
- **Continuous integration.** `.github/workflows/ci.yml` runs `pytest`, `ruff check`,
  `ruff format --check`, and `mypy` on every push and pull request, across Python 3.10 to 3.13
  and with no warehouse SDK installed.
- **Per-warehouse retention reporting.** The scan no longer claims a lookback window the
  warehouse cannot answer for. Each warehouse declares how much query history it keeps
  (BigQuery 180 days, Snowflake 365, Redshift 7, Databricks 365). Ask for more and the report
  falls back to that maximum and says so, on the scorecard header and on stderr:

  ```
  Only 7 days lookback displayed (180 requested but Redshift SYS views retain only 7)
  ```

  JSON output carries `requested_lookback_days` alongside `lookback_days`, set only when the
  window was cut.

### Fixed

- **Redshift scans no longer overstate the evidence window.** Redshift's `SYS` views retain
  far less than the 180-day default, so a project queried less often than weekly could read as
  entirely unused with nothing indicating the history had been truncated. The window is now
  reported as seven days on Redshift. ([#10](https://github.com/obrienciaran/dbt-debt/issues/10))

### Notes

- The retention cap governs what the report claims, not what the adapters ask the warehouse
  for. A warehouse cannot return history it no longer holds, so the queries still request the
  full window. On Redshift this matters: AWS documents seven days for the older `STL` views and
  states no retention for the `SYS` views, so an account may hold more, and clamping the
  queries could discard evidence.
- Redshift's seven days is a conservative floor taken from AWS's `STL` documentation, not a
  measurement of any particular account.
- On Databricks, column analysis, source freshness, and live storage metrics are deferred, each
  tracked as a GitHub issue.

## [0.0.1] - 2026-07-12

Initial release. A technical-debt scorecard for dbt projects on BigQuery, Snowflake, and
Redshift. Finds unused models, seeds, snapshots, and columns by joining the dbt graph against
the warehouse's query history, and reports what is safely removable. Also finds orphaned
relations, undeclared sources, unused declared sources, and stale sources. Reports only, and
never deletes anything.

[Unreleased]: https://github.com/obrienciaran/dbt-debt/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/obrienciaran/dbt-debt/releases/tag/v0.0.1
