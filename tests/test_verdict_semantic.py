"""Tests for the semantic-layer impact verdict and its transitive resolution."""

from __future__ import annotations

from dbt_debt.domain import Manifest, Model, SemanticConsumer
from dbt_debt.verdict.semantic import affected_semantic_consumers


def _manifest(consumers: dict[str, SemanticConsumer]) -> Manifest:
    return Manifest(
        project_name="t",
        dbt_schema_version="",
        dbt_version=None,
        models={"model.t.fct": Model(unique_id="model.t.fct", name="fct")},
        semantic_consumers=consumers,
    )


def _chain() -> dict[str, SemanticConsumer]:
    return {
        "semantic_model.t.orders": SemanticConsumer(
            unique_id="semantic_model.t.orders",
            name="orders",
            kind="semantic_model",
            depends_on=("model.t.fct",),
        ),
        "metric.t.revenue": SemanticConsumer(
            unique_id="metric.t.revenue",
            name="revenue",
            kind="metric",
            depends_on=("semantic_model.t.orders",),
        ),
        "saved_query.t.weekly": SemanticConsumer(
            unique_id="saved_query.t.weekly",
            name="weekly",
            kind="saved_query",
            depends_on=("metric.t.revenue",),
        ),
    }


def test_dead_model_affects_the_whole_semantic_chain() -> None:
    # semantic model → metric → saved query: impact flows through every hop, and each result
    # carries the dependency that condemned it, in discovery order so the chain reads top-down.
    result = affected_semantic_consumers(_manifest(_chain()), {"model.t.fct"})
    assert [(a.consumer.unique_id, a.via) for a in result] == [
        ("semantic_model.t.orders", "model.t.fct"),
        ("metric.t.revenue", "semantic_model.t.orders"),
        ("saved_query.t.weekly", "metric.t.revenue"),
    ]


def test_direct_dead_model_wins_over_an_affected_consumer() -> None:
    # A metric on both a dead model and an affected semantic model is reported against the
    # dead model: the direct cause is the actionable one.
    consumers = _chain()
    consumers["metric.t.revenue"] = SemanticConsumer(
        unique_id="metric.t.revenue",
        name="revenue",
        kind="metric",
        depends_on=("semantic_model.t.orders", "model.t.fct"),
    )
    result = affected_semantic_consumers(_manifest(consumers), {"model.t.fct"})
    by_uid = {a.consumer.unique_id: a.via for a in result}
    assert by_uid["metric.t.revenue"] == "model.t.fct"


def test_alive_models_affect_nothing() -> None:
    assert affected_semantic_consumers(_manifest(_chain()), set()) == []


def test_metric_cycles_terminate() -> None:
    consumers = {
        "metric.t.a": SemanticConsumer(
            unique_id="metric.t.a", name="a", kind="metric", depends_on=("metric.t.b",)
        ),
        "metric.t.b": SemanticConsumer(
            unique_id="metric.t.b", name="b", kind="metric", depends_on=("metric.t.a",)
        ),
    }
    # A dependency cycle with no dead model beneath it resolves to unaffected, not a hang.
    assert affected_semantic_consumers(_manifest(consumers), set()) == []
