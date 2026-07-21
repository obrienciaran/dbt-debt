"""Tests for the Snowflake client's SDK-free surface and the warehouse seam.

The connector is an optional extra, so these tests pin the behaviour that must hold without it:
the module imports cleanly, the missing dependency produces a friendly `WarehouseError`, the
Protocol is satisfied, and the paths that never reach the SDK (empty datasets, no inferable
database) behave. The missing dependency is simulated via `without_connector`, so these hold
whether or not the extra happens to be installed.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from dbt_debt.config import Config
from dbt_debt.consumption.client import (
    InvalidIdentifierError,
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
    # require the SDK; only constructing a live client does. What actually enforces this is
    # CI installing only [dev]: with the extra absent, a top-level SDK import would break this
    # file's own imports at collection. This is the readable statement of the rule.
    import dbt_debt.consumption.snowflake  # noqa: F401


def test_missing_connector_reads_as_a_friendly_warehouse_error(
    without_connector: Callable[[str], None],
) -> None:
    without_connector("snowflake.connector")
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


def test_existing_relations_rejects_a_malformed_schema_name() -> None:
    # A schema name failing the builder's injection guard must degrade like a missing grant
    # (the CLI warns and skips orphan discovery), never escape as a ValueError traceback.
    with pytest.raises(InvalidIdentifierError, match="orphaned-relation discovery is skipped"):
        _bare_client("analytics").existing_relations({"bad-schema"})
