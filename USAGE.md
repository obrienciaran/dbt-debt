# dbt-debt: usage, internals, and development

For what dbt-debt is and how to get started, see [`README.md`](README.md). For how the code is
put together, see [`DESIGN.md`](DESIGN.md).

## 🔧 How it works

1. Read `manifest.json` and `catalog.json` from `target/`. dbt-debt never imports or runs dbt;
   it only reads files dbt already wrote. The manifest's `adapter_type` picks the warehouse;
   `--warehouse` overrides it.
2. Ask the warehouse which tables were queried in the lookback window, ignoring dbt's own
   queries. On BigQuery this reads `INFORMATION_SCHEMA.JOBS`; on Snowflake,
   `ACCOUNT_USAGE.ACCESS_HISTORY`; on Redshift, `SYS_QUERY_HISTORY` joined to the scan steps
   in `SYS_QUERY_DETAIL`. The same rows say how many bytes each query scanned, which ranks
   the review lists by what queries actually cost. With `--columns`, also read the query
   text to see which columns were used.
3. Trace each column back through your models' SQL, so usage flows up to the columns that fed
   it.
4. Compare what was used against everything in the project, and report what's unused and safe
   to remove.
5. List the tables that actually exist in the datasets dbt builds into, and flag orphans,
   undeclared sources, and declared sources nothing in the project reads.
6. Check when each declared source's table last received data, from warehouse metadata. No new
   data for more than `--stale-source-days` (default 30) flags it stale: the loader upstream of
   dbt has likely stopped.
7. Add the hygiene extras from the dbt files alone (no warehouse access needed): test and docs
   coverage, documentation drift (YAML columns that no longer exist in the built table),
   likely-dead exposures, and, on BigQuery, tables of 1 GB or more with neither `partition_by`
   nor `cluster_by`. On Redshift one hygiene check does read the warehouse (a single
   `SVV_TABLE_INFO` select): large tables whose VACUUM/ANALYZE maintenance has fallen behind.
   The thresholds are explained in the README.

### 🔍 Orphans and sources, explained

dbt keeps a record of every table it builds and every table it reads. Comparing that record
against the warehouse and against your own project flags four kinds of gap:

- An **orphan** exists in a dataset dbt builds into, but dbt has no record of it. Usually a
  leftover from a renamed or deleted model, or a table made by hand. The report shows any
  queries people ran against it directly; a queried orphan is still in use and dangerous to
  drop, so those are listed first.
- An **undeclared source** is a table a model reads that dbt was never told about, so it sits
  outside the DAG, untracked and untested. Declare it in a `sources.yml` and reference it with
  `{{ source() }}`.
- An **unused declared source** is the reverse: a `sources.yml` entry no model, exposure, or
  semantic-layer consumer references. The report shows any queries people ran against the raw
  table directly, so you can tell a dead declaration (delete the entry) from a table your team
  reads outside dbt (consider modelling it).
- A **stale source** is a declared source with no new data for more than `--stale-source-days`
  days (default 30; `0` turns the check off). The last-data date comes from warehouse metadata,
  never the query log; a source whose metadata can't be read is skipped, not guessed at.

Two rules keep the orphan counts clean:

1. **Where we look.** Only datasets dbt builds into are searched. Datasets that just hold raw
   data loaded by something else (Fivetran, Airbyte, a manual load) never are, so raw input
   tables are never flagged.
2. **How we classify.** An unrecognized table within a dbt project
   is an undeclared source if a dbt model queries it, or an orphan if nothing does.

## 🎯 What counts as "usage"

Usage is any `SELECT` that ran in the lookback window and wasn't dbt's own query. That includes
BI tools and dashboards that query the warehouse directly (Looker, Tableau, scheduled queries).

A few cases to keep in mind:

- **Reads that don't hit the warehouse.** A cached BI extract, a scheduled export, or a
  downstream copy never appears in the query log, so it can look unused. Declare those
  consumers as exposures (see below); a model feeding one is flagged for review instead of
  marked removable.
- **Anything used less often than the lookback window.** BigQuery keeps 180 days of history
  (the default and the max); Snowflake keeps a year, so `--lookback-days` can go to 365 there.
  Redshift's SYS views keep much less (AWS leaves the retention unstated; the older STL views
  keep seven days), so on Redshift the window is capped by whatever the account retains and
  "unused" is a correspondingly weaker signal. A report that runs once a year can still look
  unused; that needs a human call.
- **Anything created recently.** A table first seen in the query log fewer than 7 days ago
  (`--min-age-days`) hasn't had a fair chance to be queried, so it's reported as "too new to
  judge" and left out of the removable-test and reclaimable-storage figures. On Snowflake, a
  table with no first-seen date at all gets the same treatment ("missing a first-seen date,
  likely a new table"), because `ACCOUNT_USAGE.TABLES` lags about 90 minutes behind reality.
- **Semantic-layer declarations.** Models feeding a semantic model, metric, or saved query are
  flagged for review when unused (like exposures), and columns a semantic model names are never
  counted as removable. Each affected consumer is reported with its cause (the unused model it
  is built on, or the affected consumer it reads through) in the summary and in a detail
  section of its own.
- **`SELECT *`** counts every column as used, so a column read only through a `*` is never
  wrongly called unused.

So "unused" means "no sign of use in the log". How far to trust it depends on who reads the
column:

- Columns mid-pipeline are read by other dbt models, whose reads land in the log. For them, an
  "unused" verdict is strong.
- Columns in your final marts are often read by tools outside the warehouse, whose reads can
  miss the log. An "unused" verdict there is less certain; use judgement, and declare those
  consumers as exposures so the models feeding them are flagged for review instead.

### 📣 Telling dbt-debt about your dashboards (exposures)

dbt-debt doesn't hunt for dashboards; it reads the exposures your team has already written
down. An exposure is a small block in any `.yml` file naming the models a downstream thing
depends on:

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

The more real consumers you write down this way, the fewer things get wrongly called "unused"
at the end of your pipeline.

## 🔐 Permissions and signing in

### BigQuery

dbt-debt signs in the same way `gcloud` does (`gcloud auth application-default login`) and runs
in the project your models live in (read from your project, or set with `--project`).

- **Required.** Permission to see everyone's queries, not just your own
  (`bigquery.jobs.listAll`, part of `roles/bigquery.resourceViewer`). dbt-debt checks this up
  front and stops if it's missing; otherwise "unused" would mean "unused by me".
- **Optional, for orphans.** Read access to the datasets dbt builds into, the basic access
  anyone who writes dbt models already has. Without it, the orphan list is skipped with a
  warning and the rest of the scan is unaffected.
- **Optional, for stale sources.** Read access to the datasets your sources live in, where the
  last-data date is read from dataset metadata. Without it, the stale-source check is skipped
  with a warning.

That required grant is the only one. On BigQuery, table sizes (used to rank unused tables)
come from `catalog.json`, which `dbt docs generate` already fills in, so they need no extra
access.

### Snowflake

Install the optional connector (`pip install 'dbt-debt[snowflake]'`) and define a connection
the connector can find, either in `~/.snowflake/connections.toml` or as `SNOWFLAKE_*`
environment variables. Pass `--connection NAME` if it isn't the default one.

**Signing in.** Any sign-in method the Snowflake connector supports will work. In practice a
key pair is the reliable choice: new Snowflake accounts require MFA on password logins, and
browser sign-in (`externalbrowser`) fails without an identity provider set up. The key-pair
setup is done once:

1. Make a key pair on your machine:

   ```
   mkdir -p ~/.snowflake && cd ~/.snowflake
   openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out snowflake_key.p8 -nocrypt
   openssl rsa -in snowflake_key.p8 -pubout -out snowflake_key.pub
   chmod 600 snowflake_key.p8
   ```

2. Register the public key on your Snowflake user. In a Snowsight worksheet, paste the contents
   of `snowflake_key.pub` as one long line:

   ```sql
   ALTER USER MY_USER SET RSA_PUBLIC_KEY='MIIB...';
   ```

3. Point your connection at the private key in `~/.snowflake/connections.toml`:

   ```toml
   [default]
   account = "myorg-myaccount"
   user = "MY_USER"
   authenticator = "SNOWFLAKE_JWT"
   private_key_file = "/Users/me/.snowflake/snowflake_key.p8"
   role = "MY_ROLE"
   warehouse = "MY_WH"
   database = "MY_DB"
   ```

   dbt itself can use the same key by setting `private_key_path` (instead of `password`) in
   your `profiles.yml`.

**What the scan needs:**

- **Required.** IMPORTED PRIVILEGES on the `SNOWFLAKE` database (so `ACCOUNT_USAGE` is
  readable) and Enterprise edition (`ACCESS_HISTORY` is an Enterprise view). dbt-debt checks up
  front and stops if either is missing. Usage comes from ACCESS_HISTORY's access metadata and
  never from parsing query text, so an unparseable query can never erase evidence of use.
- **Optional, for orphans.** USAGE on the database and its managed schemas, to read
  `INFORMATION_SCHEMA.TABLES`. Missing access skips the orphan list with a warning, as on
  BigQuery.

The stale-source check needs no extra grant on Snowflake: it reads `ACCOUNT_USAGE.TABLES`,
already required for the "too new" guard. One caveat: its `last_altered` date also moves on DDL
changes (even a table comment), so a stale table can occasionally look fresher than its data.

Table sizes need no extra grant either: they come live from
`ACCOUNT_USAGE.TABLE_STORAGE_METRICS`, so no `dbt docs generate` is needed for them. Each
unused table also shows the time-travel and fail-safe copies Snowflake still bills for it, as
bytes rather than dollars, since storage rates vary by contract. dbt builds transient
tables by default, which keep no fail-safe copies, so those figures are often zero.

**Two timing notes.** `ACCOUNT_USAGE` views lag reality; Snowflake documents 90 minutes for
`TABLES` and 3 hours for `ACCESS_HISTORY` (both approximate; in practice often much less).
Because the table list behind the "too new" guard can lag, a dead table with no first-seen date
yet is set aside as "missing a first-seen date (likely a new table)" rather than judged. Scan
again later, or with `--no-cache`, and it settles into a normal verdict.

### Redshift

Install the optional connector (`pip install 'dbt-debt[redshift]'`) and set the connection as
environment variables, since there is no connections file:

```
export REDSHIFT_HOST=<workgroup or cluster endpoint>
export REDSHIFT_USER=<user>
export REDSHIFT_PASSWORD=<password>
```

`REDSHIFT_DATABASE` and `REDSHIFT_PORT` are optional; the database defaults to the one your
models live in and the port to 5439. Serverless workgroups and provisioned clusters both work:
the SYS system views the scan reads cover both.

**What the scan needs:**

- **Required.** A user who can see *everyone's* queries in the SYS query-history views: a
  superuser (on Serverless, the namespace admin) or a user granted
  `ALTER USER ... SYSLOG ACCESS UNRESTRICTED`. Redshift lets any user select from those views
  but silently filters them to the user's own rows, so dbt-debt checks up front and stops
  rather than let "unused" mean "unused by me". Usage comes from the scan-step metadata in
  `SYS_QUERY_DETAIL` and never from parsing query text, so an unparseable query can never
  erase evidence of use.
- **Optional, for orphans.** USAGE on the managed schemas, to read `SVV_REDSHIFT_TABLES`.
  Missing access skips the orphan list with a warning, as on the other warehouses.

Table sizes come live from `SVV_TABLE_INFO` (1 MB blocks, reported as bytes), so no
`dbt docs generate` is needed for them; an empty table has no SVV_TABLE_INFO row and keeps its
catalog size. Redshift has no time-travel or fail-safe storage, so there is no retained-bytes
breakdown, and it exposes no table last-modified metadata, so the stale-source check is
skipped with a note. One retention caveat: the SYS views keep a bounded history (AWS leaves
the exact figure unstated; the older STL views keep seven days), which caps both the
effective lookback window and how far back a first-seen date can reach.

The same `SVV_TABLE_INFO` read feeds a Redshift-only table-hygiene check: a table of 1 GB or
more is flagged when its unsorted region reaches 20% (needs VACUUM), its `stats_off` reaches
10 (stale planner statistics; needs ANALYZE), or its slice-skew ratio reaches 4 (worth a
distribution-key review), listed most-scanned-bytes first so the top entry is the fix that
saves the most. Automatic vacuum and analyze (always on for Serverless and the default on
provisioned clusters) usually keep every figure near zero, so the section simply not
appearing is the healthy state.

## ⚙️ Options

```
dbt-debt scan
    --project-dir .           your dbt project folder (default: current folder)
    --target-path target      where manifest.json and catalog.json live
    --warehouse <name>        bigquery, snowflake, or redshift (default: read from the manifest)
    --project <id>            which database to query: the GCP project on BigQuery, the
                              database on Snowflake or Redshift (default: read from your models)
    --region US               which BigQuery region your query log is in (BigQuery only)
    --connection <name>       named Snowflake connection from connections.toml (Snowflake only;
                              Redshift connects from REDSHIFT_* environment variables)
    --lookback-days 180       how far back to look (max 180 on BigQuery, 365 on Snowflake;
                              capped by SYS-view retention on Redshift)
    --query-comment-pattern   how to recognise dbt's own queries (a regex)
    --columns                 also check which columns are unused (default: models only)
    --min-age-days 7          tables first seen in the query log more recently than this are
                              "too new to judge", not unused (0 disables the guard)
    --rare-threshold 5        models queried at most this many times are "rarely used"
                              (0 disables the band)
    --stale-source-days 30    declared sources with no new data for more than this many days
                              are stale (0 disables the check)
    --top-n 10                how many unused assets the summary list shows
    --print                   print the full report instead of opening the viewer (every unused
                              table and column, grouped by model, with file paths)
    --format text|json        json always includes the full list
    -o, --output <file>       write the report to a file instead of the screen
    --orphans                 print only the orphan and undeclared-source report
    --no-cache                ask the warehouse directly, ignoring (and not writing) saved results
    --cache-ttl 1             how many hours saved results stay fresh before being re-fetched;
                              remembered per entry, so it survives closing the terminal
                              (passing the flag again overrides the remembered value)
    --clear-cache             clear this project's saved results, then run a fresh scan
```

Exit codes: `0` the scan completed (including degraded scans, say orphans skipped for lack of
access); `2` a local problem (a missing or malformed manifest/catalog, an invalid option, an
unsupported adapter, an unwritable output path); `3` a warehouse problem (not signed in, missing
the required permission, a missing optional connector, or any warehouse error mid-scan); `130`
interrupted with Ctrl-C.

## ⚡ Making repeat runs fast (the cache)

The slow part of a scan is talking to the warehouse, so the first scan saves its results to a
small file in your temp folder. Scan again soon after and it reads that file instead of
re-querying. Results are keyed by warehouse and query parameters, so different warehouses'
scans never collide.

Saved results count as fresh for 1 hour; after that the next scan refetches and replaces them.
Change the window with `--cache-ttl <hours>`, or skip saved results with `--no-cache` for the
latest numbers.

The 1-hour limit only decides when results are too old to trust; the file itself stays in your
temp folder until something removes it:

- `dbt-debt --clear-cache` deletes all of dbt-debt's saved results and does nothing else;
- `dbt-debt scan --clear-cache` deletes this project's results, then runs a fresh scan;
- the next scan replaces results over an hour old;
- or your OS clears its temp folder, which is slow and unpredictable (Windows may never do it), so
  don't count on it.

For a clean slate, run `dbt-debt --clear-cache`.

## 🛠️ Working on dbt-debt

```
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy dbt_debt
```

The tests run on small sample dbt files with a stand-in for the warehouse, so they need no
cloud access and no credentials. For how the code is put together, see [`DESIGN.md`](DESIGN.md).
