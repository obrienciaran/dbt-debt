"""Tests for the unused-column verdict and its lineage propagation."""

from __future__ import annotations

from dbt_debt.domain import ColumnEdge
from dbt_debt.verdict.columns import dead_columns

A_ID = "a"
B_ID = "b"


def _edge(up: tuple[str, str], down: tuple[str, str]) -> ColumnEdge:
    return ColumnEdge(upstream=up, downstream=down)


def test_unconsumed_columns_are_dead() -> None:
    all_cols = {(A_ID, "x"), (A_ID, "y")}
    assert dead_columns(all_cols, set(), []) == {(A_ID, "x"), (A_ID, "y")}


def test_consumed_column_is_alive() -> None:
    all_cols = {(A_ID, "x"), (A_ID, "y")}
    assert dead_columns(all_cols, {(A_ID, "x")}, []) == {(A_ID, "y")}


def test_consuming_a_descendant_keeps_its_upstream_alive() -> None:
    # a.x feeds b.x; consuming b.x must keep a.x alive even though a.x is never queried.
    all_cols = {(A_ID, "x"), (B_ID, "x")}
    edges = [_edge((A_ID, "x"), (B_ID, "x"))]
    assert dead_columns(all_cols, {(B_ID, "x")}, edges) == set()


def test_multi_hop_column_propagation() -> None:
    all_cols = {(A_ID, "x"), (B_ID, "x"), ("c", "x")}
    edges = [_edge((A_ID, "x"), (B_ID, "x")), _edge((B_ID, "x"), ("c", "x"))]
    assert dead_columns(all_cols, {("c", "x")}, edges) == set()


def test_consumed_columns_outside_universe_are_ignored() -> None:
    # A source column may be consumed but is not part of all_columns; it must not appear.
    all_cols = {(A_ID, "x")}
    assert dead_columns(all_cols, {("source", "raw")}, []) == {(A_ID, "x")}
