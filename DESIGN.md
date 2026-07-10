# Design

This doc explains how `dbt-debt` is built. For how to use it, see [`README.md`](README.md).
The tool covers BigQuery and Snowflake, runs as a standalone Python command-line program, and
works at both the model and the column level. Both warehouses are validated against live data.
The Snowflake section lists what is confirmed and what remains open.

Two words come up throughout. A **model** is one of your `.sql` files. A **relation** is the
actual table or view that model builds in the warehouse.

## How the code is organised

The code is split into layers, and a layer never reaches into one it shouldn't. The most
important layer is `verdict/`. It only does the working-out, on data that has already been
loaded for it. It never talks to a warehouse and never reads files itself. That is what lets us
test the tricky logic with small hand-written examples and no cloud access at all.

### What lives where (`†` = planned, not built yet)

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
    cache.py             # an optional saved-results layer that wraps any client (same interface)
    jobs.py              # the BigQuery queries (query log, table list, first-seen) and the
                         #   warehouse-neutral row parsers both clients feed
    snowflake_queries.py # the Snowflake ACCOUNT_USAGE / INFORMATION_SCHEMA queries
    exclusion.py         # the filter that throws out dbt's own queries
    usage.py             # turn "these tables were used" into "these models were used"
    columns.py           # turn query text into the (model, column) pairs that were actually read

  lineage/               # which column feeds which, from one model to the next
    base.py              # the shared interface a lineage source has to provide
    sqlglot_source.py    # the default; reads each model's SQL and traces its columns back upstream
    fusion_source.py †   # an optional faster source from dbt Fusion (experimental, needs a login)

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
BigQuery's `num_bytes` — the catalog reader checks both (views carry no stats and report 0),
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
  comes from JOBS rather than TABLES. *Unverified inference:* that dropped incarnations are
  retained long enough to matter. *Confirmed live:* the guard itself. A brand-new dead model is
  set aside as too-new at the default `--min-age-days`, its test leaves the removable count, and
  the rarely-used band empties, once `ACCOUNT_USAGE.TABLES` has a row for it. *Decided
  2026-07-10:* `ACCOUNT_USAGE.TABLES` lags reality (documented 90 minutes), so on Snowflake a
  dead node with no first-seen row cannot prove its age and is set aside as "missing a
  first-seen date (likely a new table)" — a review list beside too-new, excluded from every
  unused-derived figure, and the rare band gets the same protection. BigQuery is untouched:
  there first-seen comes from JOBS, so a missing row means zero jobs all window, the strongest
  unused signal there is.
- **The dbt exclusion assumes dbt's query-comment lands in `query_text`** (it does on BigQuery).
  The pattern sits in a `$$...$$` dollar-quoted string (Snowflake's no-escape literal) inside
  `REGEXP_COUNT(...) = 0`, because Snowflake's `REGEXP_LIKE` anchors to the whole string.
  *Confirmed live:* dbt's builds are correctly excluded from usage counts.
- **Orphans** read one `<database>.INFORMATION_SCHEMA.TABLES` filtered by lowercased schema name.
  This is one query, unlike BigQuery's per-dataset union, because Snowflake's information schema
  spans the database. Snowflake's uppercase identifiers normalize away because every relation
  key is lowercased on both sides.
- ACCOUNT_USAGE lags reality (documented: 90 minutes for TABLES, 3 hours for ACCESS_HISTORY,
  both approximate and often much less in practice). Harmless for a debt scan; the
  missing-first-seen set-aside above absorbs the TABLES gap.
- **DuckDB is deliberately unsupported.** It keeps no query history at all, so the core "unused"
  verdict has no signal to stand on, and its enterprise footprint among dbt users is small.

## The rarely-used band and the hygiene checks

Between active and unused sits a third verdict (`verdict/rarity.py`). A model queried at most
`--rare-threshold` times (default 5) in the window is **rarely used**. It is reported with its
query count, last-queried date, and size so an owner can judge it, and it is never folded into
any unused-derived figure, because observed use is use. The too-new guard applies to the band
the same way it applies to the dead set (a model created mid-window hasn't had a full window to
accumulate queries). The usage counts were always fetched; this band just stops discarding them.

Three hygiene stats ride along, all computed from dbt's own files with no warehouse call.
`verdict/coverage.py` counts models with at least one test and models and columns with
descriptions (the column denominator prefers the catalog's physical columns, and the sentence
says which universe was used). `verdict/drift.py` reports documentation drift: a column
declared in a model's YAML that no longer exists in the built relation per `catalog.json`.
Nodes absent from the catalog are skipped (an unknown physical schema is not drift), and the
report notes that a stale catalog can false-positive, pointing at `dbt docs generate`.
`verdict/partitioning.py` flags the largest `table` and
`incremental` models (1 GiB or more, at most 20) declaring neither `partition_by` nor
`cluster_by`. That check only runs on BigQuery, since Snowflake micro-partitions automatically
and its explicit clustering keys are optional large-table tuning rather than debt. It ranks by
*stored* bytes; scan cost is not collected (see the backlog).

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
(`lineage/base.py`) so a different one could be swapped in. dbt Fusion could be faster, but its
column lineage needs a strict mode and looks tied to a dbt-platform account, so it can never be
a requirement. It stays unbuilt until that's confirmed.

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
skip this finding with a warning and the scan still succeeds.

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
check with a warning. *Inference to confirm live:* `__TABLES__` is a legacy surface, readable
from standard SQL. On Snowflake the check reads `ACCOUNT_USAGE.TABLES.last_altered` (already
required for first-seen, so no new grant), taking `MAX` over the live rows. Documented caveat:
`last_altered` also moves on DDL, so the check can under-report staleness there, never invent
it. A source with no metadata row is skipped, since absent metadata is not evidence. The
verdict is pure (sources and a date map in, a list out) and, like every review band, feeds no
unused figure.

Two things stop false alarms in the orphan check. We only look inside the datasets dbt builds
into, so raw and landing tables in source datasets never get flagged. And a table a model reads
always counts as an undeclared source, so it never shows up as an orphan. (Seeds, snapshots,
and sources are taken out too, so a seed sitting next to your models is never flagged.)

`verdict/orphans.py` and `verdict/sources.py` only do the comparing. They are handed ready-made
sets; the file reading and the warehouse call happen before them. If the table-list access is
missing, the tool falls back to reporting undeclared sources only rather than failing the scan.

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
through it a metric or saved query, is flagged for review and never revived. A dead column that
a semantic model names in an entity, dimension, or measure `expr` is *blocked* rather than
consumed (`verdict/semantic.py` and the blocker check). Real semantic-layer queries hit the
warehouse and count as observed usage anyway. Two things here are inference, flagged per our
rule. The semantic-node shapes are parsed from the published v12 manifest schema and not yet
checked against a populated real-world manifest (the parser stays lenient), and expression
parsing falls back to "no column refs" when sqlglot can't read an expr.

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

Each saved file carries its creation time **and the time-to-keep it was written under** —
`--cache-ttl` is not a setting stored anywhere; it persists across sessions only because every
entry records its own lifetime (`created` + `ttl_hours` inside the JSON file, which lives in
the OS temp directory and so outlives the terminal session). A later flag-less run judges each
entry against the entry's own TTL; passing `--cache-ttl` explicitly overrides the stored values
for that run, in both directions (it can extend or force-shorten). Because the TTL lives in the
entries, clearing the cache also clears the remembered TTL — the next scan writes fresh entries
at the default (1 hour) unless the flag is passed again.

Past its time-to-keep an entry counts as a miss, and expired files are swept at the start of
the next scan by our own code, so cleanup behaves the same on every OS (Windows never clears
its temp folder on reboot). Clearing by hand has two forms. `dbt-debt --clear-cache` deletes
the whole cache folder and stops; `dbt-debt scan --clear-cache` clears this project's results
and then scans fresh. `--no-cache` skips the cache entirely — it neither reads nor writes.

## Why build a new tool

The combination dbt-debt provides did not exist before it. Verdicts are driven by the
warehouse's own query history rather than by metadata quality, they cover every model, seed,
snapshot, source, and column without naming resources up front, they work from the artifacts
dbt already writes, and the tool needs no account, plan, or login.
