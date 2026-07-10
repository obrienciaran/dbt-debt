"""Tests for the pure unpartitioned-large-tables verdict."""

from __future__ import annotations

from dbt_debt.domain import Model
from dbt_debt.verdict.partitioning import PARTITION_FLOOR_BYTES, unpartitioned_large_tables

_GB = 1024**3


def _model(uid: str, materialized: str | None, **kwargs: object) -> Model:
    return Model(
        unique_id=uid,
        name=uid.split(".")[-1],
        database="p",
        schema="d",
        materialized=materialized,
        **kwargs,  # type: ignore[arg-type]
    )


def test_flags_only_large_bare_tables_and_incrementals() -> None:
    models = {
        "model.p.big": _model("model.p.big", "table"),
        "model.p.inc": _model("model.p.inc", "incremental"),
        "model.p.view": _model("model.p.view", "view"),
        "model.p.part": _model("model.p.part", "table", partitioned=True),
        "model.p.clus": _model("model.p.clus", "table", clustered=True),
        "model.p.tiny": _model("model.p.tiny", "table"),
    }
    storage = {
        "p.d.big": 5 * _GB,
        "p.d.inc": 2 * _GB,
        "p.d.view": 9 * _GB,
        "p.d.part": 9 * _GB,
        "p.d.clus": 9 * _GB,
        "p.d.tiny": PARTITION_FLOOR_BYTES - 1,
    }
    flagged = unpartitioned_large_tables(models, storage)
    # Views cannot be partitioned; declared partition_by/cluster_by clears a table; the floor
    # spares small ones. Largest offender first.
    assert flagged == ("model.p.big", "model.p.inc")


def test_seeds_and_unknown_sizes_are_never_flagged() -> None:
    models = {
        "seed.p.codes": Model(
            unique_id="seed.p.codes",
            name="codes",
            database="p",
            schema="d",
            resource_type="seed",
            materialized="seed",
        ),
        "model.p.nosize": _model("model.p.nosize", "table"),
    }
    assert unpartitioned_large_tables(models, {"p.d.codes": 5 * _GB}) == ()


def test_scanned_bytes_rank_ahead_of_stored_size() -> None:
    # A smaller table user queries actually read outranks a bigger untouched one — the top of
    # the list is the partitioning fix that saves the most, not the biggest table. The floor
    # stays on stored size, so scanned bytes never flag a table too small to matter.
    models = {
        "model.p.busy": _model("model.p.busy", "table"),
        "model.p.idle": _model("model.p.idle", "table"),
        "model.p.small": _model("model.p.small", "table"),
    }
    storage = {"p.d.busy": 2 * _GB, "p.d.idle": 9 * _GB, "p.d.small": _GB // 2}
    scanned = {"p.d.busy": 50 * _GB, "p.d.small": 80 * _GB}
    flagged = unpartitioned_large_tables(models, storage, scanned_bytes=scanned)
    assert flagged == ("model.p.busy", "model.p.idle")


def test_cap_keeps_the_largest() -> None:
    models = {f"model.p.t{i:02d}": _model(f"model.p.t{i:02d}", "table") for i in range(25)}
    storage = {f"p.d.t{i:02d}": (25 - i) * _GB for i in range(25)}
    flagged = unpartitioned_large_tables(models, storage)
    assert len(flagged) == 20
    assert flagged[0] == "model.p.t00"
    assert "model.p.t24" not in flagged
