"""A `BigQueryClient` test double returning canned data, no network or credentials."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Set

from dbt_debt.consumption.client import MissingPermissionError
from dbt_debt.domain import UsageRow, WarehouseRelation


class FakeBigQueryClient:
    """Canned implementation of the `BigQueryClient` Protocol for deterministic tests.

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
    ) -> None:
        self._usage = list(usage)
        self._query_texts = list(query_texts)
        self._permitted = permitted
        self._existing = list(existing)
        self._orphans_permitted = orphans_permitted
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

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        self.calls["existing_relations"] += 1
        if not datasets:
            return []
        if not self._orphans_permitted:
            raise MissingPermissionError("fake: cannot read managed-dataset table metadata")
        wanted = {dataset.lower() for dataset in datasets}
        return [r for r in self._existing if r.relation_key.split(".")[1] in wanted]
