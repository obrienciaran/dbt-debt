"""Tests for the Redshift client's SDK-free surface and the warehouse seam.

The connector is deliberately absent from the dev environment (it is an optional extra), so
these tests pin exactly the behaviour that must hold without it: the module imports cleanly,
the missing dependency produces a friendly `WarehouseError`, the Protocol is satisfied, and
the paths that never reach the SDK (empty datasets, no inferable database, no staleness
metadata) behave.
"""

from __future__ import annotations

import pytest

from dbt_debt.config import Config
from dbt_debt.consumption.client import (
    MissingPermissionError,
    WarehouseClient,
)
from dbt_debt.consumption.redshift import RealRedshiftClient


def _bare_client(database: str | None) -> RealRedshiftClient:
    """A client with no live connection, for exercising the pre-SDK code paths."""

    client = RealRedshiftClient.__new__(RealRedshiftClient)
    client._config = Config(warehouse="redshift")
    client._database = database
    return client


def test_module_imports_without_the_connector() -> None:
    # The lazy-import guarantee: loading the module (as the CLI factory does) must never
    # require the SDK; only constructing a live client does.
    import dbt_debt.consumption.redshift  # noqa: F401


def test_missing_settings_or_connector_reads_as_a_friendly_error() -> None:
    # With the connector absent this trips the extra hint; with it installed but no
    # REDSHIFT_* environment variables it names them instead. Both are WarehouseErrors the
    # CLI turns into exit 3.
    with pytest.raises(Exception, match=r"dbt-debt\[redshift\]|REDSHIFT_HOST"):
        RealRedshiftClient(Config(warehouse="redshift"))


def test_implementations_satisfy_the_protocol() -> None:
    assert issubclass(RealRedshiftClient, WarehouseClient)


def test_existing_relations_empty_datasets_short_circuits() -> None:
    assert _bare_client("dev").existing_relations(set()) == []


def test_existing_relations_without_a_database_steps_aside() -> None:
    # No inferable database means the orphan scan cannot run; it must degrade like a missing
    # metadata grant (skipped with a message), never crash the scan.
    with pytest.raises(MissingPermissionError, match="--project"):
        _bare_client(None).existing_relations({"marts"})


def test_naive_datetimes_are_stamped_utc() -> None:
    # The SYS views report UTC timestamps as `timestamp without time zone`, so the driver
    # returns naive datetimes; the verdicts compare against aware `now` values and would
    # raise on a naive one (seen on the first live scan).
    from datetime import datetime, timezone

    from dbt_debt.consumption.redshift import _as_utc

    naive = datetime(2026, 7, 10, 12, 0, 0)
    assert _as_utc(naive) == datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    aware = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert _as_utc(aware) is aware
    assert _as_utc("not a datetime") == "not a datetime"


def test_source_last_modified_is_always_empty() -> None:
    # Redshift exposes no last-data-received metadata; the stale-source check is skipped by
    # the CLI, and the client must never guess.
    assert _bare_client("dev").source_last_modified({"dev.raw"}) == {}
