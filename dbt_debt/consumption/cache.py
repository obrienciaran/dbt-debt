"""An optional, self-pruning disk cache in front of any `WarehouseClient`.

`CachingWarehouseClient` is a decorator implementing the `WarehouseClient` Protocol, so it
composes with any real client and is exercised by the same `FakeWarehouseClient` in tests. It
memoizes the slow warehouse round-trips (`table_usage`, `query_texts`, `relation_first_seen`,
`existing_relations`, `table_storage`, `source_last_modified`) to JSON files keyed by the query
parameters — never the manifest, which warehouse results don't depend on. The permission
preflight is delegated, never cached, because permissions can change and the check is
load-bearing.

Entries carry their creation time *and the TTL they were written under*: an expired read is a
miss (and the file is removed), and every cache directory is pruned of expired files on
construction. Storing the TTL per entry is what makes `--cache-ttl 2` outlive the session that
passed it — a later flag-less run honors each entry's own lifetime rather than re-judging it
against the default. An explicit `--cache-ttl` on the current run overrides the stored values
(`honor_entry_ttl=False`), in both directions. The TTL prune is the guaranteed, cross-platform
teardown — important because Windows does not clear its temp directory on reboot the way Unix
clears `/tmp`. The cache therefore cannot persist forever.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections.abc import Callable, Mapping, Set
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from dbt_debt.consumption.client import WarehouseClient
from dbt_debt.domain import TableStorage, UsageRow, WarehouseRelation

CACHE_ROOT_NAME = "dbt-debt-cache"

_T = TypeVar("_T")


def cache_root() -> Path:
    """The directory holding every project's cache, under the OS temp dir.

    `tempfile.gettempdir()` resolves correctly on every OS (`/tmp`, `$TMPDIR`, or `%TEMP%`). The
    bare `dbt-debt --clear-cache` removes this whole directory.
    """

    return Path(tempfile.gettempdir()) / CACHE_ROOT_NAME


def cache_dir_for(project_dir: Path) -> Path:
    """The per-project cache directory under `cache_root()`.

    Keyed by a hash of the resolved project path so different dbt projects never collide and
    `dbt-debt scan --clear-cache` can wipe exactly one project's cache.
    """

    digest = hashlib.sha256(str(project_dir.resolve()).encode()).hexdigest()[:16]
    return cache_root() / digest


def _usage_to_json(rows: list[UsageRow]) -> list[dict[str, Any]]:
    return [
        {
            "relation_key": row.relation_key,
            "query_count": row.query_count,
            "last_queried": row.last_queried.isoformat() if row.last_queried else None,
            "bytes_scanned": row.bytes_scanned,
        }
        for row in rows
    ]


def _usage_from_json(data: list[dict[str, Any]]) -> list[UsageRow]:
    return [
        UsageRow(
            relation_key=row["relation_key"],
            query_count=row["query_count"],
            last_queried=datetime.fromisoformat(row["last_queried"])
            if row["last_queried"]
            else None,
            # Entries written before bytes were collected have no key; read them as 0.
            bytes_scanned=row.get("bytes_scanned", 0),
        )
        for row in data
    ]


def _first_seen_to_json(data: dict[str, datetime]) -> dict[str, str]:
    return {key: value.isoformat() for key, value in data.items()}


def _first_seen_from_json(data: dict[str, str]) -> dict[str, datetime]:
    return {key: datetime.fromisoformat(value) for key, value in data.items()}


def _relations_to_json(rows: list[WarehouseRelation]) -> list[dict[str, Any]]:
    return [{"relation_key": r.relation_key, "relation_type": r.relation_type} for r in rows]


def _relations_from_json(data: list[dict[str, Any]]) -> list[WarehouseRelation]:
    return [
        WarehouseRelation(relation_key=r["relation_key"], relation_type=r["relation_type"])
        for r in data
    ]


def _storage_to_json(data: dict[str, TableStorage]) -> dict[str, list[int]]:
    return {key: [s.active_bytes, s.time_travel_bytes, s.failsafe_bytes] for key, s in data.items()}


def _storage_from_json(data: dict[str, list[int]]) -> dict[str, TableStorage]:
    return {
        key: TableStorage(
            active_bytes=values[0], time_travel_bytes=values[1], failsafe_bytes=values[2]
        )
        for key, values in data.items()
    }


class CachingWarehouseClient:
    """Wrap an inner `WarehouseClient`, serving the slow calls from a TTL-bounded disk cache."""

    def __init__(
        self,
        inner: WarehouseClient,
        *,
        cache_dir: Path,
        ttl: timedelta,
        key_parts: Mapping[str, str],
        honor_entry_ttl: bool = True,
    ) -> None:
        self._inner = inner
        self._dir = cache_dir
        self._ttl = ttl
        self._honor_entry_ttl = honor_entry_ttl
        self._key_parts = dict(key_parts)
        self._disabled = False
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._prune()
        except OSError as exc:
            self._warn_disabled(exc)

    def assert_usage_permission(self) -> None:
        """Delegate the preflight; permissions are never cached."""

        self._inner.assert_usage_permission()

    def table_usage(self) -> list[UsageRow]:
        return self._cached(
            "table_usage", {}, self._inner.table_usage, _usage_to_json, _usage_from_json
        )

    def query_texts(self) -> list[str]:
        return self._cached(
            "query_texts", {}, self._inner.query_texts, lambda v: v, lambda v: list(v)
        )

    def relation_first_seen(self) -> dict[str, datetime]:
        return self._cached(
            "relation_first_seen",
            {},
            self._inner.relation_first_seen,
            _first_seen_to_json,
            _first_seen_from_json,
        )

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        extra = {"datasets": sorted(datasets)}
        return self._cached(
            "existing_relations",
            extra,
            lambda: self._inner.existing_relations(datasets),
            _relations_to_json,
            _relations_from_json,
        )

    def table_storage(self) -> dict[str, TableStorage]:
        return self._cached(
            "table_storage",
            {},
            self._inner.table_storage,
            _storage_to_json,
            _storage_from_json,
        )

    def source_last_modified(self, datasets: Set[str]) -> dict[str, datetime]:
        extra = {"datasets": sorted(datasets)}
        return self._cached(
            "source_last_modified",
            extra,
            lambda: self._inner.source_last_modified(datasets),
            _first_seen_to_json,
            _first_seen_from_json,
        )

    def _path(self, method: str, extra: Mapping[str, object]) -> Path:
        payload = {"method": method, "key_parts": self._key_parts, "extra": extra}
        blob = json.dumps(payload, sort_keys=True).encode()
        return self._dir / f"{hashlib.sha256(blob).hexdigest()}.json"

    def _cached(
        self,
        method: str,
        extra: Mapping[str, object],
        fetch: Callable[[], _T],
        encode: Callable[[_T], Any],
        decode: Callable[[Any], _T],
    ) -> _T:
        if self._disabled:
            return fetch()
        path = self._path(method, extra)
        cached = self._read(path)
        if cached is not None:
            return decode(cached)
        value = fetch()
        try:
            self._write(path, encode(value))
        except OSError as exc:
            self._warn_disabled(exc)
        return value

    def _warn_disabled(self, exc: OSError) -> None:
        """Mark the cache unusable for this run; a cache that can't write must never kill a scan."""

        self._disabled = True
        print(f"scan cache disabled ({exc}); querying the warehouse live.", file=sys.stderr)

    @staticmethod
    def _load_entry(path: Path) -> tuple[datetime, timedelta | None, object] | None:
        """Parse a cache file into (created, own ttl, data), or None when missing or corrupt.

        Any malformed file — unreadable, not JSON, not a dict, missing or mistyped fields —
        reads as None so a damaged cache degrades to a miss instead of crashing the scan. The
        ttl is None for entries written before it was stored per entry.
        """

        try:
            entry = json.loads(path.read_text())
            ttl_hours = entry.get("ttl_hours")
            ttl = timedelta(hours=ttl_hours) if isinstance(ttl_hours, (int, float)) else None
            return datetime.fromisoformat(entry["created"]), ttl, entry["data"]
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            return None

    def _effective_ttl(self, entry_ttl: timedelta | None) -> timedelta:
        """The entry's own lifetime when we honor stored TTLs, else this run's."""

        if self._honor_entry_ttl and entry_ttl is not None:
            return entry_ttl
        return self._ttl

    def _read(self, path: Path) -> object | None:
        """Return the cached payload, or None when absent or expired (expired files are removed)."""

        entry = self._load_entry(path)
        if entry is None:
            return None
        created, entry_ttl, data = entry
        if datetime.now(timezone.utc) - created > self._effective_ttl(entry_ttl):
            with suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        return data

    def _write(self, path: Path, data: object) -> None:
        entry = {
            "created": datetime.now(timezone.utc).isoformat(),
            "ttl_hours": self._ttl / timedelta(hours=1),
            "data": data,
        }
        path.write_text(json.dumps(entry))

    def _prune(self) -> None:
        """Delete every expired or corrupt entry in the cache directory — the teardown that bounds growth."""

        now = datetime.now(timezone.utc)
        for path in self._dir.glob("*.json"):
            entry = self._load_entry(path)
            if entry is None or now - entry[0] > self._effective_ttl(entry[1]):
                path.unlink(missing_ok=True)
