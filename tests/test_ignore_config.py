"""Tests for reading the ignore-list JSON file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbt_debt.ignore_config import IgnoreConfigError, load_ignored_models


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "dbt-debt-ignore.json"
    path.write_text(json.dumps(payload))
    return path


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_ignored_models(tmp_path / "nope.json") == {}


def test_valid_file_returns_name_to_reason(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"ignored_models": [{"name": "fct_orders", "reason": "fed by an external export"}]},
    )
    assert load_ignored_models(path) == {"fct_orders": "fed by an external export"}


def test_multiple_entries_all_load(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "ignored_models": [
                {"name": "a", "reason": "reason a"},
                {"name": "b", "reason": "reason b"},
            ]
        },
    )
    assert load_ignored_models(path) == {"a": "reason a", "b": "reason b"}


def test_empty_list_returns_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, {"ignored_models": []})
    assert load_ignored_models(path) == {}


def test_not_valid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "dbt-debt-ignore.json"
    path.write_text("{not json")
    with pytest.raises(IgnoreConfigError):
        load_ignored_models(path)


def test_missing_top_level_key_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"wrong_key": []})
    with pytest.raises(IgnoreConfigError):
        load_ignored_models(path)


def test_top_level_not_a_list_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"ignored_models": "not a list"})
    with pytest.raises(IgnoreConfigError):
        load_ignored_models(path)


def test_entry_missing_reason_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"ignored_models": [{"name": "fct_orders"}]})
    with pytest.raises(IgnoreConfigError):
        load_ignored_models(path)


def test_entry_missing_name_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"ignored_models": [{"reason": "no name given"}]})
    with pytest.raises(IgnoreConfigError):
        load_ignored_models(path)


def test_entry_empty_reason_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"ignored_models": [{"name": "fct_orders", "reason": ""}]})
    with pytest.raises(IgnoreConfigError):
        load_ignored_models(path)


def test_entry_not_an_object_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"ignored_models": ["fct_orders"]})
    with pytest.raises(IgnoreConfigError):
        load_ignored_models(path)
