# 🧹 dbt-debt

dbt-debt finds the dead weight in a dbt project on BigQuery: which models and columns nobody uses
anymore, which are safe to remove, and which tables exist in your warehouse with no dbt model behind
them.

It works by comparing your dbt project against two things BigQuery already knows, a log of every
query that has run, and a list of the tables that actually exist. There's no account to make and
nothing to log into. If `dbt run` works on your machine, `dbt-debt scan` works too, provided you have the correct BigQuery permissions.

dbt-debt only reports. It never edits or deletes anything.

https://github.com/user-attachments/assets/850ba77f-61c8-446e-bf5c-cc49f0225391

```
Models:
  ✓ 213 active
  ✗ 17 unused (incl. 2 seeds)
  ? 1 too new to judge (first seen recently; not counted as unused)
Columns:
  ✓ 4382 active
  ✗ 3 unused
Orphans:
  ✗ 4 tables in managed datasets with no dbt model
  ! 2 sources found but not declared in the manifest

Potential savings:
  - 3 columns removable
  - 2 tests removable
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
The **lookback window** is how far back we read BigQuery's query log (180 days by default).

- **active / unused models.** A model is **unused** if, in the lookback window, nothing queried it
  and nothing queried anything built from it. Everything else is **active**. Seeds and snapshots
  are checked the same way and tagged `(seed)` / `(snapshot)` in the lists.
- **too new to judge.** A model whose table first appeared in the query log fewer than 7 days ago
  (`--min-age-days`) hasn't had a fair chance to be queried yet, so it's listed separately instead
  of being called unused. This guard only applies to the things dbt builds, i.e. models, seeds,
  or snapshots. An orphaned table is reported regardless of its age, as it means "this table exists
  and dbt has no record of it", which is just as true on the day it was made, or a later point in time.
- **active / unused columns.** A column is **unused** if no query read it, and nothing read another
  column built from it.
- **columns you could remove.** Unused columns that nothing in your dbt project still depends on (no
  data test, no contract). These are suggestions, not an automatic delete — "unused" comes from the
  query log, which can't see everything (see *What counts as usage*). An unused column that still
  has a dependency is listed separately and not counted here.
- **tests you could remove.** Data tests attached to a model or column you'd be removing. If the
  thing goes, the test can go with it.
- **exposures affected.** An **exposure** is a downstream consumer (e.g. a dashboard or report) your
  team has written into the dbt project. An unused model that feeds one is flagged "affected — check
  before removing" so you don't pull out something still feeding a dashboard. Exposures whose models
  are all active aren't listed.
- **semantic-layer consumers affected.** If your project declares semantic models, metrics, or saved
  queries (dbt's semantic layer), an unused model that feeds one — even through a chain of metrics —
  is flagged for review the same way, and a column a semantic model names is never counted as
  removable.
- **tables with no dbt model behind them (orphans).** A real table or view in a dataset dbt builds
  into, but which dbt has no record of, usually left over from a renamed or deleted model, or made
  by hand. dbt-debt only looks inside the datasets dbt builds into, so any raw input tables are
  never flagged.
- **tables your models read but dbt was never told about.** A model reads from a table you never
  declared. These need to be added as a `source()`. dbt-debt finds these by reading the model's SQL, so it needs no
  extra BigQuery permission and shows up even if it can't list the entire warehouse tables.
- **top unused models / columns.** Biggest win first. A whole unused table shows the storage you'd
  reclaim; a single column can't be sized (BigQuery doesn't report storage per column), so columns
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
every column), asks BigQuery what's been used, and tells you what isn't.

In the terminal, `dbt-debt scan` opens a simple tabbed UI (Summary / Detail / JSON /
Export). When you pipe the output, run it in CI, or send it to a script, there is no UI and the report just prints:

| What you run | What you get |
|---|---|
| `dbt-debt scan` piped or in CI | a plain summary |
| `dbt-debt scan --detail` | the full list — every unused table, column, and orphan |
| `dbt-debt scan --format json` | JSON (pipe it to `jq`) |
| `dbt-debt scan --format json -o debt.json` | JSON written to a file |
| `dbt-debt scan --no-interactive` | plain text, even in a terminal |
| `dbt-debt scan --orphans` | just the orphan and undeclared-source report |
| `dbt-debt scan --min-age-days 30` | anything first seen in the last 30 days is "too new to judge", not unused (default: 7; `0` turns the guard off) |

For the cache, how it works, permissions, full options, and how to work on dbt-debt, see
[`USAGE.md`](USAGE.md).
