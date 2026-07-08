"""An optional, self-pruning disk cache in front of any `BigQueryClient`.

`CachingBigQueryClient` is a decorator implementing the `BigQueryClient` Protocol, so it composes
with the real client and is exercised by the same `FakeBigQueryClient` in tests. It memoizes the
three slow warehouse round-trips (`table_usage`, `query_texts`, `existing_relations`) to JSON
files keyed by the query parameters — never the manifest, which warehouse results don't depend on.
The `jobs.listAll` preflight is delegated, never cached, because permissions can change and the
check is load-bearing.

Entries carry their creation time and expire after `ttl`: an expired read is a miss (and the file
is removed), and every cache directory is pruned of expired files on construction. That TTL prune
is the guaranteed, cross-platform teardown — important because Windows does not clear its temp
directory on reboot the way Unix clears `/tmp`. The cache therefore cannot persist forever.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping, Set
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from dbt_debt.consumption.client import BigQueryClient
from dbt_debt.domain import UsageRow, WarehouseRelation

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
        )
        for row in data
    ]


def _relations_to_json(rows: list[WarehouseRelation]) -> list[dict[str, Any]]:
    return [{"relation_key": r.relation_key, "relation_type": r.relation_type} for r in rows]


def _relations_from_json(data: list[dict[str, Any]]) -> list[WarehouseRelation]:
    return [
        WarehouseRelation(relation_key=r["relation_key"], relation_type=r["relation_type"])
        for r in data
    ]


class CachingBigQueryClient:
    """Wrap an inner `BigQueryClient`, serving the slow calls from a TTL-bounded disk cache."""

    def __init__(
        self,
        inner: BigQueryClient,
        *,
        cache_dir: Path,
        ttl: timedelta,
        key_parts: Mapping[str, str],
    ) -> None:
        self._inner = inner
        self._dir = cache_dir
        self._ttl = ttl
        self._key_parts = dict(key_parts)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._prune()

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

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        extra = {"datasets": sorted(datasets)}
        return self._cached(
            "existing_relations",
            extra,
            lambda: self._inner.existing_relations(datasets),
            _relations_to_json,
            _relations_from_json,
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
        path = self._path(method, extra)
        cached = self._read(path)
        if cached is not None:
            return decode(cached)
        value = fetch()
        self._write(path, encode(value))
        return value

    @staticmethod
    def _load_entry(path: Path) -> tuple[datetime, object] | None:
        """Parse a cache file into (created, data), or None when it is missing or corrupt.

        Any malformed file — unreadable, not JSON, not a dict, missing or mistyped fields —
        reads as None so a damaged cache degrades to a miss instead of crashing the scan.
        """

        try:
            entry = json.loads(path.read_text())
            return datetime.fromisoformat(entry["created"]), entry["data"]
        except (ValueError, KeyError, TypeError, OSError):
            return None

    def _read(self, path: Path) -> object | None:
        """Return the cached payload, or None when absent or expired (expired files are removed)."""

        entry = self._load_entry(path)
        if entry is None:
            return None
        created, data = entry
        if datetime.now(timezone.utc) - created > self._ttl:
            path.unlink(missing_ok=True)
            return None
        return data

    def _write(self, path: Path, data: object) -> None:
        entry = {"created": datetime.now(timezone.utc).isoformat(), "data": data}
        path.write_text(json.dumps(entry))

    def _prune(self) -> None:
        """Delete every expired or corrupt entry in the cache directory — the teardown that bounds growth."""

        now = datetime.now(timezone.utc)
        for path in self._dir.glob("*.json"):
            entry = self._load_entry(path)
            if entry is None or now - entry[0] > self._ttl:
                path.unlink(missing_ok=True)
