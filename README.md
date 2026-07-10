# 🧹 dbt-debt

dbt-debt finds the dead weight in a dbt project on BigQuery or Snowflake: which models and
columns nobody uses anymore, which are barely used, which are safe to remove, and which tables
exist in your warehouse with no dbt model behind them. (For Snowflake's install extra,
connection setup, and permissions, see [`USAGE.md`](USAGE.md).)

It works by comparing your dbt project against two things the warehouse already knows, a log of
every query that has run, and a list of the tables that actually exist. There's no account to make
and nothing to log into. If `dbt run` works on your machine, `dbt-debt scan` works too, provided
you have the correct warehouse permissions.

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
  ! 1 semantic-layer consumer affected (review before removing)
      - total_revenue (metric)
  - 5.0 GB reclaimable storage

Top 3 of 3 unused columns (ranked by table bytes; BigQuery has no per-column sizes):
  1. dim_customer.old_marketing_score
  2. fct_orders.legacy_discount_code
  3. mart_sales.temp_margin_calc
```

## 📊 What the numbers mean

A **model** is one of your `.sql` files. A **column** is one field in the table that model builds.
The **lookback window** is how far back we read the warehouse's query log. The default is 180
days, which is also the most BigQuery keeps. Snowflake keeps a year, so `--lookback-days` can go
up to 365 there.

- **active / unused models.** A model is **unused** if, in the lookback window, nothing queried it
  and nothing queried anything built from it. Everything else is **active**. Seeds and snapshots
  are checked the same way and tagged `(seed)` / `(snapshot)` in the lists.
- **rarely used models.** A model that was queried, but at most 5 times in the whole window
  (`--rare-threshold`; `0` turns the band off). These still count as used. Each is listed with its
  query count, last-queried date, and size so an owner can judge whether those few queries still
  earn its keep. They never feed the removable or reclaimable figures.
- **too new to judge.** A model whose table first appeared in the query log fewer than 7 days ago
  (`--min-age-days`) hasn't had a fair chance to be queried yet, so it's listed separately instead
  of being called unused. This guard only applies to the things dbt builds, i.e. models, seeds,
  or snapshots. An orphaned table is reported regardless of its age, as it means "this table exists
  and dbt has no record of it", which is just as true on the day it was made, or a later point in time.
  On Snowflake, a model with no first-seen date at all is set aside the same way, as "missing a
  first-seen date (likely a new table)". The account metadata behind the date
  (`ACCOUNT_USAGE.TABLES`) lags about 90 minutes, so a brand-new table briefly has no age to prove.
- **active / unused columns.** A column is **unused** if no query read it, and nothing read another
  column built from it. Column reads come from parsing the query text, and not every query parses,
  so the report states its confidence, e.g. "column verdicts based on 96% of query text, 183 of
  190 queries parsed". Model-level usage never depends on parsing, so those verdicts are unaffected.
- **columns you could remove.** Unused columns that nothing in your dbt project still depends on (no
  data test, no contract). These are suggestions rather than an automatic delete, because "unused"
  comes from the query log, which can't see everything (see *What counts as usage*). An unused
  column that still has a dependency is listed separately and not counted here.
- **tests you could remove.** Data tests attached to a model or column you'd be removing. If the
  thing goes, the test can go with it.
- **exposures affected.** An **exposure** is a downstream consumer (e.g. a dashboard or report) your
  team has written into the dbt project. An unused model that feeds one is flagged "affected" so you
  check before removing it, and don't pull out something still feeding a dashboard. Exposures whose
  models are all active aren't listed.
- **exposures that are likely dead.** When *every* model an exposure reads is unused, nothing the
  dashboard shows was queried in the whole window, so the dashboard itself is probably dead.
  These are listed by name, separately from the merely affected ones, as candidates to retire.
- **semantic-layer consumers affected.** If your project declares semantic models, metrics, or saved
  queries (dbt's semantic layer), an unused model that feeds one (even through a chain of metrics)
  is flagged for review the same way, and a column a semantic model names is never counted as
  removable.
- **tables with no dbt model behind them (orphans).** A real table or view in a dataset dbt builds
  into, but which dbt has no record of, usually left over from a renamed or deleted model, or made
  by hand. dbt-debt only looks inside the datasets dbt builds into, so any raw input tables are
  never flagged.
- **tables your models read but dbt was never told about.** A model reads from a table you never
  declared. These need to be added as a `source()`. dbt-debt finds these by reading the model's SQL,
  so it needs no extra warehouse permission and shows up even if it can't list the warehouse tables.
- **sources declared but never read.** The reverse case. A source sits in a `sources.yml` but no
  model, exposure, or semantic-layer consumer references it. Each one is listed with its file path
  and any queries people ran against the raw table directly, so you can tell a dead declaration
  (delete the entry) from a table your team reads outside dbt (consider modelling it). A test on
  the source doesn't count as use, since a test guards data without consuming it. This is a review
  list and never feeds the unused-model figures.
- **stale sources.** A declared source whose table has received no new data for more than 30 days
  (`--stale-source-days`; `0` turns the check off) usually means the loader upstream of dbt has
  stopped. The last-data date is read from warehouse metadata: on BigQuery it needs read access
  to the source datasets (skipped with a warning without it), on Snowflake no extra grant. Each
  stale source is listed with the date its data last changed. On Snowflake that date can also
  move on DDL changes, so a stale table can occasionally look fresher than its data.
- **documentation drift.** A column declared in a model's YAML that no longer exists in the built
  table (per `catalog.json`) is stale documentation to delete. Compared against the catalog, so
  rerun `dbt docs generate` first if it's old.
- **coverage.** Three hygiene sentences: how many models have at least one test, how many have a
  description, and how many columns do. The column figure counts the real columns from
  `catalog.json` when it's present (and says so), else the ones declared in YAML.
- **large tables without partitioning or clustering (BigQuery only).** A table or incremental
  model of 1 GB or more built with neither `partition_by` nor `cluster_by` in its config gets a
  full scan from every query that touches it. The largest offenders (up to 20) are listed by
  stored size. Skipped on Snowflake, which micro-partitions automatically.
- **top unused models / columns.** Biggest win first. A whole unused table shows the storage you'd
  reclaim. A single column can't be sized (BigQuery doesn't report storage per column), so columns
  rank by their table's size instead.

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

The warehouse is detected from your dbt artifacts (`--warehouse bigquery|snowflake` overrides).
Snowflake needs the optional extra, `pip install 'dbt-debt[snowflake]'`.

For the cache, how it works, permissions and sign-in (BigQuery and Snowflake), full options, and
how to work on dbt-debt, see [`USAGE.md`](USAGE.md).
