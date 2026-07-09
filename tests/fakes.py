"""A `WarehouseClient` test double returning canned data, no network or credentials."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Set
from datetime import datetime

from dbt_debt.consumption.client import MissingPermissionError
from dbt_debt.domain import UsageRow, WarehouseRelation


class FakeWarehouseClient:
    """Canned implementation of the `WarehouseClient` Protocol for deterministic tests.

    `calls` counts invocations per method so cache tests can assert the inner client was hit
    exactly once (a hit serves the second call from disk without touching it).
    """

    def __init__(
        self,
        usage: Iterable[UsageRow] = (),
        query_texts: Iterable[str] = (),
        permitted: bool = True,
        existing: Iterable[WarehouseRelation] = (),
        orphans_permitted: bool = True,
        first_seen: Mapping[str, datetime] | None = None,
        last_modified: Mapping[str, datetime] | None = None,
        freshness_permitted: bool = True,
    ) -> None:
        self._usage = list(usage)
        self._query_texts = list(query_texts)
        self._permitted = permitted
        self._existing = list(existing)
        self._orphans_permitted = orphans_permitted
        self._first_seen = dict(first_seen or {})
        self._last_modified = dict(last_modified or {})
        self._freshness_permitted = freshness_permitted
        self.calls: Counter[str] = Counter()

    def assert_usage_permission(self) -> None:
        self.calls["assert_usage_permission"] += 1
        if not self._permitted:
            raise MissingPermissionError("fake: bigquery.jobs.listAll missing")

    def table_usage(self) -> list[UsageRow]:
        self.calls["table_usage"] += 1
        return list(self._usage)

    def query_texts(self) -> list[str]:
        self.calls["query_texts"] += 1
        return list(self._query_texts)

    def relation_first_seen(self) -> dict[str, datetime]:
        self.calls["relation_first_seen"] += 1
        return dict(self._first_seen)

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        self.calls["existing_relations"] += 1
        if not datasets:
            return []
        if not self._orphans_permitted:
            raise MissingPermissionError("fake: cannot read managed-dataset table metadata")
        wanted = {dataset.lower() for dataset in datasets}
        return [r for r in self._existing if r.relation_key.split(".")[1] in wanted]

    def source_last_modified(self, datasets: Set[str]) -> dict[str, datetime]:
        self.calls["source_last_modified"] += 1
        if not datasets:
            return {}
        if not self._freshness_permitted:
            raise MissingPermissionError("fake: cannot read source-dataset table metadata")
        wanted = {dataset.lower() for dataset in datasets}
        return {
            key: value
            for key, value in self._last_modified.items()
            if key.rsplit(".", 1)[0] in wanted
        }
