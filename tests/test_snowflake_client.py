"""Tests for the Snowflake client's SDK-free surface and the warehouse seam.

The connector is deliberately absent from the dev environment (it is an optional extra), so
these tests pin exactly the behaviour that must hold without it: the module imports cleanly,
the missing dependency produces a friendly `WarehouseError`, the Protocol is satisfied, and the
paths that never reach the SDK (empty datasets, no inferable database) behave.
"""

from __future__ import annotations

import pytest

from dbt_debt.config import Config
from dbt_debt.consumption.client import (
    MissingPermissionError,
    WarehouseClient,
    WarehouseError,
)
from dbt_debt.consumption.snowflake import RealSnowflakeClient
from tests.fakes import FakeWarehouseClient


def _bare_client(database: str | None) -> RealSnowflakeClient:
    """A client with no live connection, for exercising the pre-SDK code paths."""

    client = RealSnowflakeClient.__new__(RealSnowflakeClient)
    client._config = Config(warehouse="snowflake")
    client._database = database
    return client


def test_module_imports_without_the_connector() -> None:
    # The lazy-import guarantee: loading the module (as the CLI factory does) must never
    # require the SDK; only constructing a live client does.
    import dbt_debt.consumption.snowflake  # noqa: F401


def test_missing_connector_reads_as_a_friendly_warehouse_error() -> None:
    with pytest.raises(WarehouseError, match=r"dbt-debt\[snowflake\]"):
        RealSnowflakeClient(Config(warehouse="snowflake"))


def test_implementations_satisfy_the_protocol() -> None:
    assert issubclass(RealSnowflakeClient, WarehouseClient)
    assert issubclass(FakeWarehouseClient, WarehouseClient)


def test_existing_relations_empty_datasets_short_circuits() -> None:
    assert _bare_client("analytics").existing_relations(set()) == []


def test_existing_relations_without_a_database_steps_aside() -> None:
    # No inferable database means the orphan scan cannot run; it must degrade like a missing
    # metadata grant (skipped with a message), never crash the scan.
    with pytest.raises(MissingPermissionError, match="--project"):
        _bare_client(None).existing_relations({"marts"})
