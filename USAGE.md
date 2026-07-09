# dbt-debt: usage, internals, and development

For what dbt-debt is and how to get started, see [`README.md`](README.md). For how the code is put
together, see [`DESIGN.md`](DESIGN.md).

## ⚡ Making repeat runs fast (the cache)

The slow part of a scan is talking to the warehouse, so the first scan saves its results to a
small file in your temp folder. Run `dbt-debt scan` (or `dbt-debt scan --columns`) again soon after
and it reads that file instead of re-querying. Results are keyed by warehouse and query
parameters, so BigQuery and Snowflake scans never collide.

Saved results count as fresh for 1 hour; after that the next scan refetches and replaces them. Change
the window with `--cache-ttl <hours>`, or skip saved results with `--no-cache` for the latest
numbers.

The file isn't deleted when it goes stale. The 1-hour limit only decides when results are too old to
trust. It stays in your temp folder until something removes it:

- `dbt-debt --clear-cache` deletes all of dbt-debt's saved results and does nothing else;
- `dbt-debt scan --clear-cache` deletes this project's results, then runs a fresh scan;
- the next scan replaces results over an hour old;
- or your OS clears its temp folder, which is slow and unpredictable (Windows may never do it), so
  don't count on this.

For a clean slate, it's easiest to run `dbt-debt --clear-cache`.

## 🔧 How it works

1. Read `manifest.json` and `catalog.json` from `target/`. (dbt-debt never imports or runs dbt, it
   just reads the files dbt already wrote.) The manifest's `adapter_type` picks the warehouse;
   `--warehouse` overrides it.
2. Ask the warehouse which tables were queried (by people or tools) in the lookback window,
   ignoring dbt's own queries. On BigQuery this reads `INFORMATION_SCHEMA.JOBS`; on Snowflake,
   `ACCOUNT_USAGE.ACCESS_HISTORY`. With `--columns`, also read those queries' text to see which
   columns they used.
3. Trace where each column came from, using your models' SQL, so usage flows back up to the columns
   that fed it.
4. Compare what got used against everything in your project, and report what's unused and safe to
   remove.
5. Look at the tables that really exist in the datasets dbt builds into, and flag the ones dbt has no
   record of (orphans), plus the tables your models read but you never declared.
6. From the dbt files alone, add the hygiene extras: test and docs coverage counts, and (on
   BigQuery) any table of 1 GB or more built with neither `partition_by` nor `cluster_by`. These
   need no warehouse access and no options; the thresholds are explained in the README.

### 🔍 Orphans and undeclared sources, explained

dbt keeps a record of every table it builds and every table it reads. The orphan check compares that
record against the tables actually in the warehouse and flags two kinds of gap:

- An **orphan** is a table really there in the warehouse, in a dataset dbt builds into, but with no
  dbt record. It's usually left over from a renamed or deleted model, or made by hand.
- An **undeclared source** is a table a model reads from that you never told dbt about, so it sits
  outside the DAG and is never tracked or tested. Fix it by declaring it in a `sources.yml` file
  and referencing it with `{{ source() }}`.

Two rules. We only look inside the datasets dbt builds into so raw input tables
are never flagged, and a table a model reads always counts as undeclared, never as an orphan.

## 🎯 What counts as "usage"

Usage is any `SELECT` that ran against the warehouse in the lookback window and wasn't dbt's own
query. That includes BI tools and dashboards that query the warehouse directly (Looker, Tableau,
scheduled queries), which land in the query log like anything else.

A few cases to keep in mind:

- **Reads that don't hit the warehouse.** A cached BI extract, a scheduled export, or a copy
  downstream never appears in the query log, so it can look unused. Tell dbt-debt about them by
  declaring exposures (see below); a model that feeds one is flagged for review instead of marked
  removable.
- **Anything used less often than the lookback window.** On BigQuery the default 180 days is also
  the max, since that's all it keeps; Snowflake keeps a year, so `--lookback-days` can go to 365
  there. A report that runs once a year can still look unused, so those need a human call.
- **Anything created recently.** A table that first appeared in the query log fewer than 7 days ago
  (`--min-age-days`) hasn't had a fair chance to be queried, so it's reported as "too new to judge"
  instead of unused, and left out of the removable-test and reclaimable-storage figures.
- **Semantic-layer declarations.** Models feeding a semantic model, metric, or saved query are
  flagged for review when unused (like exposures), and columns a semantic model names are marked
  blocked, never removable.
- **`SELECT *`** is handled carefully: every column counts as used, so a column read only through a
  `*` is never wrongly called unused.

So "unused" means "no sign of use in the log." How far to trust it depends on who reads the column:

- Columns mid-pipeline are mostly read by other dbt models, whose reads land in the log. Nothing in
  the log is a strong "unused" signal you can trust.
- Columns at the end, your final marts, are often read by tools outside the warehouse, like a
  dashboard or export, whose reads can miss the log. An "unused" verdict there is less certain; use
  judgement. Best practice is to declare those consumers as exposures so a model feeding one is
  flagged for review instead.

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

## 🔐 Permissions and signing in

### BigQuery

dbt-debt signs in the same way `gcloud` does (`gcloud auth application-default login`) and runs in
the project your models live in (read from your project, or set with `--project`).

- **Required:** permission to see everyone's queries, not just your own (`bigquery.jobs.listAll`,
  part of `roles/bigquery.resourceViewer`). dbt-debt checks for this up front and stops if it's
  missing; otherwise "unused" would mean "unused by me".
- **Optional (for orphans):** read access to the datasets dbt builds into. Listing the tables that
  physically exist asks each dataset for its own table list, basic read access anyone who writes dbt
  models already has, rather than the project-wide access even an Owner can be refused. Without it,
  the orphan list is skipped with a warning and the rest of the scan is unaffected.

That required grant is the only one. Table sizes (used to rank unused tables) come from
`catalog.json`, which `dbt docs generate` already fills in, so they need no extra access.

### Snowflake

Install the optional connector (`pip install 'dbt-debt[snowflake]'`) and define a connection the
connector can find, either in `~/.snowflake/connections.toml` or as `SNOWFLAKE_*` environment
variables. Pass `--connection NAME` if it isn't the default one.

**Signing in.** Any sign-in method the Snowflake connector supports will work. In practice a key
pair is the reliable choice, because new Snowflake accounts require MFA on password logins, and
browser sign-in (`externalbrowser`) fails unless the account has an identity provider set up. The
key-pair setup is done once:

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
   account = "myorg-myaccount"        # from your account URL, before .snowflakecomputing.com
   user = "MY_USER"
   authenticator = "SNOWFLAKE_JWT"
   private_key_file = "/Users/me/.snowflake/snowflake_key.p8"
   role = "MY_ROLE"
   warehouse = "MY_WH"
   database = "MY_DB"
   ```

   dbt itself can use the same key: set `private_key_path` (instead of `password`) in your
   `profiles.yml`.

**What the scan needs:**

- **Required:** IMPORTED PRIVILEGES on the `SNOWFLAKE` database (so `ACCOUNT_USAGE` is readable)
  and Enterprise edition (`ACCESS_HISTORY` is an Enterprise view). dbt-debt checks up front and
  stops if either is missing. Usage deliberately comes from ACCESS_HISTORY's access metadata and
  never from parsing query text, so an unparseable query can never erase evidence of use.
- **Optional (for orphans):** USAGE on the database and its managed schemas, to read
  `INFORMATION_SCHEMA.TABLES`. Missing access skips the orphan list with a warning, as on
  BigQuery.

**Two timing notes.** `ACCOUNT_USAGE` views lag reality; Snowflake documents up to ~45 minutes,
and in practice it is often around 20. The table list behind the "too new" guard
(`ACCOUNT_USAGE.TABLES`) lags further, so a table only minutes old can briefly be judged before
its age is known. Scan again later, or with `--no-cache`, and it settles.

## ⚙️ Options

```
dbt-debt scan
    --project-dir .           your dbt project folder (default: current folder)
    --target-path target      where manifest.json and catalog.json live
    --warehouse <name>        bigquery or snowflake (default: read from the manifest)
    --project <id>            which database to query: the GCP project on BigQuery, the
                              database on Snowflake (default: read from your models)
    --region US               which BigQuery region your query log is in (BigQuery only)
    --connection <name>       named Snowflake connection from connections.toml (Snowflake only)
    --lookback-days 180       how far back to look (max 180 on BigQuery, 365 on Snowflake)
    --query-comment-pattern   how to recognise dbt's own queries (a regex)
    --columns                 also check which columns are unused (default: models only)
    --min-age-days 7          tables first seen in the query log more recently than this are
                              "too new to judge", not unused (0 disables the guard)
    --rare-threshold 5        models queried at most this many times are "rarely used"
                              (0 disables the band)
    --top-n 10                how many unused assets the summary list shows
    --detail                  list every unused table and column (grouped by model, with file paths)
    --format text|json        json always includes the full list
    -o, --output <file>       write the report to a file instead of the screen
    --no-interactive          print the report instead of opening the viewer
    --orphans                 print only the orphan and undeclared-source report
    --no-cache                ask the warehouse directly, ignoring (and not writing) saved results
    --cache-ttl 1             how many hours saved results stay fresh before being re-fetched
    --clear-cache             clear this project's saved results, then run a fresh scan
```

Exit codes: `0` the scan completed (including degraded scans, say orphans skipped for lack of
access); `2` a local problem (a missing or malformed manifest/catalog, an invalid option, an
unsupported adapter, an unwritable output path); `3` a warehouse problem (not signed in, missing
the required permission, a missing optional connector, or any warehouse error mid-scan); `130`
interrupted with Ctrl-C.

## 🛠️ Working on dbt-debt

```
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy dbt_debt
```

The tests run on small sample dbt files with a stand-in for the warehouse, so they need no cloud
access and no credentials. For how the code is put together, see [`DESIGN.md`](DESIGN.md).
