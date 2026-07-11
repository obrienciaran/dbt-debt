"""Tests for the pure Redshift table-hygiene verdict."""

from __future__ import annotations

from dbt_debt.domain import Model, TableHygiene
from dbt_debt.verdict.redshift_hygiene import (
    HYGIENE_FLOOR_BYTES,
    SKEW_THRESHOLD,
    STATS_OFF_THRESHOLD,
    UNSORTED_THRESHOLD,
    unhealthy_tables,
)

_GB = 1024**3


def _model(uid: str, **kwargs: object) -> Model:
    return Model(
        unique_id=uid,
        name=uid.split(".")[-1],
        database="db",
        schema="s",
        **kwargs,  # type: ignore[arg-type]
    )


def _hygiene(
    unsorted: float = 0,
    stats_off: float = 0,
    skew: float = 0,
    active_bytes: int = 5 * _GB,
) -> TableHygiene:
    return TableHygiene(
        unsorted_percent=unsorted,
        stats_off_percent=stats_off,
        skew_rows=skew,
        total_rows=1000,
        active_bytes=active_bytes,
    )


def test_each_threshold_flags_on_its_own() -> None:
    models = {
        "model.p.vac": _model("model.p.vac"),
        "model.p.ana": _model("model.p.ana"),
        "model.p.skew": _model("model.p.skew"),
    }
    hygiene = {
        "db.s.vac": _hygiene(unsorted=UNSORTED_THRESHOLD),
        "db.s.ana": _hygiene(stats_off=STATS_OFF_THRESHOLD),
        "db.s.skew": _hygiene(skew=SKEW_THRESHOLD),
    }
    flagged = unhealthy_tables(models, hygiene)
    assert set(flagged) == {"model.p.vac", "model.p.ana", "model.p.skew"}


def test_healthy_and_zero_figures_never_flag() -> None:
    # Auto vacuum/analyze keep a well-managed cluster near zero, and NULL columns parse as 0;
    # neither may produce noise — the empty result is the healthy state.
    models = {"model.p.ok": _model("model.p.ok"), "model.p.null": _model("model.p.null")}
    hygiene = {
        "db.s.ok": _hygiene(
            unsorted=UNSORTED_THRESHOLD - 1,
            stats_off=STATS_OFF_THRESHOLD - 1,
            skew=SKEW_THRESHOLD - 1,
        ),
        "db.s.null": _hygiene(),
    }
    assert unhealthy_tables(models, hygiene) == ()


def test_floor_holds_both_ways() -> None:
    # Large but healthy stays out; small but filthy stays out too — maintenance on a table
    # below the floor is cheap anyway.
    models = {"model.p.big": _model("model.p.big"), "model.p.small": _model("model.p.small")}
    hygiene = {
        "db.s.big": _hygiene(active_bytes=9 * _GB),
        "db.s.small": _hygiene(unsorted=90, stats_off=90, active_bytes=HYGIENE_FLOOR_BYTES - 1),
    }
    assert unhealthy_tables(models, hygiene) == ()


def test_scanned_bytes_rank_ahead_of_stored_size() -> None:
    models = {"model.p.busy": _model("model.p.busy"), "model.p.idle": _model("model.p.idle")}
    hygiene = {
        "db.s.busy": _hygiene(unsorted=50, active_bytes=2 * _GB),
        "db.s.idle": _hygiene(unsorted=50, active_bytes=9 * _GB),
    }
    scanned = {"db.s.busy": 50 * _GB}
    flagged = unhealthy_tables(models, hygiene, scanned_bytes=scanned)
    assert flagged == ("model.p.busy", "model.p.idle")


def test_cap_keeps_the_most_scanned() -> None:
    models = {f"model.p.t{i:02d}": _model(f"model.p.t{i:02d}") for i in range(25)}
    hygiene = {f"db.s.t{i:02d}": _hygiene(unsorted=50) for i in range(25)}
    scanned = {f"db.s.t{i:02d}": (25 - i) * _GB for i in range(25)}
    flagged = unhealthy_tables(models, hygiene, scanned_bytes=scanned)
    assert len(flagged) == 20
    assert flagged[0] == "model.p.t00"
    assert "model.p.t24" not in flagged


def test_nodes_without_a_hygiene_row_are_skipped() -> None:
    # Views and ephemeral models have no SVV_TABLE_INFO row (and neither do empty tables);
    # keying on the hygiene map filters them without a materialization check.
    models = {"model.p.view": _model("model.p.view", materialized="view")}
    assert unhealthy_tables(models, {}) == ()


def test_seeds_and_snapshots_are_physical_tables_here() -> None:
    # Unlike the partitioning check (dbt-config debt, models only), hygiene is physical-table
    # debt: a bloated seed or snapshot needs the same VACUUM.
    models = {"seed.p.codes": _model("seed.p.codes", resource_type="seed", materialized="seed")}
    hygiene = {"db.s.codes": _hygiene(unsorted=50)}
    assert unhealthy_tables(models, hygiene) == ("seed.p.codes",)
