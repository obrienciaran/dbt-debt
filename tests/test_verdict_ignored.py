"""Tests for resolving ignore-list model names against the manifest."""

from __future__ import annotations

import pytest

from dbt_debt.domain import Manifest, Model
from dbt_debt.verdict.ignored import UnknownIgnoredModelError, ignored_model_ids


def _manifest() -> Manifest:
    return Manifest(
        project_name="t",
        dbt_schema_version="",
        dbt_version=None,
        models={
            "model.p.a": Model(unique_id="model.p.a", name="a"),
            "model.p.b": Model(unique_id="model.p.b", name="b"),
        },
    )


def test_names_resolve_to_unique_ids() -> None:
    assert ignored_model_ids(_manifest(), {"a": "used elsewhere"}) == {"model.p.a"}


def test_empty_reasons_resolve_to_empty_set() -> None:
    assert ignored_model_ids(_manifest(), {}) == set()


def test_multiple_names_all_resolve() -> None:
    reasons = {"a": "reason a", "b": "reason b"}
    assert ignored_model_ids(_manifest(), reasons) == {"model.p.a", "model.p.b"}


def test_unknown_name_raises_with_the_name_in_the_message() -> None:
    with pytest.raises(UnknownIgnoredModelError, match="typo_name"):
        ignored_model_ids(_manifest(), {"typo_name": "does not exist"})


def test_one_unknown_name_fails_the_whole_batch_even_with_valid_names_present() -> None:
    # A silent partial match would hide the typo; the whole ignore file must be trustworthy.
    with pytest.raises(UnknownIgnoredModelError):
        ignored_model_ids(_manifest(), {"a": "fine", "nonexistent": "typo"})
