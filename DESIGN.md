# Design

This doc explains how `dbt-debt` is built. For how to use it, see [`README.md`](README.md).
The tool covers BigQuery, Snowflake, and Redshift, runs as a standalone Python command-line
program, and works at both the model and the column level. All three warehouses are validated
against live data. The Snowflake and Redshift sections list what is confirmed and what
remains open.

Two words come up throughout. A **model** is one of your `.sql` files. A **relation** is the
actual table or view that model builds in the warehouse.

## How the code is organised

The code is split into layers, and a layer never reaches into one it shouldn't. The most
important layer is `verdict/`. It only does the working-out, on data that has already been
loaded for it. It never talks to a warehouse and never reads files itself. That is what lets us
test the tricky logic with small hand-written examples and no cloud access at all.

### What lives where

```
dbt_debt/
  cli.py                 # reads the arguments, wires the pieces together, prints the report
  config.py              # the settings for a run (paths, project, region, lookback, columns, format)
  domain.py              # the data classes everything passes around (Model, Column, Test, ...)
  sqlparse.py            # the SQL-reading helpers (which columns a query reads, and where they came from)
  references.py          # which warehouse tables each model reads, used for finding orphans

  artifacts/             # read dbt's own files into our data classes (plain JSON; dbt is never imported)
    manifest.py          #   manifest.json -> models/seeds/snapshots, tests, sources, exposures, semantic layer
    catalog.py           #   catalog.json -> the full column list and table sizes
    graph.py             #   the map of which buildable node depends on which
    errors.py            #   ArtifactError, so a broken artifact fails with the path and no traceback

  consumption/           # ask the warehouse what was actually used
    client.py            # WarehouseClient, the shared interface the rest of the code talks to
                         #   (so it can be faked in tests); one implementation per warehouse
    bigquery.py          # the BigQuery version (the only file that imports the BigQuery library)
    snowflake.py         # the Snowflake version (the only file that imports the connector,
                         #   which is an optional extra; see the Snowflake section)
    redshift.py          # the Redshift version (the only file that imports redshift-connector,
                         #   also an optional extra; see the Redshift section)
    cache.py             # an optional saved-results layer that wraps any client (same interface)
    jobs.py              # the BigQuery queries (query log, table list, first-seen) and the
                         #   warehouse-neutral row parsers both clients feed
    snowflake_queries.py # the Snowflake ACCOUNT_USAGE / INFORMATION_SCHEMA queries
    redshift_queries.py  # the Redshift SYS / SVV queries
    exclusion.py         # the filter that throws out dbt's own queries
    usage.py             # turn "these tables were used" into "these models were used"
    columns.py           # turn query text into the (model, column) pairs that were actually read

  lineage/               # which column feeds which, from one model to the next
    base.py              # the shared interface a lineage source has to provide
    sqlglot_source.py    # the default; reads each model's SQL and traces its columns back upstream

  verdict/               # working-out only; given the data, decide what's unused
    models.py            # a node is unused if it, and everything built from it, went unqueried
    columns.py           # a column is unused if it's not read and feeds nothing that's read
    orphans.py           # an orphan is a table in a dbt dataset with no dbt model behind it
    sources.py           # a declared source is unused if nothing in the project reads it
    staleness.py         # a declared source is stale if its table stopped receiving data
    drift.py             # a YAML column missing from the built table is stale documentation
    freshness.py         # the too-new guard (first seen recently means "too new to judge")
    rarity.py            # the rarely-used band (queried, but at most --rare-threshold times)
    coverage.py          # test and docs coverage counts
    partitioning.py      # large BigQuery tables declaring neither partition_by nor cluster_by
    redshift_hygiene.py  # large Redshift tables whose VACUUM/ANALYZE maintenance fell behind
    semantic.py          # which semantic models / metrics / saved queries a dead model feeds
    tests.py / exposures.py / blockers.py   # checks that only need dbt's own files

  report/
    scorecard.py         # put the result together
    render_text.py / render_json.py
    viewer.py            # the interactive tabbed viewer (Summary / Detail / JSON / Export / Help), stdlib-only
    spinner.py           # a "working..." spinner shown only while the slow warehouse steps run
```

### How the data moves through it

```
settings ─┐
          ├─> artifacts: load manifest.json + catalog.json -> data classes, dependency map, table sizes
          ├─> consumption: the warehouse
          │     • which tables were queried (in the window, dbt's own queries removed)
          │     • the query text (only when checking columns)
          │     • the list of tables in the dbt-managed datasets (only when finding orphans)
          │     • each table's live storage bytes (Snowflake and Redshift; on BigQuery sizes come from catalog.json)
          │     • each table's maintenance state (Redshift only, for the table-hygiene check)
          │     • when each source table last received data (only for the stale-source check)
          ├─> lineage (column check): which column feeds which
          ├─> references: which tables each model reads
          └─> decide:
                models  = used tables, spread back up the dependency map
                columns = columns read in queries, spread back along the lineage
                orphans = tables in a managed dataset with no dbt model, that no model reads
                sources = tables a model reads that dbt doesn't know about (undeclared),
                          and declared sources that nothing in the project reads (unused)
                tests / exposures / blockers = from dbt's own files
                                                          -> the report -> text or JSON
```

Usage spreads **upstream**. A model counts as used if it, or anything downstream of it, was
queried. Columns work the same way, spreading back along their lineage.

## Where the BigQuery data comes from

BigQuery logs which tables a query touched, and does not log which columns. So table usage is
read straight from the log, and column usage has to be worked out by reading the text of each
query.

| What we need | Where we get it |
|------|--------|
| Which **tables** a query touched | BigQuery's query log lists them directly |
| Which **columns** a query touched | read the query text and parse it with `sqlglot` |
| Which **tables exist** in a dataset | ask each dbt-managed dataset for its own table list |
| The full column list and table sizes | `catalog.json` (from `dbt docs generate`) |

BigQuery's query log only covers the project you query it in, and keeps roughly 180 days of
history. So the scan runs in the project your models live in (read from your project, or set
with `--project`). Before scanning, the tool tries to list everyone's queries. If BigQuery
refuses, it stops and says the `bigquery.jobs.listAll` permission is missing. Without that
permission, "unused" would quietly mean "unused by me".

dbt's own queries are left out by spotting the marker dbt stamps on every query it runs
(`"app": "dbt"`). This matters because dbt's data tests are themselves `SELECT`s, and without
the filter they would look like real usage.

## Snowflake

The warehouse sits behind one Protocol (`consumption/client.py`'s `WarehouseClient`), whose
methods return parsed domain values (`UsageRow`s, `WarehouseRelation`s, first-seen dates, query
texts). Everything inward of those values (verdict, report, cache, artifacts) never touches a
warehouse. Adding a warehouse means one pure query-builder module, one SDK-touching client
module, and a sqlglot dialect threaded through the SQL parsing. The warehouse is picked from
the manifest's `adapter_type`, and `--warehouse` overrides it. Each client imports its SDK
lazily, so a BigQuery user never imports or installs anything Snowflake-related, and vice
versa. The Snowflake connector is the `[snowflake]` optional extra.

The adapter is built from Snowflake's published documentation, pinned by tests, and validated
against a live Enterprise account (`demo_snowflake/`, the same medallion project as `demo_bq/`).
Confirmed against live data so far: the ACCESS_HISTORY flatten and QUERY_HISTORY join return
exact per-relation query counts, the dbt query-comment exclusion holds (builds do not count as
use), orphan discovery finds hand-made tables through the case normalization, the
first-seen/too-new guard behaves as designed (see the first-seen bullet), and the scan exits 0
end to end. The missing reclaimable-bytes figures from that first run are explained and fixed:
dbt-snowflake writes the table size under the `bytes` stats key ("Approximate Size"), not
BigQuery's `num_bytes`. The catalog reader checks both (views carry no stats and report 0),
confirmed live 2026-07-10 with reclaimable-storage figures appearing on a demo scan.

The design decisions, and what remains to confirm:

- **Usage comes from `ACCOUNT_USAGE.ACCESS_HISTORY`** (`direct_objects_accessed`, flattened to
  one row per relation a query touched, the analogue of BigQuery's `referenced_tables`), joined
  to `QUERY_HISTORY` for the SELECT/success/window/dbt-exclusion filters. We deliberately avoid
  falling back to sqlglot-parsing `query_text` for usage. A silently unparseable query would
  erase evidence of use and produce false "unused" verdicts, the one failure mode this tool
  must never have. ACCESS_HISTORY needs Enterprise edition and IMPORTED PRIVILEGES on the
  `SNOWFLAKE` database. On Standard edition the preflight stops the scan, mirroring the
  `jobs.listAll` stance.
- **First-seen comes from `ACCOUNT_USAGE.TABLES` including dropped incarnations**, taking
  `MIN(created)` over all rows for a name and counting rows whose `deleted` is set, so dbt's
  `CREATE OR REPLACE` rebuilds don't reset the age. Same reasoning as BigQuery, where first-seen
  comes from JOBS rather than TABLES. *Confirmed live:* dropped incarnations are retained. A
  demo mart rebuilt via `CREATE OR REPLACE` and then dropped outright and rebuilt keeps one
  `TABLES` row per incarnation (the replaced and dropped ones with `deleted` set), and
  `first_seen_query()` returns the original creation date throughout; a default scan then
  files the mart under too-new with that original date, not under missing-first-seen.
  Retention is not indefinite: Snowflake keeps ACCOUNT_USAGE history for 365 days, so a
  relation's oldest incarnations age out after a year. Harmless for the guard, because any surviving
  row still dates the relation far past `--min-age-days`, and a relation with no rows at all
  no longer exists. *Confirmed live too:* the guard itself. A brand-new dead model is
  set aside as too-new at the default `--min-age-days`, its test leaves the removable count, and
  the rarely-used band empties, once `ACCOUNT_USAGE.TABLES` has a row for it. *Decided
  2026-07-10:* `ACCOUNT_USAGE.TABLES` lags reality (documented 90 minutes), so on Snowflake a
  dead node with no first-seen row cannot prove its age and is set aside as "missing a
  first-seen date (likely a new table)", a review list beside too-new, excluded from every
  unused-derived figure, and the rare band gets the same protection. BigQuery is untouched:
  there first-seen comes from JOBS, so a missing row means zero jobs all window, the strongest
  unused signal there is.
- **The dbt exclusion assumes dbt's query-comment lands in `query_text`** (it does on BigQuery).
  The pattern sits in a `$$...$$` dollar-quoted string (Snowflake's no-escape literal) inside
  `REGEXP_COUNT(...) = 0`, because Snowflake's `REGEXP_LIKE` anchors to the whole string.
  *Confirmed live:* dbt's builds are correctly excluded from usage counts.
- **Table sizes come live from `ACCOUNT_USAGE.TABLE_STORAGE_METRICS`** (same grant as the rest
  of ACCOUNT_USAGE, so no new permission), replacing the catalog sizes where a row exists,
  giving warehouse truth with no `dbt docs generate` needed. Each relation's active bytes drive the
  size and reclaimable figures; the time-travel and fail-safe bytes still billed for it are
  summed over every incarnation, dropped ones included, and shown next to each unused table.
  Reported as bytes rather than dollars, since storage rates vary by contract and region.
  Validated live: the
  per-relation sums match the account, and two caveats surfaced. dbt builds transient tables
  on Snowflake by default, which keep no fail-safe copies, so those figures are often zero;
  and the view lags like the rest of ACCOUNT_USAGE, so a brand-new table falls back to its
  catalog size until a row appears.
- **Orphans** read one `<database>.INFORMATION_SCHEMA.TABLES` filtered by lowercased schema name.
  This is one query, unlike BigQuery's per-dataset union, because Snowflake's information schema
  spans the database. Snowflake's uppercase identifiers normalize away because every relation
  key is lowercased on both sides.
- ACCOUNT_USAGE lags reality (documented: 90 minutes for TABLES, 3 hours for ACCESS_HISTORY,
  both approximate and often much less in practice). Harmless for a debt scan; the
  missing-first-seen set-aside above absorbs the TABLES gap.
- **DuckDB is deliberately unsupported.** It keeps no query history at all, so the core "unused"
  verdict has no signal to stand on, and its enterprise footprint among dbt users is small.

## Redshift

Built to the same recipe as Snowflake, with one pure query-builder module
(`consumption/redshift_queries.py`), one lazy SDK client (`consumption/redshift.py`, the
`[redshift]` optional extra, `redshift-connector`), and the sqlglot `redshift` dialect. The
SYS system views the scan reads work on both Serverless workgroups and provisioned clusters.
The adapter is built from AWS's published schemas, pinned by tests, and its core loop is
validated live against a Serverless workgroup via `demo_redshift/` (the same medallion
project): the preflight as the namespace admin, per-relation usage counts matching planted
queries, dbt-build exclusion, the orphan with direct-query evidence and scanned bytes, the
too-new guard on dbt-built relations, the result-cache join, SVV_TABLE_INFO sizes on the
scorecard, the stale-source skip note (fired once the demo declared its `raw.raw_events`
source, which itself lands under unused declared sources with its one direct query and
scanned bytes), and the table-hygiene check (ran and returned empty, rendering nothing,
the healthy state on Serverless), exit 0 end to end.

The design decisions, and what the live scans showed:

- **Usage comes from `SYS_QUERY_DETAIL` scan steps joined to `SYS_QUERY_HISTORY`.**
  SYS_QUERY_HISTORY alone has no per-table column, but each scan step in SYS_QUERY_DETAIL
  names the relation it read as a fully qualified `database.schema.table`, the engine-metadata
  analogue of `referenced_tables` and ACCESS_HISTORY, so usage never comes from parsing query
  text. The optimizer's own temp tables (`volt_tt_*`) and the system schemas are filtered out.
  *Confirmed live:* the three-part `table_name` shape, and per-relation counts matching the
  planted worksheet queries exactly.
- **Result-cache hits are followed to the originating query.** A cache-hit SELECT runs no scan
  steps, so counting scan steps alone would miss it, the dangerous direction, use looking like
  disuse. `SYS_QUERY_HISTORY.result_cache_query_id` names the originating query, so the usage
  join goes through `COALESCE(NULLIF(result_cache_query_id, 0), query_id)` and a cached repeat
  counts as use of the tables the original query read. *Confirmed live:* an identical repeat
  came back with `result_cache_hit` true and its source's query id, and both runs counted.
- **The preflight checks visibility, not access.** Every user can select from the SYS views;
  Redshift silently filters them to the user's own rows unless they are a superuser or hold
  `SYSLOG ACCESS UNRESTRICTED`. An access-error probe like the other warehouses' would
  therefore pass wrongly, so the probe instead queries `SVV_USER_INFO` for the current user's
  own visibility and treats an empty result as the missing permission. *Confirmed live* as a
  superuser (the column names hold); the regular-user empty-result path is pinned by tests
  only.
- **dbt builds surface under `__dbt_tmp` names, folded back in first-seen.** dbt-redshift
  builds each table as `<name>__dbt_tmp` and renames it into place, and the rename is DDL
  that SYS_QUERY_DETAIL records under no name (found live), so the first-seen query strips
  that suffix, letting the tmp incarnation date the final relation. Without it, dbt-built
  tables get no first-seen row and the too-new guard never protects them. One residual edge,
  also found live: a CTAS whose plan scans no base table (the demo's recursive-CTE time
  spine) is logged as plain `DDL` with *no* named steps at all, so such a relation still has
  no first-seen date and is judged like BigQuery's zero-jobs case, so a fresh model built that
  way can be called unused before `--min-age-days` passes.
- **SYS timestamps arrive naive.** The views report UTC as `timestamp without time zone`, so
  the driver hands back naive datetimes (found live; BigQuery and Snowflake return aware
  ones). The client stamps UTC on them at its boundary, since the verdicts compare against aware
  `now` values.
- **Retention caps the window.** AWS documents seven days of history for the older STL views
  and leaves the SYS views' retention unstated. Whatever it is, it bounds both the effective
  `--lookback-days` and how far back `first_seen` can reach, making "unused" a weaker signal
  on Redshift than elsewhere, documented rather than worked around. First-seen still comes
  from the query history, never `SVV_TABLE_INFO.create_time`, which resets on every rebuild.
  A dead node with no first-seen row means no jobs within retention and is judged normally,
  like BigQuery; the missing-first-seen set-aside stays Snowflake-only (lagging metadata is
  Snowflake's failure mode, not Redshift's). *Measured 2026-07-11:* `MIN(start_time)` on
  `SYS_QUERY_HISTORY` still reaches the demo account's first activity (2026-07-10), so the
  account is too young to show a retention floor; re-measure once it is comfortably older
  than the candidate windows (from mid-August 2026).
- **The dbt exclusion reuses the Snowflake form** (`REGEXP_COUNT(query_text, $$...$$) = 0`);
  Redshift supports both the function and dollar-quoted literals. `query_text` is truncated at
  4000 characters, which is harmless: dbt's query-comment leads the statement, and the
  truncation otherwise only feeds `--columns` parse-failure confidence.
- **Table sizes come live from `SVV_TABLE_INFO`** (`size` in 1 MB blocks, converted to bytes),
  replacing catalog sizes where a row exists, like Snowflake's storage metrics. Redshift has
  no time-travel or fail-safe retention, so those fields stay zero and the report shows no
  retained-bytes breakdown. SVV_TABLE_INFO omits empty tables, which then keep their catalog
  size. *Confirmed live:* the sizes land on the scorecard (a 6-row table reports 35 MB, since
  Redshift's 1 MB-block floor per column slice makes tiny tables look large; the ranking is
  still right).
- **Orphans read `SVV_REDSHIFT_TABLES`**, filtered by database and lowercased schema,
  deliberately not SVV_TABLE_INFO, which omits empty tables, and an empty leftover is still an
  orphan.
- **The stale-source check is skipped with a note.** Redshift exposes no last-data-received
  metadata (no `last_altered`, no `__TABLES__` analogue), and inferring it from the query
  history would both breach the retention window and break the "metadata, never query history"
  rule, so the check is gated off in the CLI the way the partitioning check is BigQuery-only.
  `SYS_LOAD_HISTORY` was considered and rejected: it records only `COPY` loads (tables fed by
  `INSERT`, CTAS, or streaming leave no row), and its retention is the same unmeasured SYS
  window; if that window is shorter than `--stale-source-days`, any table with a row was
  loaded within retention and can never look stale, so the check could never fire. Revisit
  only if the retention measurement shows a window comfortably longer than the threshold.
  *Confirmed live:* with a declared source in the manifest, the scan prints the skip note on
  stderr, reports `stale_checked` false, and completes with exit 0.
- **Connection comes from `REDSHIFT_*` environment variables** (`HOST`, `USER`, `PASSWORD`,
  optional `DATABASE` and `PORT`). redshift-connector has no named-connection file like
  connections.toml, and env vars keep credentials off disk; the database defaults to the one
  inferred from the models, like `--project` elsewhere. Because the endpoint lives in an env
  var rather than a flag, `REDSHIFT_HOST` joins the scan-cache key, so two workgroups sharing
  a database name never serve each other's cached rows.
- **A Redshift-only table-hygiene check reads the `SVV_TABLE_INFO` maintenance columns**
  (`verdict/redshift_hygiene.py`), the Redshift counterpart of the BigQuery-only partitioning
  check. It flags tables of 1 GiB or more (at most 20) whose `unsorted` region is 20% or
  larger (scans stop pruning until VACUUM runs), whose `stats_off` is 10 or more (stale
  planner statistics; ANALYZE resets it), or whose `skew_rows` ratio is 4 or more (one slice
  becomes the bottleneck), AWS's own maintenance lines, held as module constants rather than
  flags. Ranking is by the bytes user queries scanned from each table (stored size as the
  fallback), so the top entry is the maintenance fix that saves the most. The columns can be
  NULL (no sortable data, zero rows, DISTSTYLE ALL) and parse as 0, which never trips a
  threshold, and an empty result renders as nothing: on Serverless and modern provisioned
  clusters automatic vacuum and analyze usually keep every figure near zero, so an empty
  check is the healthy state, not a failure. The rejected alternative, ranking raw
  sort/distribution-key declarations like the partitioning check ranks `partition_by`,
  stays rejected: Redshift assigns both automatically, so a bare declaration is not debt;
  only measured neglect is. Review-only, one extra system-view read, and it never feeds any
  unused figure. *Confirmed live:* on the Serverless demo the check runs and returns empty
  (`unhealthy_tables: []` in the JSON, no section in the text), exactly the healthy state
  auto vacuum/analyze is expected to produce; a flagged specimen has not been observed live,
  since planting one is timing-dependent at best.

## The rarely-used band and the hygiene checks

Between active and unused sits a third verdict (`verdict/rarity.py`). A model queried at most
`--rare-threshold` times (default 5) in the window is **rarely used**. It is reported with its
query count, last-queried date, size, and the bytes its queries scanned, and it is never folded
into any unused-derived figure, because observed use is use. The band is ranked by scanned
bytes first (stored size as the fallback), so the expensive-but-rarely-used model, the
strongest deprecation candidate, tops the list. The too-new guard applies to the band
the same way it applies to the dead set (a model created mid-window hasn't had a full window to
accumulate queries). The usage counts were always fetched; this band just stops discarding them.

The scanned bytes come from one extra column in the existing usage queries
(`JOBS.total_bytes_processed` on BigQuery, `QUERY_HISTORY.bytes_scanned` on Snowflake), so they
cost no extra warehouse call. They are reported as bytes, never dollars: bytes map directly to
money only on BigQuery on-demand pricing, and any dollar figure would be a guess. A query
touching several tables attributes its whole figure to each, so the numbers rank tables rather
than bill them, and they are a review signal only, with no usage verdict ever depending on them. The
same figure backs the direct-query evidence on unused declared sources and on orphaned
relations: each orphan carries any query count, last-queried date, and scanned bytes from the
same usage rows, and the still-queried ones (the dangerous-to-drop ones) rank first. The
signal is validated
live on both warehouses: the rare band's counts, dates, and scanned bytes match the queries run
against the demos, and the ranking follows bytes rather than query count. One trap the live run
surfaced: a query run in a different GCP project than the scan targets never appears in that
project's `INFORMATION_SCHEMA.JOBS`, which is exactly the project-scoping the up-front
permission check exists for.

Three hygiene stats ride along, all computed from dbt's own files with no warehouse call
(the Redshift table-hygiene check is the one exception, costing a single cheap
`SVV_TABLE_INFO` read; see the Redshift section).
`verdict/coverage.py` counts models with at least one test and models and columns with
descriptions (the column denominator prefers the catalog's physical columns, and the sentence
says which universe was used). `verdict/drift.py` reports documentation drift: a column
declared in a model's YAML that no longer exists in the built relation per `catalog.json`.
Nodes absent from the catalog are skipped (an unknown physical schema is not drift), and the
report notes that a stale catalog can false-positive, pointing at `dbt docs generate`.
`verdict/partitioning.py` flags the largest `table` and
`incremental` models (1 GiB or more of *stored* bytes, at most 20) declaring neither
`partition_by` nor `cluster_by`. That check only runs on BigQuery, since Snowflake
micro-partitions automatically and its explicit clustering keys are optional large-table tuning
rather than debt. The floor is on stored size, but the ranking is by the bytes user queries
scanned over the window (stored size as the fallback): an unpartitioned table only costs money
when queried, so the top entry is the partitioning fix that saves the most. Validated live on
`demo_bq`: a planted 1.34 GiB generated-rows table was flagged, disappeared once `cluster_by`
was declared and rebuilt, and small tables never fired it; with two such tables planted and
different byte volumes queried from each, the list ordered them by scanned bytes (875.5 MB
above 291.8 MB), matching the queries run.

## Working out where columns come from

Both column jobs boil down to the same thing. Take a column mentioned in a query and figure out
which real table and column it actually points at. That work lives in `sqlparse.py`:

- `columns_read` finds every table-and-column a query reads. This is how we find what's used.
- `column_lineage_edges` finds, for each column a model puts out, the source columns it was
  built from, followed through any nested queries. This is how we spread usage back upstream.

Working this out from the real table definitions, rather than matching column names as text,
avoids the three usual mistakes. `tests/test_sqlparse.py` pins each one down:

- **`SELECT *`** is counted as reading every column of the table, so nothing gets wrongly called
  dead.
- **Same name, different table.** An `id` in one query isn't assumed to be *your* `id`; it's
  matched to the table it actually came from.
- **Indirect use.** A column with no query of its own still counts as used if it feeds a column
  that does have a query.

`UNNEST` and struct/record access aren't tested against real query text yet.

`sqlglot` is the default way we read SQL, and it sits behind a shared interface
(`lineage/base.py`) so a different one could be swapped in.

## Orphans and the source findings

Most of the tool finds dbt things that nothing uses. The orphan check looks the other way and
finds tables in the warehouse that dbt doesn't know about. dbt knows two kinds of table, the
ones it **builds** (models, seeds, snapshots) and the ones it **reads** (sources). We compare
both against what's really in the warehouse.

An **orphan** is a table or view sitting in a dataset dbt builds into, but which dbt neither
builds nor reads. To see what's actually there, we ask each of those datasets for its own table
list and stack the lists together. We use the per-dataset lists because they need only read
access to that dataset, while the one region-wide list needs a stronger, project-wide grant
that even an Owner can be refused (confirmed live on BigQuery). If we can't read the lists, we
skip this finding with a warning and the scan still succeeds. The usage rows already fetched
attach direct-query evidence to each orphan, as on unused sources: a queried orphan is still
read directly and is dangerous to drop, so the still-queried ones rank first.

An **undeclared source** is a table a model reads from that dbt has no record of. It should be
declared as a `source()`. We find these by reading the model's own SQL (`references.py`), so
the check needs no warehouse access at all and works even when we can't list the warehouse
tables.

An **unused declared source** is the reverse (`verdict/sources.py`). A source sits in a
`sources.yml` and nothing in the project depends on it. No model, no exposure, no semantic-layer
consumer. A test attached to the source doesn't count as use, since a test guards data without
consuming it, so a source kept alive only by its own tests is still reported. The usage rows
already fetched for the model verdicts attach evidence to each entry. A zero query count means
the declaration is dead weight; a non-zero count means people query the raw table directly and
it may be worth modelling instead of deleting. Like the rarely-used band, this is a review list
and never feeds the unused-model figures.

A **stale source** (`verdict/staleness.py`) is a declared source whose table has received no
new data for more than `--stale-source-days` (default 30; `0` disables). That usually means the
loader upstream of dbt has stopped, which no usage figure can catch. The last-data date comes
from warehouse metadata, never from query history. On BigQuery each source dataset's legacy
`__TABLES__` table supplies `last_modified_time` (updated by loads and streaming writes) and
needs only dataset read access, the same optional grant as orphans; a missing grant skips the
check with a warning. Both halves are confirmed live on `demo_bq`: `__TABLES__` is a legacy
surface but reads fine from standard SQL, a source pointing at an old table is flagged with its
last-data date, and a source dataset the scan cannot read degrades to the one-line warning with
the rest of the scan (orphans included) unaffected and exit code 0. On Snowflake the check
reads `ACCOUNT_USAGE.TABLES.last_altered` (already required for first-seen, so no new grant),
taking `MAX` over the live rows; confirmed on the trial account, where every demo table returns
a date and a declared source past the threshold is flagged. Documented caveat, also observed
live: a bare `COMMENT ON TABLE` moves `last_altered` immediately, so any DDL resets the
staleness clock and the check can under-report staleness there, never invent it. A source with no metadata row is skipped, since absent metadata is not evidence. The
verdict is pure (sources and a date map in, a list out) and, like every review band, feeds no
unused figure.

Two things stop false alarms in the orphan check. We only look inside the datasets dbt builds
into, so raw and landing tables in source datasets never get flagged. And a table a model reads
always counts as an undeclared source, so it never shows up as an orphan. (Seeds, snapshots,
and sources are taken out too, so a seed sitting next to your models is never flagged.)

`verdict/orphans.py` and `verdict/sources.py` only do the comparing. They are handed ready-made
sets; the file reading and the warehouse call happen before them. If the table-list access is
missing, the tool falls back to reporting undeclared sources only rather than failing the scan.

## Databricks adapter and metadata validation

The Databricks adapter uses the existing `WarehouseClient` seam and lazily imports the optional
SQL connector. Authentication comes from `DATABRICKS_HOST` (or
`DATABRICKS_SERVER_HOSTNAME`), `DATABRICKS_HTTP_PATH`, and an optional `DATABRICKS_TOKEN`; the
normalized host and HTTP path isolate cache entries without including credentials. The required
preflight reads both `system.access.table_lineage` and `system.query.history`. Partial access
fails closed because a relation cannot safely be called unused from only one of those sources.
Relation inventory is separately optional and reads `system.information_schema.tables`.

Usage follows a conservative hybrid. Lineage with a `statement_id` joins successful `SELECT`
history through `cache_origin_statement_id`, which equals the query's own ID for an uncached query
and the originating ID for a cache hit. Databricks documents lineage statement IDs for SQL
warehouses, while controlled serverless notebook events also exposed joinable IDs. That permits
exact dbt query-comment exclusion while retaining cache repeats wherever the ID is available.
Lineage without a joinable statement ID counts as usage only when it has a source and no table or
path target. Source-to-target events are treated as probable build lineage and omitted. This
asymmetry intentionally permits false activity rather than false "unused" verdicts. Missing or
encrypted statement text also counts as usage.

First-seen is the earliest source or target event still retained in table lineage. Unity Catalog
`created` is not used because dbt table materializations can reset it. A relation absent from
retained lineage has unproven age and is always set aside, including when the caller disables the
recent-age threshold. Databricks query history is in Public Preview and, like table lineage, has
a rolling 365-day retention window; the effective evidence window cannot exceed retained,
regional system-table data.

Column analysis is disabled because complete query-text or column-lineage coverage has not been
proved across SQL warehouses, serverless compute, and classic compute. Source freshness is
deferred because no safe last-data timestamp has been established, and this contribution defines
no Databricks-specific hygiene verdict. These paths skip explicitly rather than manufacture
negative findings. Storage continues to use the `bytes` statistic in dbt-databricks'
`catalog.json`.

### Live validation results (2026-07-18 to 2026-07-19)

A controlled temporary development schema exercised direct and view reads, CTAS and replacement,
an exact repeated query, a dbt table run, and a dbt test on a SQL warehouse. Equivalent
synthetic reads and writes succeeded on serverless and classic job compute. No production or raw
object was created or modified. The temporary schema, workspace job, notebook, and job compute
were removed, and follow-up inventory confirmed no remaining schema or objects.

The available principal could read `system.information_schema.tables`, but lacked `USE SCHEMA` on
both `system.query` and `system.access`. The adapter's uncached end-to-end scan therefore stopped
at its required permission preflight, as designed, without attempting a weaker verdict.

A second validation in Databricks Free Edition had access to both required system schemas. It
built a four-model dbt-databricks project, including a view and a deliberately unread model, then
ran tests, docs generation, and separate SQL-warehouse reads. Fresh system-table rows appeared
after roughly 10–20 minutes. SQL-warehouse lineage IDs matched query-history statement IDs,
uncached rows had `cache_origin_statement_id` equal to their own statement ID, dbt's JSON query
comment was visible and excluded, and read/write lineage shapes matched the conservative query.
The uncached end-to-end scan reported three active models and the deliberately unread model as
unused, including its removable tests and `catalog.json` storage bytes. Relation inventory also
completed against the inferred Unity Catalog catalog and schema.

A temporary Free Edition serverless notebook job successfully read the active table and view and
was deleted immediately after completion. Its delayed lineage rows included job and notebook
metadata, `direct_access` values, and statement IDs that matched serverless query history in this
workspace. Two exact repeated warehouse reads reused an earlier result: both history rows pointed
their `cache_origin_statement_id` at the original lineage statement and were counted separately.
Column-lineage rows were visible for the SQL-warehouse workload, but column mode remains disabled
because this one controlled workload does not prove complete cross-compute coverage.
Classic-compute coverage remains documentation-based.

The available evidence did confirm that Unity Catalog reports managed tables as `MANAGED` and
views as `VIEW`. A direct `CREATE OR REPLACE TABLE` preserved `created` while changing
`last_altered`, but a dbt table materialization changed `created`, validating the decision not to
use that field for age. dbt-databricks 1.11 catalog output used the already-supported `bytes`,
`rows`, and `has_stats` keys. Representative compiled model SQL, CTAS, view, and query statements
parsed with SQLGlot's `databricks` dialect; `DESCRIBE DETAIL` did not, which is irrelevant while
the disabled column path never parses it.

The synthetic subtraction also confirmed an existing distinction: a physical table omitted from
the manifest but referenced by compiled model SQL is an undeclared source, not an orphan. An
unreferenced view was correctly inventoried as the physical orphan.

## Seeds, snapshots, and the semantic layer

dbt builds three kinds of table (models, seeds, snapshots) and they all face the same question.
Did anything query what this builds? So all three live in `Manifest.models`, told apart by a
`resource_type` tag (a seed simply has no SQL and no dependencies), and everything downstream
works unchanged. Usage rows join to seeds, the dependency graph keeps model-to-seed edges (so a
queried mart keeps the seed it descends from alive), and a dead seed ranks by its catalog bytes
like any dead model. `Manifest.relations` holds sources only.

Exposures carry one extra verdict of their own. An exposure whose every model dependency is
dead is reported as **likely dead** itself: nothing the dashboard reads was queried in the
whole window, so the strongest explanation is that nobody opens the dashboard either. The
affected and likely-dead lists are mutually exclusive (an exposure with some but not all
upstream models dead stays "affected, review"), non-model dependencies are ignored for the
all-dead rule, and because the dead set already excludes too-new and rarely-used nodes, an
exposure over those is never flagged.

The semantic layer (semantic models, metrics, saved queries; dbt 1.6+) is treated like
exposures. These declare use; they don't prove it. A dead model that feeds a semantic model, or
through it a metric or saved query, is flagged for review and never revived. The fixpoint
records the dependency that condemned each consumer (a dead model directly, or the affected
consumer in between), so the report names the cause on every line and the detail view walks
the chain from the saved query down to the model. A dead column that
a semantic model names in an entity, dimension, or measure `expr` is *blocked* rather than
consumed (`verdict/semantic.py` and the blocker check). Real semantic-layer queries hit the
warehouse and count as observed usage anyway. The node shapes are validated against a real
populated manifest (dbt 1.11, manifest v12, via the demo projects' `_semantic.yml`): the three
top-level keys arrive as `{unique_id: node}` dicts, `depends_on.nodes` chains model →
semantic model → metric → saved query exactly as the fixpoint expects, and the
entity/dimension/measure `expr`-with-name-fallback parsing produces the right `column_refs`,
with no parser change needed. A live scan itemizes all three consumers under a dead model.
One inference remains, flagged per our rule: expression parsing falls back to "no column
refs" when sqlglot can't read an expr. One practical note: dbt refuses to parse a project
with metrics but no MetricFlow time-spine model, so the demos carry a minimal
`metricflow_time_spine`.

## Too new to judge

A model created a few days ago has had no fair chance to be queried, so calling it "unused"
would be false-confident. Its creation date is taken as its **first appearance in the job
history**, computed as `MIN(creation_time)` per relation over *all* jobs in the window, dbt's
own builds included, unioned across `referenced_tables` and `destination_table`. An old model
rebuilt nightly has jobs throughout the window and is judged normally; a new one first appears
when it was created. The two obvious alternatives don't work. The manifest's `created_at` is
just parse time, and `INFORMATION_SCHEMA.TABLES.creation_time` resets on every `dbt run`
(tables and views are dropped and recreated) and lives on the permission-fragile orphan path.
First-seen instead rides on `JOBS_BY_PROJECT`, the one grant already hard-required, so there is
no new degradation mode. (Inference to confirm live: that CTAS/dbt builds populate
`destination_table`. It is standard JOBS-schema behaviour, which is why the query unions it in.)

A dead node first seen younger than `--min-age-days` (default 7; `0` disables) becomes a third
bucket, "too new to judge", listed separately and excluded from the unused count and from
everything derived from it (removable tests, exposure and semantic impact, reclaimable bytes).
A node never seen at all is judged normally, since no job in the whole window is the strongest
"unused" signal there is.

## Failing without tracebacks

Exit codes are a contract. `0` means the scan completed (including degraded scans, e.g. no
catalog, or orphans skipped), `2` a local problem (bad arguments, missing or malformed
artifacts), `3` any warehouse problem, `130` interrupted. Behind it sits a small error family.
Every warehouse failure is a `WarehouseError` (credentials and permissions are subclasses, and
any other BigQuery API error is translated in `bigquery.py`, still the only file that touches
google exceptions), and every unreadable artifact is an `ArtifactError` carrying its path. A
malformed manifest is fatal; a malformed catalog degrades exactly like a missing one. The cache
fails open (a cache directory that can't be written disables the cache with a warning, never
the scan), the viewer renders an export failure into its pane and treats Ctrl-C as quit, and an
unwritable `--output` path is a clean exit 2.

## The spinner and the saved-results cache

The warehouse steps of a scan can take a while, so two comforts exist for them. Both step
aside cleanly rather than become things the tool can't run without.

The **spinner** (`report/spinner.py`) shows a "working..." line during the slow warehouse
steps so a long scan doesn't look hung. It uses only the standard library, writes to the error
stream (reports, JSON, and `-o` output are never touched), only animates in a real terminal,
and draws plain text with no special terminal codes, so it is safe on older Windows consoles
and inert during tests.

The **cache** (`consumption/cache.py`) saves the slow warehouse round-trips (used tables,
query text, and the table lists) as JSON files so repeat runs are fast. It wraps the client
rather than changing it, which keeps the "only one file touches each warehouse SDK" rule and
lets the fake client exercise it in tests. Files are keyed by the things that change the
answer (warehouse, project, region, lookback window, and which queries count as dbt's) and
deliberately never by your dbt project. The permission preflight is never cached, because
permissions can change and that check is load-bearing.

The cache lives in the user's own cache directory (`$XDG_CACHE_HOME/dbt-debt`, or
`~/.cache/dbt-debt` when the variable is unset), never in a shared location like `/tmp`. The
files hold warehouse metadata (table names, usage counts, sizes), so the directories are
created owner-only (0700) and each entry is written atomically to a 0600 file via a private
temp file and a rename, which also stops another local user pre-planting files or symlinks at
the predictable paths. For the same reason, the finished text report strips terminal control
bytes before printing, so a crafted model name or file path in `manifest.json` cannot inject
escape sequences that recolour or overwrite report lines.

Each saved file carries its creation time **and the time-to-keep it was written under**.
`--cache-ttl` is not a setting stored anywhere; it persists across sessions only because every
entry records its own lifetime (`created` + `ttl_hours` inside the JSON file, which lives on
disk and so outlives the terminal session). A later flag-less run judges each
entry against the entry's own TTL; passing `--cache-ttl` explicitly overrides the stored values
for that run, in both directions (it can extend or force-shorten). Because the TTL lives in the
entries, clearing the cache also clears the remembered TTL; the next scan writes fresh entries
at the default (1 hour) unless the flag is passed again.

Past its time-to-keep an entry counts as a miss, and expired files are swept at the start of
the next scan by our own code, so cleanup behaves the same on every OS and nothing else has to
clean the cache directory. Clearing by hand has two forms. `dbt-debt --clear-cache` deletes
the whole cache folder and stops; `dbt-debt scan --clear-cache` clears this project's results
and then scans fresh. `--no-cache` skips the cache entirely; it neither reads nor writes.

## Why build a new tool

The combination dbt-debt provides did not exist before it. Verdicts are driven by the
warehouse's own query history rather than by metadata quality, they cover every model, seed,
snapshot, source, and column without naming resources up front, they work from the artifacts
dbt already writes, and the tool needs no account, plan, or login.
