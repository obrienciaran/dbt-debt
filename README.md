# 🧹 dbt-debt

dbt-debt finds the dead weight in a dbt project on BigQuery, Snowflake, or Redshift: which
models and columns nobody uses anymore, which are barely used, which are safe to remove, and
which tables exist in your warehouse with no dbt model behind them. (For the Snowflake and
Redshift install extras, connection setup, and permissions, see [`USAGE.md`](USAGE.md).)

It works by comparing your dbt project against two things the warehouse already knows: a log of
every query that has run, and a list of the tables that actually exist. There's no account to
make and nothing to log into. If `dbt run` works on your machine, `dbt-debt scan` works too,
provided you have the right warehouse permissions.

👉 dbt-debt only reports. It never edits or deletes anything.

https://github.com/user-attachments/assets/850ba77f-61c8-446e-bf5c-cc49f0225391

```
Models:
  ✓ 213 active
  ✗ 17 unused (incl. 2 seeds)
  ~ 5 rarely used (at most 5 queries; not counted in 'unused')
  ? 1 too new to judge (first seen recently; not counted in 'unused')
Columns:
  ✓ 4382 active
  ✗ 3 unused
Sources:
  ✗ 1 declared source nothing in the project reads
  ! 1 source stale (no new data in 30+ days)
Docs drift:
  ! 2 documented columns no longer exist in the table
Orphans:
  ✗ 4 tables in managed datasets with no dbt model
  ! 2 sources found but not declared in the manifest
Coverage:
  - tests: 121 of 230 models have at least one test (53%)
  - model docs: 88 of 230 models have a description (38%)
  - column docs: 1930 of 4385 columns have a description (44%, catalog columns)

Potential savings:
  - 3 columns removable
  - 2 tests removable
  ! 1 exposure fed only by unused models (likely dead)
      - legacy_kpi_dashboard
  ! 1 exposure affected (review before removing)
      - weekly_revenue_dashboard
  ! 1 semantic-layer consumer reads unused models (it would break if those models are removed):
      - total_revenue (metric) — built on legacy_revenue (unused)
  - 5.0 GB reclaimable storage

Top 3 of 3 unused columns (ranked by table bytes; BigQuery has no per-column sizes):
  1. dim_customer.old_marketing_score
  2. fct_orders.legacy_discount_code
  3. mart_sales.temp_margin_calc
```

## 📊 What the numbers mean

A **model** is one of your `.sql` files. A **column** is one field in the table that model builds.
The **lookback window** is how far back we read the warehouse's query log: 180 days by default,
which is also the most BigQuery keeps. Snowflake keeps a year, so `--lookback-days` can go up
to 365 there. Redshift's SYS query-history views keep much less (AWS leaves the exact
retention unstated; the older STL views keep seven days), so on Redshift the effective window
is however much history the account actually retains, so "unused" there means "unused within
that history".

- **active / unused models.** A model is **unused** if, in the window, nothing queried it and
  nothing queried anything built from it. Everything else is **active**. Seeds and snapshots are
  checked the same way and tagged `(seed)` / `(snapshot)`.
- **rarely used models.** Queried, but at most 5 times in the whole window (`--rare-threshold`;
  `0` turns the band off). These still count as used and never feed the removable or reclaimable
  figures. Each is listed with its query count, last-queried date, size, and the bytes those few
  queries scanned, most scanned first. A big scanned figure on a tiny query count means expensive
  and barely used, the strongest case for deprecating.
- **too new to judge.** A model first seen in the query log fewer than 7 days ago
  (`--min-age-days`) hasn't had a fair chance to be queried, so it's listed separately instead of
  being called unused. The guard only covers what dbt builds (models, seeds, snapshots); an
  orphan is reported whatever its age, because "dbt has no record of this table" is true from day
  one. On Snowflake, a model with no first-seen date at all is set aside the same way ("missing a
  first-seen date, likely a new table"), because the metadata behind the date
  (`ACCOUNT_USAGE.TABLES`) lags about 90 minutes.
- **active / unused columns.** A column is **unused** if no query read it and nothing read a
  column built from it. Column reads come from parsing query text, and not every query parses, so
  the report states its confidence ("column verdicts based on 96% of query text"). Model verdicts
  never depend on parsing.
- **columns you could remove.** Unused columns nothing in the project still depends on (no data
  test, no contract). These are suggestions, not an automatic delete, since "unused" comes from
  the query log, which can't see everything (see *What counts as usage*). An unused column that
  still has a dependency is listed separately and not counted here.
- **tests you could remove.** Data tests attached to an unused model or column. Remove the model
  or column and its tests go with it.
- **exposures affected.** An **exposure** is a downstream consumer (a dashboard, a report) your
  team has written into the dbt project. An unused model feeding one is flagged "affected" so you
  check before removing it. Exposures whose models are all active aren't listed.
- **exposures that are likely dead.** When *every* model an exposure reads is unused, nothing the
  dashboard shows was queried all window, so the dashboard itself is probably dead. Listed by
  name, separately from the merely affected, as candidates to retire.
- **semantic-layer consumers affected.** An unused model feeding a semantic model, metric, or
  saved query (even through a chain of metrics) is flagged for review the same way, and a column
  a semantic model names is never counted as removable. Each consumer is listed with what makes
  it affected: the unused model it is built on, or the affected consumer it reads through
  ("via"), so you can see the whole chain from the saved query down to the model.
- **tables with no dbt model behind them (orphans).** A real table or view in a dataset dbt
  builds into, but with no dbt record, usually left over from a renamed or deleted model, or
  made by hand. Only datasets dbt builds into are searched, so raw input tables are never
  flagged. Each is listed with any direct queries people ran against it (count, last date,
  bytes scanned); a queried orphan is still in use and dangerous to drop, so those come first.
- **tables your models read but dbt was never told about.** A model reads a table you never
  declared; add it as a `source()`. Found by reading the model's SQL, so it needs no extra
  warehouse permission.
- **sources declared but never read.** The reverse case, a `sources.yml` entry no model, exposure,
  or semantic-layer consumer references. Each is listed with its file path and any direct queries
  people ran against the raw table (count, last date, bytes scanned), so you can tell a dead
  declaration (delete the entry) from a table your team reads outside dbt (consider modelling
  it). A test on the source doesn't count as use. A review list; never feeds the unused-model
  figures.
- **stale sources.** A declared source with no new data for more than 30 days
  (`--stale-source-days`; `0` turns the check off), which usually means the loader upstream of
  dbt has stopped. The date comes from warehouse metadata. BigQuery needs read access to the
  source datasets (skipped with a warning without it); Snowflake needs no extra grant. On
  Snowflake the date also moves on DDL changes (even a table comment), so a stale table can
  occasionally look fresher than its data. Redshift exposes no last-modified metadata at all,
  so the check is skipped there with a note.
- **documentation drift.** A column declared in a model's YAML that no longer exists in the
  built table (per `catalog.json`) is stale documentation to delete. Rerun `dbt docs generate`
  first if the catalog is old.
- **coverage.** Three hygiene sentences covering how many models have at least one test, how many
  have a description, and how many columns do. The column figure counts the real columns from
  `catalog.json` when present (and says so), else the ones declared in YAML.
- **large tables without partitioning or clustering (BigQuery only).** A table or incremental
  model of 1 GB or more built with neither `partition_by` nor `cluster_by` gets a full scan from
  every query that touches it. The offenders (up to 20) are listed with stored size and the
  bytes user queries scanned over the window, most scanned first, so the top entry is the
  partitioning fix that saves the most. BigQuery only: Snowflake micro-partitions
  automatically, and Redshift manages sort and distribution itself.
- **large tables needing maintenance (Redshift only).** A table of 1 GB or more whose
  `SVV_TABLE_INFO` row shows a big unsorted region (20%+, needs VACUUM), stale planner
  statistics (`stats_off` 10+, needs ANALYZE), or heavy slice skew (4x+, needs a
  distribution-key review). Listed with stored size and the bytes user queries scanned, most
  scanned first. Automatic vacuum and analyze usually keep this list empty, and an empty list is
  the healthy state, and BigQuery and Snowflake maintain storage layout themselves.
- **top unused models / columns.** Biggest win first. A whole unused table shows the storage
  you'd reclaim; on Snowflake and Redshift the sizes come live from the warehouse (no
  `dbt docs generate` needed), and Snowflake's include the time-travel and fail-safe copies
  still billed for it. Columns can't be sized (no warehouse reports storage per column), so
  they rank by their table's size.

## 📦 Installing it

Not on PyPI yet, so install from a copy of this repo (needs Python 3.10+):

```
git clone <this repo>
cd dbt-debt
pip install .
```

## 🚀 Using it

```
cd your-dbt-project
dbt docs generate                 # refreshes the column list dbt-debt reads
dbt-debt scan                     # checks models only
dbt-debt scan --columns           # checks models and columns
```

`scan` reads two files dbt writes into `target/` (`manifest.json`, your project; `catalog.json`,
every column), asks the warehouse what's been used, and tells you what isn't.

In the terminal, `dbt-debt scan` opens a simple tabbed UI (Summary / Detail / JSON /
Export / Help; the Help tab lists the scan flags and example commands). When you pipe the output, run it in CI, or send it to a script, there is no UI and the report just prints:

| What you run | What you get |
|---|---|
| `dbt-debt scan` | basic reporting |
| `dbt-debt scan --print` | the full plain-text report: every unused table, column, and orphan |
| `dbt-debt scan --format json` | JSON (pipe it to `jq`) |
| `dbt-debt scan --format json -o debt.json` | JSON written to a file |
| `dbt-debt scan --orphans` | just the orphan and undeclared-source report |
| `dbt-debt scan --min-age-days 30` | anything first seen in the last 30 days is "too new to judge", not unused (default: 7; `0` turns the guard off) |
| `dbt-debt scan --rare-threshold 10` | models with at most 10 queries in the window are "rarely used" (default: 5; `0` turns the band off) |

The warehouse is detected from your dbt artifacts (`--warehouse bigquery|snowflake|redshift`
overrides). Snowflake and Redshift need their optional extras, `pip install
'dbt-debt[snowflake]'` / `pip install 'dbt-debt[redshift]'`.

For the cache, how it works, permissions and sign-in (BigQuery, Snowflake, and Redshift), full
options, and how to work on dbt-debt, see [`USAGE.md`](USAGE.md).

## 🔧 Note

- Built using Claude Fable 5 and 10 years of experience as a data professional in medium to large enterprises.
- Security audited using Cloudflare's [`security audit skill`](https://github.com/cloudflare/security-audit-skill).
- If you love this project, please consider giving it a ⭐.
