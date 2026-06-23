# dbt-debt

dbt-debt finds the dead weight in a dbt project on BigQuery. It tells you which models and columns
nobody is using anymore, which of them are safe to remove, and which tables exist in your warehouse
with no dbt model behind them. 

It does this by comparing your dbt project against two things BigQuery already knows, a log of every
query that has run, and a list of the tables that actually exist. There's no account to make and
nothing to log into. If `dbt run` works on your machine, `dbt-debt scan` works too.

Note: dbt-debt only reports. It never edits or deletes anything.


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

## What the numbers mean

A quick note on words: a **model** is one of your `.sql` files. A **column** is one field inside the
table that model builds. The **lookback window** is how far back we look in BigQuery's query log
(180 days by default).

- **active / unused models.** A model is **unused** if, in the lookback window, nothing queried it
  and nothing queried anything built from it. Everything else is **active**.
- **active / unused columns.** A column is **unused** if no query read it, and nothing read another
  column that was built from it.
- **columns you could remove.** Unused columns that nothing in your dbt project still depends on (no
  data test on them, no contract promising they exist). These are suggestions to look at, not an
  automatic delete. "Unused" comes from the query log, and the log can't see everything (see *What
  counts as usage*). If an unused column still has something depending on it, we list it separately
  and don't count it here.
- **tests you could remove.** Data tests attached to a model or column you'd be removing. If the
  thing goes, the test can go with it.
- **exposures affected.** An **exposure** is something downstream — a dashboard or a report — that
  your team has written down in the dbt project. If we find an unused model that feeds an exposure,
  we flag it as "affected — check before removing", so you don't pull out something that's still
  feeding a dashboard. Exposures whose models are all active aren't listed.
- **tables with no dbt model behind them (orphans).** A real table or view sitting in a dataset dbt
  builds into, but which dbt has no record of. Usually it's left over from a model you renamed or
  deleted, or a table someone made by hand. We only look inside the datasets dbt builds into, so
  your raw input tables are never flagged. We report these; we never delete them.
- **tables your models read but dbt was never told about.** One of your models reads from a table,
  but you never declared that table to dbt. You should add it as a `source()`. We find these by
  reading the model's own SQL, so it needs no extra BigQuery permission and shows up even when we
  can't list what's in the warehouse.
- **top unused models / columns.** The unused models and columns, biggest win first. A whole unused
  table shows how much storage you'd get back, and the savings include a **storage you'd reclaim**
  total. A single column can't be sized — BigQuery doesn't report storage per column — so columns
  are ranked by how big their table is instead.

## Installing it

It's not on PyPI yet, so install it from a copy of this repo:

```
git clone <this repo>
cd dbt-debt
pip install .
```

You need Python 3.10 or newer.

## Using it

```
cd your-dbt-project
dbt docs generate                 # refreshes the column list dbt-debt reads
dbt-debt scan                     # checks models only
dbt-debt scan --columns           # checks models and columns
```

`scan` reads two files dbt writes into your `target/` folder (`manifest.json`, a description of your
project, and `catalog.json`, the list of every column), asks BigQuery what's been used, and tells
you what isn't.

In a normal terminal, `dbt-debt scan` opens a simple tabbed viewer you can click through (Summary /
Detail / JSON / Export). No flags to remember.

When you pipe the output somewhere, run it in CI, or send it to a script, the viewer gets out of the
way and the report just prints:

| What you run | What you get |
|---|---|
| `dbt-debt scan` piped or in CI | a plain summary |
| `dbt-debt scan --detail` | the full list — every unused table, column, and orphan |
| `dbt-debt scan --format json` | JSON (pipe it to `jq`) |
| `dbt-debt scan --format json -o debt.json` | JSON written to a file |
| `dbt-debt scan --no-interactive` | plain text, even in a terminal |
| `dbt-debt scan --orphans` | just the orphan and undeclared-source report |

### Making repeat runs fast (the cache)

The slow part of a scan is talking to BigQuery. So the first scan saves its BigQuery results to a
small file in your computer's temp folder. If you run `dbt-debt scan` (or `scan --columns`) again
soon after, it reads that file instead of asking BigQuery again, and finishes almost instantly.

Saved results count as fresh for *1 hour. After that, the next scan treats them as out of date,
ignores them, and fetches new results from BigQuery (replacing the old file). You can change the
hour with `--cache-ttl <hours>`, or skip the saved results entirely with `--no-cache` when you want
the very latest numbers.

The saved file stays on your computer until something removes it. It does not delete itself
after an hour — the 1-hour limit only decides when the results are too old to trust. The file sits
in your temp folder until one of these happens:

- you run `dbt-debt --clear-cache`, which deletes all of dbt-debt's saved results and does
  nothing else (no scan, no report);
- you run `dbt-debt scan --clear-cache` (or `dbt-debt scan --columns --clear-cache`), which deletes
  this project's saved results and then runs a fresh scan that builds new ones;
- the next scan replaces results that are over an hour old with fresh ones;
- or your operating system clears its temp folder, which it does slowly and unpredictably — Windows
  especially may never do it on its own, so don't count on this.

All of these work the same on Mac, Windows, and Linux. The easiest one to remember is run
`dbt-debt --clear-cache` any time you want a completely clean slate.

## How it works

1. Read `manifest.json` and `catalog.json` from your `target/` folder. (dbt-debt never imports or
   runs dbt — it just reads the files dbt already wrote.)
2. Ask BigQuery which tables real people queried in the lookback window, ignoring dbt's own queries.
   With `--columns`, also read the text of those queries to see which columns they used.
3. Trace where each column came from, using the SQL of your models, so usage flows back up to the
   columns that fed it.
4. Compare what got used against everything in your project, and report what's unused and what's
   safe to remove.
5. Look at the tables that really exist in the datasets dbt builds into, and flag the ones dbt has
   no record of (orphans), plus the tables your models read that you never told dbt about.

### Orphans and undeclared sources, explained

dbt keeps track of two kinds of table; the ones it builds (your models, seeds, and snapshots)
and the ones it reads (the sources you've declared). The orphan check compares both of those
against what's actually sitting in BigQuery, and flags two kinds of mismatch:

- An **orphan** is a table that's really there in BigQuery, in a dataset dbt builds into, but dbt
  has no record of. Usually it's left over from a model you renamed or deleted, or a table someone
  made by hand.
- An **undeclared source** is a table one of your models reads from that you never told dbt about.
  You'd fix it by declaring the table as a `source()`.

Two rules keep these honest. We only look inside the datasets dbt builds into, so your raw input
tables never get flagged. And a table a model reads always counts as an undeclared source, never an
orphan.

## What counts as "usage"

Usage is any `SELECT` that ran against BigQuery in the lookback window and wasn't dbt's own query.
That includes BI tools and dashboards that query BigQuery directly (Looker, Tableau, scheduled
queries). They show up in BigQuery's query log like any other query, so a column read by a dashboard
counts as used.

A few cases to keep in mind:

- **Something that reads the data without hitting BigQuery** — a cached BI extract, a scheduled
  export to another system, a copy living somewhere downstream, won't appear in the query log, so
  it can look unused even though it isn't. You can tell dbt-debt about these by declaring them as
  exposures (see below). A model that looks unused but feeds an exposure is flagged for review
  instead of marked removable.
- **Anything used less often than the lookback window.** The default of 180 days is also the most
  you can look back, because that's all BigQuery keeps in its query log. Setting `--lookback-days`
  higher won't help, since there's no older history to read. So a report that runs once a
  once a year, for example, can look unused, and those tables and columns need a human to make the call. To judge
  yearly usage you'd have to record the query history yourself over time; dbt-debt doesn't do that.
- **`SELECT *`** is handled carefully. Every column of the table counts as used, so a column that's
  only ever read through a `*` is never wrongly called unused.

So "unused" really means "no sign of use in the log". How much you can trust an "unused" verdict
depends on *who* reads the column:

- Columns in the middle of your pipeline are mostly read by other dbt models, and those reads are
  queries that land in BigQuery's log. So if the log shows nothing, that's a strong signal you can
  trust the "unused" verdict.
- Columns at the very end of your pipeline, your final marts, are often read by tools outside
  BigQuery, like a dashboard or an export. Those reads can miss the log (see the cases above), so an
  "unused" verdict there is less certain and is where to use your own judgement before removing
  anything. The best practice is to declare those dashboards and exports as exposures
  (see below), then a model that feeds one is flagged for review instead of called unused.

### Telling dbt-debt about your dashboards (exposures)

dbt-debt doesn't go looking for dashboards on its own. It reads the exposures your team has
already written down. An exposure is a small block in any `.yml` file in your dbt project that names
the models a downstream thing depends on:

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

dbt records these for dbt-debt to read. The more of your real consumers you write down this way, the
fewer things get wrongly called "unused" at the end of your pipeline.

## Permissions

dbt-debt signs in the same way the `gcloud` tool does
(`gcloud auth application-default login`). The scan runs in the project your models live in (read
from your project, or set it yourself with `--project`).

- **Required:** permission to see everyone's queries, not just your own
  (`bigquery.jobs.listAll`, part of the `roles/bigquery.resourceViewer` role). dbt-debt checks for
  this up front and stops with an error if it's missing; otherwise "unused" would quietly mean
  "unused by me".
- **Optional (for finding orphans):** read access to the datasets dbt builds into. Listing the
  tables that physically exist asks each dataset for its own table list, which needs only basic
  read access to that dataset (anyone who can write dbt models already has it) — not the stronger,
  project-wide access that even an Owner can be refused. Without this access, the orphan list is
  skipped with a warning, and the rest of the scan (including undeclared sources) is unaffected.

The permission to see everyone's queries is the only one that's required. Table sizes (used to
rank unused tables) come from `catalog.json`, which `dbt docs generate` already fills in, so they
need no extra BigQuery access.

## Options

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

## Working on dbt-debt

```
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy dbt_debt
```

The tests run on small sample dbt files with a stand-in for BigQuery, so they need no cloud access
and no credentials. For how the code is put together, see [`DESIGN.md`](DESIGN.md).
