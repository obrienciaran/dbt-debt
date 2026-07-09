# Design

This is the "how it's built" doc for `dbt-debt`. For how to use it, see [`README.md`](README.md).
What it covers: BigQuery and Snowflake, a standalone Python command-line tool, working at both
the model and the column level. Both warehouses are validated against live data; the Snowflake
section lists what is confirmed and what remains open.

A couple of words used throughout: a **model** is one of your `.sql` files; a **relation** is the
actual table or view that model builds in the warehouse.

## How the code is organised

The code is split into layers, and the rule is that a layer doesn't reach into another one it
shouldn't. The most important layer is `verdict/`: it only does the working-out, on data that's
already been loaded for it. It never talks to a warehouse and never reads files itself. That's
what lets us test the tricky logic with small hand-written examples and no cloud access at all.

### What lives where (`†` = planned, not built yet)

```
dbt_debt/
  cli.py                 # reads the arguments, wires the pieces together, prints the report
  config.py              # the settings for a run (paths, project, region, lookback, columns, format)
  domain.py              # the data classes everything passes around (Model, Column, Test, ...)
  sqlparse.py            # the SQL-reading helpers: which columns a query reads, and where they came from
  references.py          # which warehouse tables each model reads, used for finding orphans

  artifacts/             # read dbt's own files into our data classes (plain JSON; dbt is never imported)
    manifest.py          #   manifest.json -> models/seeds/snapshots, tests, exposures, semantic layer, SQL
    catalog.py           #   catalog.json -> the full column list and table sizes
    graph.py             #   the map of which buildable node depends on which: descendants() / ancestors()
    errors.py            #   ArtifactError: a broken artifact fails with the path, not a traceback

  consumption/           # ask the warehouse what was actually used
    client.py            # WarehouseClient, the shared interface the rest of the code talks to
                         #   (so it can be faked in tests); one implementation per warehouse
    bigquery.py          # the BigQuery version (the only file that imports the BigQuery library)
    snowflake.py         # the Snowflake version (the only file that imports the connector,
                         #   which is an optional extra; see the Snowflake section)
    cache.py             # an optional saved-results layer that wraps any client (same interface)
    jobs.py              # the BigQuery queries (query log, table list, first-seen) + the
                         #   warehouse-neutral row parsers both clients feed
    snowflake_queries.py # the Snowflake ACCOUNT_USAGE / INFORMATION_SCHEMA queries
    exclusion.py         # the filter that throws out dbt's own queries
    usage.py             # turn "these tables were used" into "these models were used"
    columns.py           # turn query text into the (model, column) pairs that were actually read

  lineage/               # which column feeds which, from one model to the next
    base.py              # the shared interface a lineage source has to provide
    sqlglot_source.py    # the default: read each model's SQL and trace its columns back upstream
    fusion_source.py †   # an optional faster source from dbt Fusion (experimental, needs a login)

  verdict/               # working-out only: given the data, decide what's unused
    models.py            # a node is unused if it, and everything built from it, went unqueried
    columns.py           # a column is unused if it's not read and feeds nothing that's read
    orphans.py           # an orphan is a table in a dbt dataset with no dbt model behind it
    freshness.py         # the too-new guard: first seen recently means "too new to judge", not unused
    rarity.py            # the rarely-used band: queried, but at most --rare-threshold times
    coverage.py          # test/docs coverage counts (models tested, models/columns documented)
    partitioning.py      # large BigQuery tables declaring neither partition_by nor cluster_by
    semantic.py          # which semantic models / metrics / saved queries a dead model feeds
    tests.py / exposures.py / blockers.py   # checks that only need dbt's own files

  report/
    scorecard.py         # put the result together
    render_text.py / render_json.py
    viewer.py            # the interactive tabbed viewer (Summary / Detail / JSON / Export), stdlib-only
    spinner.py           # a "working..." spinner shown only while the slow warehouse steps run
```

### How the data moves through it

```
settings ─┐
          ├─> artifacts: load manifest.json + catalog.json ─> data classes, dependency map, table sizes
          ├─> consumption: the warehouse
          │     • which tables were queried (in the window, dbt's own queries removed)
          │     • the query text (only when checking columns)
          │     • the list of tables in the dbt-managed datasets (only when finding orphans)
          ├─> lineage (column check): which column feeds which
          ├─> references: which tables each model reads
          └─> decide:
                models  = used tables, spread back up the dependency map
                columns = columns read in queries, spread back along the lineage
                orphans = tables in a managed dataset with no dbt model, that no model reads
                sources = tables a model reads that dbt doesn't know about (undeclared)
                tests / exposures / blockers = from dbt's own files
                                                          ─> the report ─> text or JSON
```

Usage spreads **upstream**: a model counts as used if it, or anything downstream of it, was
queried. Columns work the same way, spreading back along their lineage.

## Where the BigQuery data comes from

BigQuery doesn't log which *columns* a query touched, only which tables. So table usage is clean and
direct, but column usage has to be worked out by reading the text of each query.

| What we need | Where we get it |
|------|--------|
| Which **tables** a query touched | BigQuery's query log lists them directly |
| Which **columns** a query touched | read the query text and parse it with `sqlglot` |
| Which **tables exist** in a dataset | ask each dbt-managed dataset for its own table list |
| The full column list and table sizes | `catalog.json` (from `dbt docs generate`) |

BigQuery's query log only covers the project you query it in, and keeps roughly 180 days of history.
So the scan runs in the project your models live in (read from your project, or set with
`--project`). Before scanning, the tool tries to list *everyone's* queries. If BigQuery refuses, it
stops and says the `bigquery.jobs.listAll` permission is missing, because without it "unused" would
quietly mean "unused by me".

dbt's own queries are left out by spotting the marker dbt stamps on every query it runs
(`"app": "dbt"`). This matters because dbt's data tests are themselves `SELECT`s, and without this
they'd look like real usage.

## Snowflake

The warehouse sits behind one Protocol (`consumption/client.py`'s `WarehouseClient`), whose
methods return parsed domain values: `UsageRow`s, `WarehouseRelation`s, first-seen dates, query
texts. Everything inward of those values (verdict, report, cache, artifacts) is warehouse-free.
Adding a warehouse means one pure query-builder module, one SDK-touching client module, and a
sqlglot dialect threaded through the SQL parsing. The warehouse is picked from the manifest's
`adapter_type` (`--warehouse` overrides). Each client imports its SDK lazily, so a BigQuery user
never imports or installs anything Snowflake-related, and vice versa. The Snowflake connector is
the `[snowflake]` optional extra.

The adapter is built from Snowflake's published documentation, pinned by tests, and validated
against a live Enterprise account (`demo_snowflake/`, the same medallion project as `demo_bq/`).
Confirmed against live data: the ACCESS_HISTORY flatten and QUERY_HISTORY join return exact
per-relation query counts, the dbt query-comment exclusion holds (builds do not count as use),
orphan discovery finds hand-made tables through the case normalization, the first-seen/too-new
guard behaves as designed (see the first-seen bullet), and the scan exits 0 end to end. One check
is open: reclaimable-bytes figures do not appear on Snowflake, and whether dbt-snowflake's
`catalog.json` stats use a different key than we read is unverified.

The design decisions, and what remains to confirm:

- **Usage comes from `ACCOUNT_USAGE.ACCESS_HISTORY`** (`direct_objects_accessed`, flattened, one
  row per relation a query touched, the analogue of BigQuery's `referenced_tables`), joined to
  `QUERY_HISTORY` for the SELECT/success/window/dbt-exclusion filters. We deliberately do *not*
  fall back to sqlglot-parsing `query_text` for usage: a silently unparseable query would erase
  evidence of use and produce false "unused" verdicts, the one failure mode this tool must never
  have. ACCESS_HISTORY needs Enterprise edition and IMPORTED PRIVILEGES on the `SNOWFLAKE`
  database; on Standard the preflight stops the scan, mirroring the `jobs.listAll` stance.
- **First-seen comes from `ACCOUNT_USAGE.TABLES` including dropped incarnations**, taking
  `MIN(created)` over all rows for a name and counting rows whose `deleted` is set, so dbt's
  `CREATE OR REPLACE` rebuilds don't reset the age. Same reasoning as BigQuery's JOBS-not-TABLES
  choice. *Unverified inference:* that dropped incarnations are retained long enough to matter.
  *Confirmed live:* the guard itself. A brand-new dead model is set aside as too-new at the
  default `--min-age-days`, its test leaves the removable count, and the rarely-used band
  empties, once `ACCOUNT_USAGE.TABLES` has a row for it. *Open design question:*
  `ACCOUNT_USAGE.TABLES` lags further behind than ACCESS_HISTORY, so during that gap a node has
  usage but no first-seen row and is judged rather than set aside; whether a missing first-seen
  should mean too-new is undecided.
- **The dbt exclusion assumes dbt's query-comment lands in `query_text`** (it does on BigQuery).
  The pattern sits in a `$$...$$` dollar-quoted string (Snowflake's no-escape literal) inside
  `REGEXP_COUNT(...) = 0`, because Snowflake's `REGEXP_LIKE` anchors to the whole string.
  *Confirmed live:* dbt's builds are correctly excluded from usage counts.
- **Orphans** read one `<database>.INFORMATION_SCHEMA.TABLES` filtered by lowercased schema name
  (one query, unlike BigQuery's per-dataset union, because Snowflake's information schema spans
  the database). Snowflake's uppercase identifiers normalize away because every relation key is
  lowercased on both sides.
- ACCOUNT_USAGE lags reality by up to ~45 minutes (documented; around 20 in practice). Harmless
  for a debt scan.
- **DuckDB is deliberately unsupported**: it keeps no query history at all, so the core "unused"
  verdict has no signal to stand on, and its enterprise footprint among dbt users is small.

## The rarely-used band, coverage, and the partitioning check

Between active and unused sits a third verdict (`verdict/rarity.py`): a model queried at most
`--rare-threshold` times (default 5) in the window is **rarely used**. It is reported with its
query count, last-queried date, and size so an owner can judge it, but never folded into any
unused-derived figure, because observed use is use. The too-new guard applies to the band the
same way it applies to the dead set (a model created mid-window hasn't had a full window to
accumulate queries). The usage counts were always fetched; this band just stops discarding them.

Two artifact-only hygiene stats ride along. `verdict/coverage.py` counts models with at least one
test and models/columns with descriptions (the column denominator prefers the catalog's physical
columns, and the sentence says which universe was used). `verdict/partitioning.py` flags the
largest `table`/`incremental` models (1 GiB or more, at most 20) declaring neither `partition_by`
nor `cluster_by`. That check is BigQuery-only, since Snowflake micro-partitions automatically and
its explicit clustering keys are optional large-table tuning rather than debt. It ranks by
*stored* bytes; scan cost is not collected (see the backlog).

## Working out where columns come from

Both column jobs boil down to the same thing: take a column mentioned in a query and figure out
which real table and column it actually points at. That work lives in `sqlparse.py`:

- `columns_read`: every table-and-column a query reads (this is how we find what's used).
- `column_lineage_edges`: for each column a model puts out, the source columns it was built from,
  followed through any nested queries (this is how we spread usage back upstream).

Working this out from the real table definitions, rather than just matching column names as text,
avoids the three usual mistakes. `tests/test_sqlparse.py` pins each one down:

- **`SELECT *`**: counted as reading every column of the table, so nothing gets wrongly called dead.
- **same name, different table**: an `id` in one query isn't assumed to be *your* `id`; it's matched
  to the table it actually came from.
- **indirect use**: a column with no query of its own still counts as used if it feeds a column that
  does have a query.

`UNNEST` and struct/record access aren't tested against real query text yet.

`sqlglot` is the default way we read SQL, and it sits behind a shared interface (`lineage/base.py`)
so a different one could be swapped in. dbt Fusion could be faster, but its column lineage needs a
strict mode and looks tied to a dbt-platform account, so it can never be a *requirement*. It
stays unbuilt until that's confirmed.

## Orphans and undeclared sources

Most of the tool finds dbt things that nothing uses. The orphan check looks the other way: it finds
tables in the warehouse that dbt doesn't know about. dbt knows two kinds of table, the ones it
**builds** (models, seeds, snapshots) and the ones it **reads** (sources). We compare both against
what's really in the warehouse and get two findings.

An **orphan** is a table or view sitting in a dataset dbt builds into, but which dbt neither builds
nor reads. To see what's actually there, we ask each of those datasets for its own table list and
stack the lists together. We use the per-dataset lists rather than one big region-wide list because
the per-dataset list needs only read access to that dataset, while the region-wide one needs a
stronger, project-wide grant that even an Owner can be refused (confirmed live on BigQuery). If
we can't read the lists, we skip this finding with a warning and the scan still succeeds.

An **undeclared source** is a table a model reads from that dbt has no record of. It should be
declared as a `source()`. We find these by reading the model's own SQL (`references.py`), so it
needs no warehouse access at all and shows up even when we can't list the warehouse tables.

Two things stop false alarms. We only look inside the datasets dbt builds into, so raw/landing
tables in source datasets never get flagged. And a table a model reads always counts as an
undeclared source, so it never shows up as an orphan. (Seeds, snapshots, and sources are taken out
too, so a seed sitting next to your models is never flagged.)

`verdict/orphans.py` only does the comparing. It's handed three ready-made sets: what exists in
the warehouse, what the models read, and what dbt knows about. The reading and the warehouse call
happen before it, and if the table-list access is missing the tool falls back to "undeclared
sources only" rather than failing the scan.

## Seeds, snapshots, and the semantic layer

dbt builds three kinds of table (models, seeds, snapshots) and they all face the same question:
did anything query what this builds? So all three live in `Manifest.models`, told apart by a
`resource_type` tag (a seed simply has no SQL and no dependencies), and everything downstream
works unchanged: usage rows join to seeds, the dependency graph keeps model→seed edges (so a
queried mart keeps the seed it descends from alive), and a dead seed ranks by its catalog bytes
like any dead model. `Manifest.relations` holds sources only.

The semantic layer (semantic models, metrics, saved queries; dbt 1.6+) is treated like exposures:
declared use, not observed use. A dead model that feeds a semantic model, or through it a metric
or saved query, is flagged for review, never revived. A dead column that a semantic model names
in an entity/dimension/measure `expr` is *blocked*, not consumed (`verdict/semantic.py` and the
blocker check). Real semantic-layer queries hit the warehouse and count as observed usage anyway.
Two things here are inference, flagged per our rule: the semantic-node shapes are parsed from the
published v12 manifest schema and not yet checked against a populated real-world manifest (the
parser stays lenient), and expression parsing falls back to "no column refs" when sqlglot can't
read an expr.

## Too new to judge

A model created a few days ago has had no fair chance to be queried, so calling it "unused"
would be false-confident. Its creation date is taken as its **first appearance in the job
history**: `MIN(creation_time)` per relation over *all* jobs in the window, dbt's own builds
included, unioned across `referenced_tables` and `destination_table`. An old model rebuilt
nightly has jobs throughout the window (judged normally); a new one first appears when it was
created. The two obvious alternatives don't work: the manifest's `created_at` is just parse
time, and `INFORMATION_SCHEMA.TABLES.creation_time` resets on every `dbt run` (tables and views
are dropped and recreated) and lives on the permission-fragile orphan path. First-seen instead
rides on `JOBS_BY_PROJECT`, the one grant already hard-required, so there is no new degradation
mode. (Inference to confirm live: that CTAS/dbt builds populate `destination_table`; it is
standard JOBS-schema behaviour, which is why the query unions it in.)

A dead node first seen younger than `--min-age-days` (default 7; `0` disables) becomes a third
bucket, "too new to judge", listed separately and excluded from the unused count and from
everything derived from it: removable tests, exposure and semantic impact, reclaimable bytes.
A node never seen at all is judged normally, since no job in the whole window is the strongest
"unused" signal there is.

## Failing without tracebacks

Exit codes are a contract: `0` the scan completed (including degraded scans, e.g. no catalog, or
orphans skipped), `2` a local problem (bad arguments, missing or malformed artifacts), `3` any
warehouse problem, `130` interrupted. Behind it sits a small error family. Every warehouse
failure is a `WarehouseError` (credentials and permissions are subclasses, and any other
BigQuery API error is translated in `bigquery.py`, still the only file that touches google
exceptions), and every unreadable artifact is an `ArtifactError` carrying its path. A malformed
manifest is fatal; a malformed catalog degrades exactly like a missing one. The cache fails
open (a cache directory that can't be written disables the cache with a warning, never the
scan), the viewer renders an export failure into its pane and treats Ctrl-C as quit, and an
unwritable `--output` path is a clean exit 2.

## The spinner and the saved-results cache

Two small comforts for a slow scan. Both are built to quietly step aside rather than become things
the tool can't run without.

The **spinner** (`report/spinner.py`) uses nothing but the Python standard library and writes to
the error stream, not the normal output stream. It only animates when that error stream is a real
terminal, so piped output, JSON, and file output (all of which go to the normal output stream) are
never touched. It draws plain text frames, redrawing the same line over and over, with none of the
special terminal codes that can break an older Windows console. During tests there's no real
terminal, so the spinner does nothing and the existing tests are unaffected.

The **cache** (`consumption/cache.py`) wraps the warehouse client rather than changing it, so the
"only one file touches each warehouse SDK" rule still holds, the cache works with the real client,
and the same fake client used in tests exercises it too. It saves the slow round-trips (used
tables, query text, and the table lists) as JSON files, named by the things that actually change
the answer (warehouse, project, region, lookback window, and which queries count as dbt's) and
deliberately **not** by your dbt project, since the warehouse results don't depend on it. The
"can you see everyone's queries?" check is never saved, because permissions can change and that
check is load-bearing.

Cleaning up is deliberate. Every saved file carries the time it was created. Once it's older than
the time-to-keep, it counts as a miss (we fetch fresh and delete the old file), and the whole cache
folder is swept for expired files at the *start of the next scan*. That sweep is our own code, so it
behaves the same on Mac, Linux, and Windows. The operating system tidying its own temp folder is
only a slow backup we don't rely on: it varies a lot (Linux often on reboot or by age, macOS after
a few days) and Windows doesn't clear its temp folder on reboot at all. Because the sweep happens on
the *next* run, a single one-off scan leaves its tiny JSON file behind (ignored once it's stale)
until then.

So clearing is something you do on purpose, and there are two forms depending on where you put
`--clear-cache`. **`dbt-debt --clear-cache`** on its own (no `scan`) deletes the whole cache folder
and stops: a clean slate with no scan. **`dbt-debt scan --clear-cache`** deletes just *this*
project's saved results and then carries on into the scan, building them fresh as it goes. The
default time-to-keep is 1 hour, short enough to stay fresh and long enough to cover the usual
run / look / tweak / run-again loop, which happens within minutes. `--no-cache` skips the whole
thing. (Your dbt files are read fresh every run, so the 1-hour limit only caps the age of the
query history, which barely moves against a 180-day window.)

## Why build a new tool

Other tools solve nearby problems but not this one. **dbt-score** scores how good your metadata is,
not whether things get used. **dbt-model-usage** has exactly the right query-log logic but ships it
as dbt tests, so you have to name every resource up front. **dbt-orphan** finds orphans (a table
with no dbt model) but also ships as dbt tests and not on BigQuery; dbt-debt folds the same idea
straight into the scan. **dbt-project-evaluator** flags unused sources, not unused models. And dbt
platform's query history is Enterprise-only. None of them do usage-driven dead-code finding down to
the column, open-source and with no login.
