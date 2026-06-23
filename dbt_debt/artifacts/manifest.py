"""Load and parse dbt's manifest.json into domain objects.

We read the artifact as plain JSON rather than importing dbt: it is lighter and avoids
coupling the tool to a dbt version. Unknown keys are ignored so minor schema changes do not
break loading; the schema version is captured so a breaking change is at least visible.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from dbt_debt.artifacts._json import as_dict
from dbt_debt.domain import Column, Exposure, Manifest, Model, Relation, Test, relation_key

logger = logging.getLogger(__name__)

KNOWN_SCHEMA_PREFIX = "https://schemas.getdbt.com/dbt/manifest/v"


def load_manifest(path: str | Path) -> Manifest:
    """Read manifest.json from disk and parse it into a Manifest."""

    data = json.loads(Path(path).read_text())
    return parse_manifest(data)


def parse_manifest(data: dict[str, Any]) -> Manifest:
    """Parse an already-loaded manifest dict into domain objects."""

    metadata = as_dict(data.get("metadata"))
    schema_version = str(metadata.get("dbt_schema_version", ""))
    _check_schema_version(schema_version)

    models: dict[str, Model] = {}
    tests: dict[str, Test] = {}
    relations: dict[str, Relation] = {}
    for unique_id, node in as_dict(data.get("nodes")).items():
        resource_type = node.get("resource_type")
        if resource_type == "model":
            models[unique_id] = _parse_model(unique_id, node)
        elif resource_type == "test":
            tests[unique_id] = _parse_test(unique_id, node)
        elif resource_type in ("seed", "snapshot"):
            relations[unique_id] = _parse_relation(unique_id, node, materialized=True)

    for unique_id, node in as_dict(data.get("sources")).items():
        relations[unique_id] = _parse_relation(unique_id, node, materialized=False)

    exposures = {
        unique_id: _parse_exposure(unique_id, node)
        for unique_id, node in as_dict(data.get("exposures")).items()
    }

    return Manifest(
        project_name=str(metadata.get("project_name", "")),
        dbt_schema_version=schema_version,
        dbt_version=metadata.get("dbt_version"),
        models=models,
        tests=tests,
        exposures=exposures,
        relations=relations,
    )


def _parse_model(unique_id: str, node: dict[str, Any]) -> Model:
    columns = {
        name: Column(name=col.get("name", name))
        for name, col in as_dict(node.get("columns")).items()
    }
    return Model(
        unique_id=unique_id,
        name=node.get("name", ""),
        database=node.get("database"),
        schema=node.get("schema"),
        alias=node.get("alias"),
        original_file_path=node.get("original_file_path"),
        depends_on=_depends_on(node),
        columns=columns,
        contract_enforced=bool(as_dict(node.get("contract")).get("enforced", False)),
        compiled_code=node.get("compiled_code"),
    )


def _parse_test(unique_id: str, node: dict[str, Any]) -> Test:
    return Test(
        unique_id=unique_id,
        name=node.get("name", ""),
        depends_on=_depends_on(node),
        attached_node=node.get("attached_node"),
        column_name=node.get("column_name"),
    )


def _parse_relation(unique_id: str, node: dict[str, Any], *, materialized: bool) -> Relation:
    """Parse a seed/snapshot node or a source into a non-model `Relation`.

    Seeds and snapshots key off `alias` (falling back to `name`); a source keys off `identifier`
    (falling back to `name`). The relation_key is built the same way as a model's so both sides of
    the orphan subtraction compare equal.
    """

    database = node.get("database")
    schema = node.get("schema")
    identifier = node.get("alias") or node.get("identifier") or node.get("name")
    return Relation(
        unique_id=unique_id,
        relation_key=relation_key(database, schema, identifier),
        schema=schema,
        materialized=materialized,
    )


def _parse_exposure(unique_id: str, node: dict[str, Any]) -> Exposure:
    return Exposure(
        unique_id=unique_id,
        name=node.get("name", ""),
        depends_on=_depends_on(node),
    )


def _depends_on(node: dict[str, Any]) -> tuple[str, ...]:
    nodes = as_dict(node.get("depends_on")).get("nodes") or []
    return tuple(nodes)


def _check_schema_version(schema_version: str) -> None:
    if not schema_version:
        logger.warning("manifest has no dbt_schema_version; parsing best-effort")
    elif not schema_version.startswith(KNOWN_SCHEMA_PREFIX):
        logger.warning(
            "unrecognized manifest schema version %r; parsing best-effort", schema_version
        )
