# 🧹 dbt-debt

dbt-debt finds the dead weight in a dbt project on BigQuery: which models and columns nobody uses
anymore, which are safe to remove, and which tables exist in your warehouse with no dbt model behind
them.

It works by comparing your dbt project against two things BigQuery already knows — a log of every
query that has run, and a list of the tables that actually exist. There's no account to make and
nothing to log into: if `dbt run` works on your machine, `dbt-debt scan` works too.

dbt-debt only reports. It never edits or deletes anything.

```
Models:
  ✓ 213 active
  ✗ 17 unused
Columns:
  ✓ 4,382 active
  ✗ 623 unused
Orphans:
  ✗ 4 tables in your dbt datasets with no dbt model behind them
  ! 2 tables your models read that dbt was never told about
Potential savings:
  - 623 columns you could remove
  - 71 tests you could remove with them
Top 10 of 623 unused columns (ranked by table size; BigQuery can't size a single column):
  1. dim_customer.old_marketing_score
  2. fct_orders.legacy_discount_code
  3. mart_sales.temp_margin_calc
```

## 📊 What the numbers mean

A **model** is one of your `.sql` files. A **column** is one field in the table that model builds.
The **lookback window** is how far back we read BigQuery's query log (180 days by default).

- **active / unused models.** A model is **unused** if, in the lookback window, nothing queried it
  and nothing queried anything built from it. Everything else is **active**.
- **active / unused columns.** A column is **unused** if no query read it, and nothing read another
  column built from it.
- **columns you could remove.** Unused columns that nothing in your dbt project still depends on (no
  data test, no contract). These are suggestions, not an automatic delete — "unused" comes from the
  query log, which can't see everything (see *What counts as usage*). An unused column that still
  has a dependency is listed separately and not counted here.
- **tests you could remove.** Data tests attached to a model or column you'd be removing. If the
  thing goes, the test can go with it.
- **exposures affected.** An **exposure** is a downstream consumer — a dashboard or report — your
  team has written into the dbt project. An unused model that feeds one is flagged "affected — check
  before removing" so you don't pull out something still feeding a dashboard. Exposures whose models
  are all active aren't listed.
- **tables with no dbt model behind them (orphans).** A real table or view in a dataset dbt builds
  into, but which dbt has no record of — usually left over from a renamed or deleted model, or made
  by hand. We only look inside the datasets dbt builds into, so your raw input tables are never
  flagged.
- **tables your models read but dbt was never told about.** A model reads from a table you never
  declared; add it as a `source()`. We find these by reading the model's own SQL, so it needs no
  extra BigQuery permission and shows up even when we can't list the warehouse.
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

In a normal terminal, `dbt-debt scan` opens a simple tabbed viewer (Summary / Detail / JSON /
Export) — no flags to remember. When you pipe the output, run it in CI, or send it to a script, the
viewer steps aside and the report just prints:

| What you run | What you get |
|---|---|
| `dbt-debt scan` piped or in CI | a plain summary |
| `dbt-debt scan --detail` | the full list — every unused table, column, and orphan |
| `dbt-debt scan --format json` | JSON (pipe it to `jq`) |
| `dbt-debt scan --format json -o debt.json` | JSON written to a file |
| `dbt-debt scan --no-interactive` | plain text, even in a terminal |
| `dbt-debt scan --orphans` | just the orphan and undeclared-source report |

### ⚡ Making repeat runs fast (the cache)

The slow part of a scan is talking to BigQuery, so the first scan saves its BigQuery results to a
small file in your temp folder. Run `scan` (or `scan --columns`) again soon after and it reads that
file instead of re-querying, finishing almost instantly.

Saved results count as fresh for 1 hour; after that the next scan refetches and replaces them. Change
the window with `--cache-ttl <hours>`, or skip saved results with `--no-cache` for the latest
numbers.

The file isn't deleted when it goes stale — the 1-hour limit only decides when results are too old to
trust. It stays in your temp folder until something removes it:

- `dbt-debt --clear-cache` deletes all of dbt-debt's saved results and does nothing else;
- `dbt-debt scan --clear-cache` deletes this project's results, then runs a fresh scan;
- the next scan replaces results over an hour old;
- or your OS clears its temp folder — slowly and unpredictably (Windows may never do it), so don't
  count on this.

These behave the same on Mac, Windows, and Linux. For a clean slate, `dbt-debt --clear-cache`.

## 🔧 How it works

1. Read `manifest.json` and `catalog.json` from `target/`. (dbt-debt never imports or runs dbt — it
   just reads the files dbt already wrote.)
2. Ask BigQuery which tables real people queried in the lookback window, ignoring dbt's own queries.
   With `--columns`, also read those queries' text to see which columns they used.
3. Trace where each column came from, using your models' SQL, so usage flows back up to the columns
   that fed it.
4. Compare what got used against everything in your project, and report what's unused and safe to
   remove.
5. Look at the tables that really exist in the datasets dbt builds into, and flag the ones dbt has no
   record of (orphans), plus the tables your models read but you never declared.

### 🔍 Orphans and undeclared sources, explained

dbt tracks two kinds of table: the ones it builds (models, seeds, snapshots) and the ones it reads
(declared sources). The orphan check compares both against what's actually in BigQuery and flags two
mismatches:

- An **orphan** is a table really there in BigQuery, in a dataset dbt builds into, but with no dbt
  record — usually left over from a renamed or deleted model, or made by hand.
- An **undeclared source** is a table a model reads from that you never told dbt about; fix it by
  declaring it as a `source()`.

Two rules keep these honest: we only look inside the datasets dbt builds into (so raw input tables
are never flagged), and a table a model reads always counts as undeclared, never as an orphan.

## 🎯 What counts as "usage"

Usage is any `SELECT` that ran against BigQuery in the lookback window and wasn't dbt's own query —
including BI tools and dashboards that query BigQuery directly (Looker, Tableau, scheduled queries),
which land in the query log like anything else.

A few cases to keep in mind:

- **Reads that don't hit BigQuery** — a cached BI extract, a scheduled export, a copy downstream —
  never appear in the query log, so they can look unused. Tell dbt-debt about them by declaring
  exposures (see below); a model that feeds one is flagged for review instead of marked removable.
- **Anything used less often than the lookback window.** The default 180 days is also the max, since
  that's all BigQuery keeps — raising `--lookback-days` won't help. A report that runs once a year
  can look unused, so those need a human call.
- **`SELECT *`** is handled carefully: every column counts as used, so a column read only through a
  `*` is never wrongly called unused.

So "unused" means "no sign of use in the log." How far to trust it depends on *who* reads the column:

- Columns mid-pipeline are mostly read by other dbt models, whose reads land in the log. Nothing in
  the log is a strong "unused" signal you can trust.
- Columns at the end — your final marts — are often read by tools outside BigQuery, like a dashboard
  or export, whose reads can miss the log. An "unused" verdict there is less certain; use judgement.
  Best practice is to declare those consumers as exposures so a model feeding one is flagged for
  review instead.

### 📣 Telling dbt-debt about your dashboards (exposures)

dbt-debt doesn't hunt for dashboards; it reads the exposures your team has already written down. An
exposure is a small block in any `.yml` file naming the models a downstream thing depends on:

```yaml
exposures:
  - name: weekly_revenue_dashboard
    type: dashboard
    url: https://looker.example.com/dashboards/42
    depends_on:
      - ref('fct_orders')
      - ref('dim_customers')
    owner:
      name: Analytics
      email: analytics@example.com
```

The more real consumers you write down this way, the fewer things get wrongly called "unused" at the
end of your pipeline.

## 🔐 Permissions

dbt-debt signs in the same way `gcloud` does (`gcloud auth application-default login`) and runs in
the project your models live in (read from your project, or set with `--project`).

- **Required:** permission to see everyone's queries, not just your own (`bigquery.jobs.listAll`,
  part of `roles/bigquery.resourceViewer`). dbt-debt checks for this up front and stops if it's
  missing; otherwise "unused" would quietly mean "unused by me".
- **Optional (for orphans):** read access to the datasets dbt builds into. Listing the tables that
  physically exist asks each dataset for its own table list — basic read access anyone who writes dbt
  models already has, not the project-wide access even an Owner can be refused. Without it, the
  orphan list is skipped with a warning and the rest of the scan is unaffected.

That required grant is the only one. Table sizes (used to rank unused tables) come from
`catalog.json`, which `dbt docs generate` already fills in, so they need no extra access.

## ⚙️ Options

```
dbt-debt scan
    --project-dir .           your dbt project folder (default: current folder)
    --target-path target      where manifest.json and catalog.json live
    --project <id>            which Google Cloud project to query (default: read from your models)
    --region US               which BigQuery region your query log is in
    --lookback-days 180       how far back to look; 180 is also the max BigQuery keeps
    --query-comment-pattern   how to recognise dbt's own queries (a regex)
    --columns                 also check which columns are unused (default: models only)
    --detail                  list every unused table and column (grouped by model, with file paths)
    --format text|json        json always includes the full list
    -o, --output <file>       write the report to a file instead of the screen
    --no-interactive          print the report instead of opening the viewer
    --orphans                 print only the orphan and undeclared-source report
    --no-cache                ask BigQuery directly, ignoring (and not writing) saved results
    --cache-ttl 1             how many hours saved results stay fresh before being re-fetched
    --clear-cache             clear this project's saved results, then run a fresh scan
```

To clear saved results *without* running a scan, drop the `scan`:

```
dbt-debt --clear-cache        delete all of dbt-debt's saved results and stop
```

Exit codes: `0` all good, `2` couldn't find the dbt files, `3` missing the required permission.

## 🛠️ Working on dbt-debt

```
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy dbt_debt
```

The tests run on small sample dbt files with a stand-in for BigQuery, so they need no cloud access
and no credentials. For how the code is put together, see [`DESIGN.md`](DESIGN.md).
